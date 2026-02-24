import base64
import json
import os
from pathlib import Path

import chainlit as cl
import httpx
from chainlit.types import ThreadDict
from httpx_sse import aconnect_sse
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_core import to_jsonable_python
from utils.auth import setup_oauth
from utils.data_persistence import setup_data_persistence

setup_oauth()
setup_data_persistence()

BACKEND_URL = os.environ["BACKEND_URL"]


@cl.on_chat_start
async def start():
    user = cl.user_session.get("user")
    cl.user_session.set("chat_history", [])
    if user:
        _ = await cl.Message(
            f"Hello, {user.identifier}! I'm your Interview Preparation Assistant. "
            "Upload your resume (PDF) to get started, or ask me about your "
            "previous preparations."
        ).send()


@cl.on_message
async def on_message(message: cl.Message):
    msg = cl.Message("")
    chat_history = cl.user_session.get("chat_history") or []
    user = cl.user_session.get("user")

    auth_headers = {"Authorization": f"Bearer {user.metadata['token']}"}
    payload = {
        "user_email": user.metadata["email"],
        "message": message.content,
        "chat_history_json": json.dumps(to_jsonable_python(chat_history)),
    }

    if message.elements:
        for element in message.elements:
            if element.mime and element.mime == "application/pdf" and element.path:
                pdf_data = Path(element.path).read_bytes()
                payload["resume_bytes_b64"] = base64.b64encode(pdf_data).decode()
                break

    async with httpx.AsyncClient(
        base_url=BACKEND_URL, headers=auth_headers, timeout=300.0
    ) as client:
        async with aconnect_sse(client, "POST", "/chat", json=payload) as event_source:
            try:
                async for sse in event_source.aiter_sse():
                    match sse.event:
                        case "token":
                            data = json.loads(sse.data)
                            await msg.stream_token(data["text"])

                        case "tool_call":
                            data = json.loads(sse.data)
                            async with cl.Step(name=data["name"], type="tool") as step:
                                step.input = data["args"]

                        case "pdf_generated":
                            data = json.loads(sse.data)
                            msg.elements = [
                                cl.Pdf(
                                    name="Interview Prep",
                                    url=data["url"],
                                    display="inline",
                                )
                            ]

                        case "prep_list":
                            data = json.loads(sse.data)
                            table_lines = [
                                "| Document | Created | Download |",
                                "| --- | --- | --- |",
                            ]
                            for item in data["preps"]:
                                table_lines.append(
                                    f"| {item['name']} | {item['created_at']} | [Download PDF]({item['url']}) |"
                                )
                            table_md = "\n".join(table_lines)
                            await msg.stream_token(f"\n\n{table_md}\n\n")

                        case "error":
                            data = json.loads(sse.data)
                            msg.content = f"An error occurred: {data['message']}"

                        case _:
                            pass
            except httpx.RemoteProtocolError as e:
                if (
                    str(e)
                    == "peer closed connection without sending complete message body (incomplete chunked read)"
                ):
                    await msg.stream_token(
                        " The research is taking a bit longer than expected. Please list the interview preps in 4-5 minutes to download your report!"
                    )
                else:
                    raise

    await msg.send()

    chat_history.append(ModelRequest(parts=[UserPromptPart(content=message.content)]))
    chat_history.append(ModelResponse(parts=[TextPart(content=msg.content)]))
    cl.user_session.set("chat_history", chat_history)


@cl.on_chat_resume
async def resume(thread: ThreadDict):
    chat_history: list[ModelMessage] = []

    for step in thread["steps"]:
        step_type = step.get("type", "")
        output = step.get("output", "")

        if step_type == "user_message" and output:
            chat_history.append(ModelRequest(parts=[UserPromptPart(content=output)]))
        elif step_type == "assistant_message" and output:
            chat_history.append(ModelResponse(parts=[TextPart(content=output)]))

    cl.user_session.set("chat_history", chat_history)
