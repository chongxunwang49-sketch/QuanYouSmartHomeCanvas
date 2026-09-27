"""智友问答（对话式 RAG）。见 `answer.py`。"""

from .answer import (
    ChatContext,
    build_context,
    build_history,
    build_messages,
    stream_answer,
    SYSTEM_PROMPT,
)

__all__ = [
    "ChatContext", "build_context", "build_history", "build_messages",
    "stream_answer", "SYSTEM_PROMPT",
]
