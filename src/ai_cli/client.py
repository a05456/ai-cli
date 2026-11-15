"""Async session wrapper around webai-provider for ChatGPT + Claude."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from webai_provider import (
    ConversationBound,
    ProviderClient,
    ReplaceText,
    ResponseComplete,
    SendRequest,
    TextDelta,
    ThoughtDelta,
    create_provider,
)

from .cookies import (
    parse_cookies,
    prepare_chatgpt_session,
    prepare_claude_session,
    prepare_grok_session,
)

STATE_DIR = Path.home() / ".ai-cli"
CHATS_FILE = STATE_DIR / "chats.json"

PROVIDERS = ("chatgpt", "claude", "grok")
DEFAULT_PROVIDER = "chatgpt"


# ---- shared local chat persistence --------------------------------------

def load_saved() -> dict:
    if not CHATS_FILE.is_file():
        return {}
    try:
        return json.loads(CHATS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_entry(name: str, provider: str, conversation_id, model) -> str | None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = load_saved()
    data[name] = {
        "provider": provider,
        "conversation_id": conversation_id,
        "model": model,
    }
    CHATS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return conversation_id


def resume_entry(name: str) -> dict | None:
    return load_saved().get(name)


class ChatSession:
    """Owns the ProviderClient and the active provider adapter."""

    def __init__(self, state_dir: Path = STATE_DIR):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.client: ProviderClient | None = None
        self.adapter = None
        self.provider_name = DEFAULT_PROVIDER
        self.credential_file: Path | None = None
        self.conversation_id: str | None = None
        self.model: str | None = None
        self.effort: str | None = None
        self.temporary = False
        self.catalog = None

    # ---- lifecycle ----------------------------------------------------

    async def connect(self, provider: str, cookies_path: Path) -> None:
        """Prepare credentials for `provider` and open the adapter."""
        if self.client is not None:
            await self.client.close()
            self.client = None
        self.provider_name = provider
        self.adapter = None
        self.conversation_id = None
        self.catalog = None
        self.model = None
        self.effort = None

        cookies = parse_cookies(cookies_path, provider)
        if provider == "chatgpt":
            cred = await asyncio.to_thread(
                prepare_chatgpt_session, cookies, self.state_dir
            )
        elif provider == "grok":
            cred = prepare_grok_session(cookies, self.state_dir)
        else:
            cred = prepare_claude_session(cookies, self.state_dir)
        self.credential_file = cred

        self.adapter = create_provider(provider, credential_file=cred)
        self.client = ProviderClient()
        self.client.add(self.adapter)
        await self.adapter.prepare()

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None

    # ---- models --------------------------------------------------------

    async def refresh_models(self) -> list:
        if self.adapter is None:
            return []
        self.catalog = await self.adapter.models()
        return list(self.catalog.chat_models) if self.catalog else []

    async def set_model(self, name: str | None) -> None:
        models = await self.refresh_models()
        if not name:
            self.model = None
            return
        lowered = name.lower()
        match = next(
            (m for m in models if m.id.lower() == lowered or m.title.lower() == lowered),
            None,
        )
        if match is None:
            match = next(
                (m for m in models if lowered in m.id.lower() or lowered in m.title.lower()),
                None,
            )
        if match is None:
            raise ValueError(f"unknown model '{name}'")
        self.model = match.id

    # ---- chat ----------------------------------------------------------

    async def send_stream(self, prompt: str, attachments: tuple[dict, ...] = ()):
        """Yield provider events for one turn."""
        if self.adapter is None or self.client is None:
            raise RuntimeError("not connected")
        request = SendRequest(
            provider_id=self.provider_name,
            account_id="default",
            chat_id=self.conversation_id or "new",
            prompt=prompt,
            conversation_id=self.conversation_id,
            model=self.model,
            effort=self.effort,
            temporary=self.temporary,
            attachments=attachments,
        )
        async for event in self.client.stream(request):
            if isinstance(event, ConversationBound):
                self.conversation_id = event.conversation_id
            yield event

    async def list_chats(self, limit: int = 30):
        if self.adapter is None:
            return []
        page = await self.adapter.list_chats(limit=limit)
        return list(page.items)

    async def get_chat(self, conversation_id: str):
        if self.adapter is None:
            return None
        return await self.adapter.get_chat(conversation_id)

    async def upload(self, path) -> dict:
        if self.adapter is None:
            raise RuntimeError("not connected")
        return await self.adapter.upload_file(str(path))

    async def delete_remote_chat(self, conversation_id: str | None = None) -> None:
        cid = conversation_id or self.conversation_id
        if self.adapter is not None and cid:
            try:
                await self.adapter.delete_chat(cid)
            except Exception:
                pass

    # ---- local persistence ---------------------------------------------

    def save_chat(self, name: str) -> str | None:
        return save_entry(name, self.provider_name, self.conversation_id, self.model)

    def resume_chat(self, name: str) -> dict | None:
        return resume_entry(name)
