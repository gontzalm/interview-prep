import logging
import os
from pathlib import Path

import boto3
import logfire
from pydantic_ai import Agent
from tavily import TavilyClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Set up Logfire
os.environ["LOGFIRE_TOKEN"] = boto3.client("secretsmanager").get_secret_value(
    SecretId=os.environ["LOGFIRE_SECRET"]
)["SecretString"]

logfire.configure()
logfire.instrument_pydantic_ai()

# Set up Tavily
_tavily_api_key = boto3.client("secretsmanager").get_secret_value(
    SecretId=os.environ["TAVILY_SECRET"]
)["SecretString"]
_tavily_client = TavilyClient(api_key=_tavily_api_key)

agent = Agent(
    f"bedrock:{os.environ['RESEARCH_SUBAGENT_MODEL']}",
    instructions=(Path(__file__).parent / "instructions.md").read_text(),
)


@agent.tool_plain
def research_company(query: str) -> str:
    """Research a company or topic using web search.

    Use this tool to gather real-time information about a company, including
    culture, recent news, interview processes, and more. Formulate specific
    search queries for best results.

    Args:
        query: The search query (e.g. "Acme Corp recent news 2026",
            "Acme Corp engineering interview questions").

    Returns:
        Search results as formatted text with titles, URLs, and content.
    """
    logger.info("Researching: %s", query)

    response = _tavily_client.search(query=query, max_results=5)

    results: list[str] = []
    for result in response.get("results", []):
        results.append(
            f"**{result['title']}**\nURL: {result['url']}\n{result['content']}\n"
        )

        return "\n---\n".join(results) if results else "No results found."


app = agent.to_a2a(debug=True)
