import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .agent import sse_generator
from .models import ChatRequest

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/chat")
async def chat_endpoint(
    request: ChatRequest,
) -> StreamingResponse:
    """Handle chat requests and stream SSE responses."""
    logger.info("Chat request from %s", request.user_email)

    return StreamingResponse(
        sse_generator(request),
        media_type="text/event-stream",
    )
