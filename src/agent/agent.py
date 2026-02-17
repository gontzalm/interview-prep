import json
import logging
import os
from collections.abc import AsyncGenerator
from pathlib import Path

import boto3
import httpx
import logfire
from pydantic_ai import (
    Agent,
    AgentRunResultEvent,
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    TextPartDelta,
)
from pydantic_ai.mcp import MCPServerStreamableHTTP
from pydantic_ai.messages import (
    AgentStreamEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    ToolReturnPart,
)

from .._shared.auth import AwsBotoAuth
from .models import ChatRequest

logger = logging.getLogger(__name__)

# Set up Logfire
os.environ["LOGFIRE_TOKEN"] = boto3.client("secretsmanager").get_secret_value(
    SecretId=os.environ["LOGFIRE_SECRET"]
)["SecretString"]

logfire.configure()
logfire.instrument_pydantic_ai()

MAX_HISTORY_MESSAGES = 20


def create_mcp_server_client(user_email: str) -> MCPServerStreamableHTTP:
    """Create an MCP server connection with the user's email header injected."""
    return MCPServerStreamableHTTP(
        os.environ["MCP_URL"],
        http_client=httpx.AsyncClient(
            auth=AwsBotoAuth(), headers={"X-User-Email": user_email}, timeout=20.0
        ),
    )


def process_history(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Process message history to keep it within token limits.

    Keeps the last N messages while ensuring:
    - The first message is always a ModelRequest (human message).
    - Tool calls and tool returns are always paired (never split).

    Args:
        messages: Full message history.

    Returns:
        Trimmed message history.
    """
    if len(messages) <= MAX_HISTORY_MESSAGES:
        return messages

    # Start from the end and work backwards to find a safe cut point
    trimmed = messages[-MAX_HISTORY_MESSAGES:]

    # Ensure the first message is a ModelRequest
    while trimmed and isinstance(trimmed[0], ModelResponse):
        trimmed = trimmed[1:]

    # Verify tool call/return pairing at the boundaries.
    # If the first message is a ModelRequest containing ToolReturnParts
    # but there's no preceding ModelResponse with matching ToolCallParts,
    # we need to skip past them.
    first = trimmed[0]
    if isinstance(first, ModelRequest):
        has_tool_returns = any(isinstance(part, ToolReturnPart) for part in first.parts)
        if has_tool_returns:
            # This ModelRequest has tool returns without their matching calls.
            # Skip it and the next response (if any) to find a clean boundary.
            trimmed = trimmed[1:]
            while trimmed and isinstance(trimmed[0], ModelResponse):
                trimmed = trimmed[1:]

    return trimmed


agent = Agent(
    f"bedrock:{os.environ['AGENT_MODEL']}",
    instructions=(Path(__file__).parent / "instructions.md").read_text(),
    history_processors=[process_history],
)


def _format_sse_event(event: AgentStreamEvent | AgentRunResultEvent) -> str | None:
    """Convert a Pydantic AI stream event to an SSE event string."""
    match event:
        case PartStartEvent(part=part) if isinstance(part, TextPart):
            data = json.dumps({"text": part.content})
            return f"event: token\ndata: {data}\n\n"

        case PartDeltaEvent(delta=delta) if isinstance(delta, TextPartDelta):
            data = json.dumps({"text": delta.content_delta})
            return f"event: token\ndata: {data}\n\n"

        case FunctionToolCallEvent(part=part):
            data = json.dumps(
                {
                    "name": part.tool_name,
                    "args": part.args
                    if isinstance(part.args, str)
                    else json.dumps(part.args),
                }
            )
            return f"event: tool_call\ndata: {data}\n\n"

        case FunctionToolResultEvent(result=result):
            content = result.content
            logger.info("Got tool result content '%s'", content)

            match result.tool_name:
                case "generate_prep":
                    if content.startswith("https://") and ".s3." in content:
                        data = json.dumps({"url": content})
                        return f"event: pdf_generated\ndata: {data}\n\n"

                case "list_preps":
                    # Check if the result is a list of preps (from list_preps)
                    logger.info("Sent prep_list event")
                    data = json.dumps({"preps": content})
                    return f"event: prep_list\ndata: {data}\n\n"


async def sse_generator(chat_request: ChatRequest) -> AsyncGenerator[str]:
    """Stream SSE events from the agent to the client.

    Args:
        chat_request: ChatRequest object.

    Yields:
        SSE-formatted event strings.
    """
    # If resume bytes are provided, prepend instruction to the user input and include PDF
    if chat_request.resume_bytes:
        user_input = [
            chat_request.message
            + (
                "\n\nThe user has attached a PDF resume. Call the `upload_resume` tool."
            ),
            BinaryContent(data=chat_request.resume_bytes, media_type="application/pdf"),
        ]
    else:
        user_input = chat_request.message

    try:
        async for event in agent.run_stream_events(
            user_input,
            message_history=chat_request.chat_history,
            toolsets=[create_mcp_server_client(chat_request.user_email)],
        ):
            sse_event = _format_sse_event(event)
            if sse_event is not None:
                yield sse_event

    except Exception as e:
        logger.exception("Agent run failed")
        error_data = json.dumps({"message": str(e)})
        yield f"event: error\ndata: {error_data}\n\n"
