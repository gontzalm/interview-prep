import asyncio
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

import boto3
import httpx
from botocore.exceptions import ClientError
from fasta2a.client import A2AClient
from fasta2a.schema import Message
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from md2pdf.core import md2pdf
from pydantic import BaseModel

from .._shared.auth import AwsBotoAuth

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

STORAGE_BUCKET = os.environ["STORAGE_BUCKET"]
RESEARCH_SUBAGENT_URL = os.environ["RESEARCH_SUBAGENT_URL"]

NO_RESUME_MSG = (
    "No resume found. Please ask the user to upload their PDF resume "
    "so you can process it."
)

s3 = boto3.client("s3")
mcp = FastMCP("Interview Prep Tools")
a2a_client = A2AClient(
    base_url=RESEARCH_SUBAGENT_URL,
    http_client=httpx.AsyncClient(auth=AwsBotoAuth(), timeout=20.0),
)


def _get_user_email() -> str:
    """Extract user email from the X-User-Email header."""
    headers = get_http_headers()
    email = headers.get("x-user-email")
    if not email:
        raise ValueError("Missing X-User-Email header")
    return email.replace("@", "_at_")


def _fetch_resume_text(email: str) -> str | None:
    """Fetch resume text from S3 for the given email."""
    key = f"{email}/resume.txt"
    try:
        response = s3.get_object(Bucket=STORAGE_BUCKET, Key=key)
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return
        raise

    return response["Body"].read().decode("utf-8")


@mcp.tool()
def get_resume() -> str:
    """Get the current user's resume text from S3.

    Returns the plain text content of the user's resume, or a message
    indicating the agent should ask the user to upload their PDF resume.
    """
    return _fetch_resume_text(_get_user_email()) or NO_RESUME_MSG


@mcp.tool()
def upload_resume(content: str) -> str:
    """Upload a PDF resume content to S3.

    Saves the PDF content to S3 for later use in interview preparation.

    Args:
        content: The string content of PDF resume file.

    Returns:
        Confirmation message.
    """
    email = _get_user_email()

    resume_key = f"{email}/resume.txt"
    s3.put_object(
        Bucket=STORAGE_BUCKET,
        Key=resume_key,
        Body=content.encode("utf-8"),
    )
    logger.info("Saved resume text to s3://%s/%s", STORAGE_BUCKET, resume_key)

    return "Resume uploaded successfully."


class InterviewPrepMetadata(BaseModel):
    """Metadata for a generated interview prep document."""

    name: str
    created_at: str
    url: str


@mcp.tool()
def list_preps() -> list[InterviewPrepMetadata]:
    """List all generated interview preparation documents for the current user.

    Returns:
        List of prep document metadata including name, creation date,
        and presigned download URL.
    """
    email = _get_user_email()
    prefix = f"{email}/preps/"

    response = s3.list_objects_v2(Bucket=STORAGE_BUCKET, Prefix=prefix)

    preps: list[InterviewPrepMetadata] = []
    for obj in response["Contents"]:
        key = obj["Key"]
        filename = key.removeprefix(prefix)

        presigned_url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": STORAGE_BUCKET, "Key": key},
            ExpiresIn=3600,
        )

        preps.append(
            InterviewPrepMetadata(
                name=filename.removesuffix(".pdf"),
                created_at=obj["LastModified"].isoformat(),
                url=presigned_url,
            )
        )

    return preps


@mcp.tool()
async def generate_prep(job_description: str) -> str:
    """Generate an interview preparation document.

    Fetches the user's resume, calls the research subagent to create a
    strategic interview prep, converts the result to PDF, and stores it
    in S3.

    Args:
        job_description: The full job description text to prepare for.

    Returns:
        A presigned URL to download the generated PDF, or an error message.
    """
    email = _get_user_email()

    # Fetch resume text
    resume_text = _fetch_resume_text(email)
    if resume_text is None:
        return NO_RESUME_MSG

    message: Message = {
        "role": "user",
        "parts": [
            {
                "kind": "text",
                "text": "\n\n".join(
                    [
                        "## Candidate Resume",
                        resume_text,
                        "## Job Description",
                        job_description,
                    ]
                ),
            }
        ],
        "kind": "message",
        "message_id": f"prep-{datetime.now().isoformat()}",
    }

    r = await a2a_client.send_message(message)
    task_id = r["result"]["id"]

    logger.info("Started subagent task ID '%s'", task_id)

    # Poll until completed
    for _ in range(120):  # Max ~2 minutes of polling
        logger.info("Polling subagent task ID '%s' status", task_id)
        r = await a2a_client.get_task(task_id)
        task = r["result"]
        state = task["status"]["state"]

        match state:
            case "completed":
                break
            case "failed" | "canceled" | "rejected":
                logger.error(
                    "Subagent task ID '%s' failed with state '%s' ",
                    task_id,
                    state,
                )
                return f"Research subagent failed with state: {state}"

        await asyncio.sleep(1)
    else:
        logger.warning("Subagent task ID '%s' timed out", task_id)
        return "Research subagent timed out."

    # Extract markdown from artifacts
    markdown_content = ""
    for artifact in task.get("artifacts", []):
        for part in artifact["parts"]:
            if part["kind"] == "text":
                markdown_content += part["text"]

    if not markdown_content:
        return "Research subagent returned no content."

    # Extract company-position from the markdown title for the filename
    title_match = re.search(r"#.*?:\s*(.+?)$", markdown_content, re.MULTILINE)
    if title_match:
        filename_base = re.sub(r"[^\w\s-]", "", title_match.group(1).strip())
        filename_base = re.sub(r"\s+", "-", filename_base).lower()
    else:
        filename_base = f"prep-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    # Convert Markdown -> PDF using md2pdf
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp_path = Path(tmp.name)
        md2pdf(
            tmp_path, raw=markdown_content, css=Path(__file__).parent / "pdf-styles.css"
        )
        pdf_bytes = tmp_path.read_bytes()

    # Upload to S3
    pdf_key = f"{email}/preps/{filename_base}.pdf"
    s3.put_object(Bucket=STORAGE_BUCKET, Key=pdf_key, Body=pdf_bytes)
    logger.info("Saved prep PDF to s3://%s/%s", STORAGE_BUCKET, pdf_key)

    # Generate presigned URL
    presigned_url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": STORAGE_BUCKET, "Key": pdf_key},
        ExpiresIn=3600,
    )

    return presigned_url
