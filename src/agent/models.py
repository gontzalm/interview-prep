import base64

from pydantic import BaseModel, Field, Json
from pydantic_ai import ModelMessage, ModelMessagesTypeAdapter


class ChatRequest(BaseModel):
    user_email: str
    message: str
    resume_bytes_b64: str | None = None
    resume_bytes: bytes | None = Field(
        default_factory=lambda data: (
            base64.b64decode(data["resume_bytes_b64"])
            if data["resume_bytes_b64"] is not None
            else None
        )
    )
    chat_history_json: Json
    chat_history: list[ModelMessage] = Field(
        default_factory=lambda data: ModelMessagesTypeAdapter.validate_python(
            data["chat_history_json"]
        )
    )
