"""Claude backend using claude-webapi (aiohttp-based, works where
webai-provider's curl_cffi gets Cloudflare-challenged)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from webai_provider import (
    ConversationBound,
    MessageMetadata,
    ReplaceText,
    ResponseComplete,
    TextDelta,
)

from .cookies import parse_cookies
from .client import load_saved, save_entry, resume_entry, STATE_DIR

KNOWN_MODELS = [
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-haiku-4-5-20251001",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
]


class ClaudeWebSession:
    """Same interface as client.ChatSession, backed by claude-webapi."""

    provider_name = "claude"

    def __init__(self, state_dir: Path = STATE_DIR):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.client = None          # claude_webapi.ClaudeClient
        self.chat = None            # claude_webapi.session.ChatSession
        self._conversation_id: str | None = None
        self.model: str | None = None
        self.effort: str | None = None
        self.temporary = False
        self.adapter = None         # non-None while connected (app gate)
        self.catalog = None

    @property
    def conversation_id(self) -> str | None:
        return self._conversation_id

    @conversation_id.setter
    def conversation_id(self, value) -> None:
        # Changing the id externally (/resume, /resume-remote) forces a new
        # ChatSession bound to that conversation on next send.
        if value != self._conversation_id:
            self.chat = None
        self._conversation_id = value

    # ---- lifecycle ----------------------------------------------------

    async def connect(self, provider: str, cookies_path: Path) -> None:
        from claude_webapi import ClaudeClient

        await self.close()
        cookies = parse_cookies(cookies_path, "claude")
        self.client = ClaudeClient(
            session_key=cookies["sessionKey"],
            organization_id=cookies.get("lastActiveOrg") or None,
        )
        await self.client.init(timeout=60, auto_close=False)
        self.adapter = self.client
        self.conversation_id = None
        self._new_chat()

    async def close(self) -> None:
        if self.client is not None:
            try:
                await self.client.close()
            except Exception:
                pass
            self.client = None
        self.chat = None
        self.adapter = None

    def new_chat(self) -> None:
        self.conversation_id = None
        self.chat = None

    def _new_chat(self, metadata: dict | None = None) -> None:
        self.chat = self.client.start_chat(model=self.model, metadata=metadata)
        self._conversation_id = self.chat.cid

    # ---- models --------------------------------------------------------

    async def refresh_models(self):
        models = [SimpleNamespace(id=m, title=m) for m in KNOWN_MODELS]
        self.catalog = models
        return models

    async def set_model(self, name: str | None) -> None:
        if not name:
            self.model = None
            return
        lowered = name.lower()
        match = next((m for m in KNOWN_MODELS if m.lower() == lowered), None)
        if match is None:
            match = next((m for m in KNOWN_MODELS if lowered in m.lower()), None)
        # accept raw model strings too — claude-webapi passes them through
        self.model = match or name

    # ---- chat ----------------------------------------------------------

    async def _ensure_chat(self) -> None:
        if self.chat is None:
            if self.conversation_id:
                metadata = await self._resume_metadata(self.conversation_id)
                self._new_chat(metadata=metadata)
            else:
                self._new_chat()

    async def _resume_metadata(self, cid: str) -> dict:
        """Best-effort resume metadata for an existing remote conversation."""
        try:
            conv = await self.client.get_conversation(cid)
            leaf = conv.get("current_leaf_message_uuid")
            if leaf:
                return {"conversation_id": cid, "parent_message_uuid": leaf}
        except Exception:
            pass
        return {"conversation_id": cid,
                "parent_message_uuid": "00000000-0000-4000-8000-000000000000"}

    async def send_stream(self, prompt: str, attachments: tuple[dict, ...] = ()):
        if self.client is None:
            raise RuntimeError("not connected")
        await self._ensure_chat()
        files = [item.get("_path") for item in attachments if item.get("_path")]
        yield ConversationBound(self.chat.cid)
        async for chunk in self.chat.send_message_stream(
            prompt or "Describe the attached files.", files=files or None
        ):
            delta = getattr(chunk, "text_delta", "") or ""
            if delta:
                yield TextDelta(delta, "final")
            thoughts = getattr(chunk, "thoughts", "") or ""
            if thoughts:
                yield TextDelta(thoughts, "analysis")
        yield ResponseComplete()
        yield MessageMetadata()

    # ---- history --------------------------------------------------------

    async def list_chats(self, limit: int = 30):
        if self.client is None:
            return []
        convs = await self.client.list_conversations()
        return [
            SimpleNamespace(
                origin=SimpleNamespace(remote_id=c.get("uuid") or c.get("id") or ""),
                title=c.get("name") or "(untitled)",
            )
            for c in (convs or [])[:limit]
        ]

    async def get_chat(self, conversation_id: str):
        if self.client is None:
            return None
        conv = await self.client.get_conversation(conversation_id)
        messages = []
        for m in conv.get("chat_messages") or conv.get("messages") or []:
            text = " ".join(
                p for p in (m.get("content") or []) if isinstance(p, str)
            ) or str(m.get("text") or "")
            messages.append(SimpleNamespace(
                role=(m.get("sender") or "").lower(),
                content=[SimpleNamespace(text=text)],
            ))
        return SimpleNamespace(messages=messages)

    async def upload(self, path) -> dict:
        # claude-webapi takes file paths directly in send_message
        return {"_path": str(path)}

    async def delete_remote_chat(self, conversation_id: str | None = None) -> None:
        cid = conversation_id or self.conversation_id
        if self.client is not None and cid:
            try:
                await self.client.delete_conversation(cid)
            except Exception:
                pass

    # ---- local persistence (shared with ChatSession) --------------------

    def save_chat(self, name: str) -> str | None:
        return save_entry(name, self.provider_name, self.conversation_id, self.model)

    def resume_chat(self, name: str):
        return resume_entry(name)
