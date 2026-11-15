"""Textual TUI chat client for ChatGPT + Claude via webai-provider."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import sys
import time
from pathlib import Path

from bidi.algorithm import get_display
from textual import events, on
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import Footer, Header, Markdown, Static, TextArea

from .client import ChatSession, load_saved, PROVIDERS, DEFAULT_PROVIDER
from .claude_backend import ClaudeWebSession
from .clipboard import copy_to_clipboard

SESSION_CLASSES = {"chatgpt": ChatSession, "claude": ClaudeWebSession}

COOKIE_DOMAIN_FILES = {
    "chatgpt": "chatgpt.com_cookies.txt",
    "claude": "claude.ai_cookies.txt",
}

# ChatGPT's generative-UI markup: capitalized components (<AsyncImage/>,
# <Entity/>, <Cite/>) plus lowercase DSL tags and {@...}/{#each ...} directives.
_GENUI_LOWER_TAGS = {
    "badge", "row", "box", "grid", "grid-item", "text", "button",
    "radio-group", "radio", "divider", "paper", "card", "modal", "tabs",
    "tab", "list", "list-item", "input", "select", "option", "form",
    "label", "img", "image", "link", "icon", "avatar", "chip", "tag",
    "tooltip", "popover", "dialog", "sheet", "stack", "hstack", "vstack",
    "spacer", "scroll", "table", "thead", "tbody", "tr", "td", "th",
}
# Attribute values may contain '>' inside {...} expressions or quotes
# (e.g. onClick={()=>...}), so the tag body allows those constructs.
_GENUI_TAG_RE = re.compile(
    r"<(/?)([A-Za-z][\w.-]*)"
    r"(?:\s+(?:[^>\"'{}]|\{[^}]*\}|\"[^\"]*\"|'[^']*')*)?"
    r"\s*/?>"
)
_GENUI_DIRECTIVE_RE = re.compile(r"\{[#@/][^}\n]*\}")
_GENUI_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z_$][\w$]*\}")


def _strip_genui(text: str) -> str:
    """Remove ChatGPT rich-UI markup, keeping the human-readable text."""
    def _tag_repl(m: re.Match) -> str:
        name = m.group(2)
        if name[0].isupper() or name.lower() in _GENUI_LOWER_TAGS:
            return ""
        return m.group(0)

    text = _GENUI_TAG_RE.sub(_tag_repl, text)
    text = _GENUI_DIRECTIVE_RE.sub("", text)
    text = _GENUI_PLACEHOLDER_RE.sub("", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip("\n")


HELP = """\
Commands:
  /help                  show this help
  /provider [name]       show or switch provider: chatgpt | claude
  /new                   start a new conversation
  /save [name]           persist current chat locally (~/.ai-cli/chats.json)
  /resume <name>         resume a saved chat (/chats lists them)
  /chats                 list saved local chats
  /model [name]          show or switch model (/models lists them)
  /models                list available models
  /effort [level]        show or set reasoning effort (if the model supports it)
  /cookies [path]        set the cookies export file for the current provider
  /history               list chats from the provider's server history
  /read <cid>            read a remote chat's history
  /resume-remote <cid>   continue a remote chat by id
  /tmp [on|off]          toggle temporary mode (not saved to provider history)
  /attach <paths>        attach files to the next message
  /lines <n>             max lines shown per reply (0 = unlimited)
  /copy                  copy all recent messages
  /print                 print all recent messages to terminal for selection
  /status                show session info
  /clear                 clear the screen (keeps the conversation)
  /exit, /quit           exit

Tips:
  - Enter sends. Shift+Enter (or Ctrl+J) inserts a newline — on terminals
    that can't tell them apart, a backslash followed quickly by Enter works too.
  - To copy text: click on the response text to select it, then press Ctrl+C.
  - If clipboard doesn't work: use /print to suspend TUI and print text
    to terminal for native selection (works in Azure Cloud Shell, SSH, etc).
  - Prefix a file path with @ (e.g. @report.pdf) or paste a bare path
    to attach it to your message automatically.
  - Press Ctrl+X while the model is replying to cancel the request.
  - Note: Hebrew/Arabic text may appear reversed depending on your terminal.
    This is a terminal limitation — WezTerm/kitty/iTerm handle BiDi correctly.
"""


class ChatInput(TextArea):
    """Multiline input: Enter submits, Shift+Enter / Ctrl+J inserts newline.

    Some terminals can't distinguish Shift+Enter and send a literal backslash
    followed by Enter instead (e.g. VSCode's built-in terminal) — that pair is
    detected by timestamp and converted into a newline as well.
    """

    _BACKSLASH_ENTER_GAP = 0.15

    class Submitted(Message):
        def __init__(self, input: "ChatInput", value: str) -> None:
            super().__init__()
            self.input = input
            self.value = value

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._backslash_time: float | None = None

    def action_insert_newline(self) -> None:
        self.insert("\n")

    async def _on_key(self, event: events.Key) -> None:
        now = time.monotonic()
        if event.key == "enter":
            event.stop()
            event.prevent_default()
            # Terminal workaround: '\\' + Enter (emitted for Shift+Enter)
            if (
                self._backslash_time is not None
                and now - self._backslash_time <= self._BACKSLASH_ENTER_GAP
                and self.text.endswith("\\")
            ):
                self._backslash_time = None
                self.action_delete_left()  # remove the stray backslash
                self.action_insert_newline()
                return
            self._backslash_time = None
            text = self.text.strip()
            if text:
                self.post_message(self.Submitted(input=self, value=text))
            return
        if event.key == "\\":
            self._backslash_time = now
        if event.key in ("shift+enter", "ctrl+j", "alt+enter"):
            event.stop()
            event.prevent_default()
            self.action_insert_newline()
            return
        await super()._on_key(event)


class ChatTUI(App):
    TITLE = "AI CLI"
    ALLOW_SELECT = True
    CSS = """
    #chat { height: 1fr; }
    #status { height: 1; padding: 0 1; color: $text-muted; }
    #input { dock: bottom; height: auto; max-height: 40%; min-height: 3; }
    .msg-user { margin: 1 0 0 0; }
    .msg-user > .lbl { color: $accent; text-style: bold; }
    .msg-ai > .lbl { color: $success; text-style: bold; }
    .msg-meta { color: $text-muted; height: auto; }
    .msg-err { color: $error; }
    """
    BINDINGS = [("ctrl+x", "cancel_worker", "Cancel")]

    def __init__(self, cookies_dir: str | None = None, provider: str = DEFAULT_PROVIDER, **kwargs):
        super().__init__(**kwargs)
        self.session = SESSION_CLASSES[provider if provider in SESSION_CLASSES else DEFAULT_PROVIDER]()
        self.cookies_dir = Path(cookies_dir) if cookies_dir else None
        self.provider = provider if provider in PROVIDERS else DEFAULT_PROVIDER
        self.pending_uploads: list[dict] = []
        self.pending_names: list[str] = []
        self.max_lines = 0
        self.busy = False
        self._worker = None
        self._last_stream_update = 0.0
        self.last_response = ""
        self.recent_messages: list[str] = []
        self._thoughts = ""

    # ---- layout -----------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="chat"):
            yield Static("Connecting...", id="boot")
        yield Static("", id="status")
        yield ChatInput(id="input")
        yield Footer()

    async def on_mount(self) -> None:
        ta = self.query_one("#input", ChatInput)
        ta.soft_wrap = True
        ta.show_line_numbers = False
        ta.placeholder = "Message... (Enter=send, Shift+Enter=newline)"
        ta.focus()
        await self._init_session()

    def _cookies_candidates(self, provider: str) -> list[Path]:
        domain_file = COOKIE_DOMAIN_FILES[provider]
        env_name = f"AI_CLI_{provider.upper()}_COOKIES"
        candidates = [
            os.environ.get(env_name),
            os.environ.get("AI_CLI_COOKIES"),
            str(self.cookies_dir / domain_file) if self.cookies_dir else None,
            domain_file,  # current directory
            str(self.session.state_dir / domain_file),
            str(Path.home() / ".ai-cli" / domain_file),
        ]
        return [Path(c) for c in candidates if c and Path(c).is_file()]

    async def _connect_provider(self, provider: str, cookies_path: Path | None = None) -> bool:
        path = cookies_path or next(iter(self._cookies_candidates(provider)), None)
        if path is None:
            await self._say(
                f"No cookies file found for '{provider}'. Looked for:\n"
                + "\n".join(f"  - {c}" for c in self._cookies_candidates(provider))
                + f"\nExport cookies and pass --cookies <path>, set {provider.upper()}_COOKIES, "
                "or use /cookies <path>.",
                error=True,
            )
            return False
        # swap backend object when switching providers
        if not isinstance(self.session, SESSION_CLASSES[provider]):
            try:
                await self.session.close()
            except Exception:
                pass
            self.session = SESSION_CLASSES[provider]()
        try:
            await self.session.connect(provider, path)
        except Exception as e:
            await self._say(f"Failed to connect to {provider}: {e}", error=True)
            return False
        return True

    async def _init_session(self) -> None:
        if not await self._connect_provider(self.provider):
            self._set_status("not connected — set cookies and /provider")
            return
        await self._post_connect()

    async def _post_connect(self) -> None:
        boot = self.query_one("#boot", Static)
        if boot:
            await boot.remove()
        try:
            models = await self.session.refresh_models()
        except Exception:
            models = []
        self._set_status(
            f"{self.session.provider_name} | model: {self.session.model or 'default'} | "
            f"models: {len(models)} | /help"
        )
        self.notify(f"Connected to {self.session.provider_name}")

    # ---- helpers ----------------------------------------------------

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)
        self.recent_messages.append(f"[STATUS] {text}")
        if len(self.recent_messages) > 100:
            self.recent_messages = self.recent_messages[-100:]

    async def _chat_area(self) -> VerticalScroll:
        return self.query_one("#chat", VerticalScroll)

    async def _say(self, text: str, *, error: bool = False, cls: str = "") -> None:
        area = await self._chat_area()
        style = "msg-err" if error else ""
        await area.mount(
            Static(self._bidi(text), classes=f"msg-meta {style} {cls}".strip())
        )
        area.scroll_end(animate=False)
        self.recent_messages.append(text)
        if len(self.recent_messages) > 100:
            self.recent_messages = self.recent_messages[-100:]

    async def _say_user(self, text: str) -> None:
        area = await self._chat_area()
        await area.mount(
            Static(f"\nYou:\n{self._bidi(text)}", classes="msg-user")
        )
        area.scroll_end(animate=False)
        self.recent_messages.append(f"You: {text}")
        if len(self.recent_messages) > 100:
            self.recent_messages = self.recent_messages[-100:]

    async def _say_ai_start(self) -> Markdown:
        area = await self._chat_area()
        md = Markdown("", classes="msg-ai")
        await area.mount(md)
        area.scroll_end(animate=False)
        return md

    def _truncate_lines(self, text: str) -> str:
        if self.max_lines <= 0:
            return text
        lines = text.splitlines()
        if len(lines) <= self.max_lines:
            return text
        return "\n".join(lines[: self.max_lines]) + (
            f"\n\n... ({len(lines) - self.max_lines} more lines — "
            f"raise with /lines {self.max_lines * 2} or set 0)"
        )

    def _bidi(self, text: str) -> str:
        """Apply Unicode Bidirectional Algorithm for correct RTL display."""
        try:
            return get_display(text)
        except Exception:
            return text

    # ---- input handling ----------------------------------------------

    @on(ChatInput.Submitted)
    async def on_submit(self, event: ChatInput.Submitted) -> None:
        raw = event.value.strip()
        event.input.clear()
        if not raw:
            return
        if raw.startswith("/"):
            await self._handle_command(raw)
        else:
            await self._send_message(raw)

    async def _extract_files(self, text: str) -> tuple[str, list[str]]:
        """Pull @path tokens and bare existing paths out of the message."""
        kept: list[str] = []
        for token in shlex.split(text, posix=False):
            candidate = token[1:] if token.startswith("@") else token
            candidate = candidate.strip("'\"`")
            path = Path(candidate).expanduser()
            if path.is_file():
                self.pending_names.append(path.name)
            else:
                kept.append(token)
        return " ".join(kept), list(self.pending_names)

    # ---- commands ----------------------------------------------------

    async def _handle_command(self, raw: str) -> None:
        parts = raw[1:].split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("exit", "quit"):
            self.exit()
        elif cmd == "help":
            await self._say(HELP)
        elif cmd == "clear":
            area = await self._chat_area()
            await area.remove_children()
        elif cmd == "status":
            await self._say(
                f"provider: {self.session.provider_name}\n"
                f"model: {self.session.model or 'default'}\n"
                f"effort: {self.session.effort or 'default'}\n"
                f"conversation: {self.session.conversation_id or '(new)'}\n"
                f"temporary: {self.session.temporary}\n"
                f"max lines: {self.max_lines or 'unlimited'}\n"
                f"pending uploads: {len(self.pending_uploads)}"
            )
        elif cmd == "provider":
            if not arg:
                await self._say(f"Current provider: {self.session.provider_name}  (chatgpt | claude)")
                return
            name = arg.lower()
            if name not in PROVIDERS:
                await self._say(f"Unknown provider '{name}'. Use chatgpt or claude.", error=True)
                return
            if name == self.session.provider_name and self.session.adapter is not None:
                await self._say(f"Already on '{name}'.")
                return
            self.provider = name
            if await self._connect_provider(name):
                self._thoughts = ""
                await self._post_connect()
                await self._say(f"Switched to {name}.")
        elif cmd == "new":
            if hasattr(self.session, "new_chat"):
                self.session.new_chat()
            else:
                self.session.conversation_id = None
            self._thoughts = ""
            await self._say("Started a new conversation.")
        elif cmd == "tmp":
            if arg in ("on", "off"):
                self.session.temporary = arg == "on"
            else:
                self.session.temporary = not self.session.temporary
            await self._say(f"Temporary mode: {'ON' if self.session.temporary else 'OFF'}")
        elif cmd == "lines":
            try:
                self.max_lines = max(0, int(arg or "0"))
            except ValueError:
                await self._say("Usage: /lines <n>", error=True)
                return
            await self._say(f"Max reply lines: {self.max_lines or 'unlimited'}")
        elif cmd == "copy":
            if not self.recent_messages:
                await self._say("Nothing to copy yet.", error=True)
            else:
                text = "\n\n".join(self.recent_messages)
                success, method = copy_to_clipboard(text)
                if success:
                    await self._say(f"Copied all recent messages via {method}.")
                else:
                    await self._print_to_terminal()
        elif cmd == "print":
            await self._print_to_terminal()
        elif cmd == "attach":
            if not arg:
                await self._say("Usage: /attach <path> [path...]", error=True)
                return
            ok, bad = [], []
            for p in shlex.split(arg):
                fp = Path(p).expanduser()
                (ok if fp.is_file() else bad).append(str(fp))
            if ok:
                self._say(f"Queued {len(ok)} file(s) for upload on next message.")
                # uploads happen lazily in _send_message via session.upload
                for f in ok:
                    self.pending_names.append(Path(f).name)
                    self.pending_uploads.append({"_path": f})
            if bad:
                await self._say(f"Not found: {', '.join(bad)}", error=True)
        elif cmd == "save":
            name = arg or "last"
            cid = self.session.save_chat(name)
            await self._say(f"Chat saved as '{name}' (cid: {cid or 'new'})")
        elif cmd == "chats":
            saved = load_saved()
            if not saved:
                await self._say("No saved chats. Use /save <name>.")
            else:
                lines = [
                    f"- {n}  ({v.get('provider','?')}, cid: {(v.get('conversation_id') or '?')[:12]})"
                    for n, v in saved.items()
                ]
                await self._say("Saved chats:\n" + "\n".join(lines))
        elif cmd == "resume":
            if not arg:
                await self._say("Usage: /resume <name>", error=True)
                return
            entry = self.session.resume_chat(arg)
            if not entry:
                await self._say(f"No saved chat named '{arg}'.", error=True)
                return
            provider = entry.get("provider", DEFAULT_PROVIDER)
            if provider != self.session.provider_name:
                if await self._connect_provider(provider):
                    await self._post_connect()
            self.session.conversation_id = entry.get("conversation_id")
            if entry.get("model"):
                self.session.model = entry["model"]
            await self._say(f"Resumed '{arg}' on {provider}.")
        elif cmd == "model":
            if not arg:
                await self._say(f"Current model: {self.session.model or 'default'}")
                return
            try:
                await self.session.set_model(arg)
                await self._say(f"Model set to '{self.session.model}'.")
            except Exception as e:
                await self._say(str(e), error=True)
        elif cmd == "models":
            try:
                models = await self.session.refresh_models()
                lines = [f"- {m.id}  ({m.title})" for m in models]
                await self._say("\n".join(lines) or "No models discovered yet.")
            except Exception as e:
                await self._say(f"Could not list models: {e}", error=True)
        elif cmd == "effort":
            if not arg:
                await self._say(f"Current effort: {self.session.effort or 'default'}")
                return
            self.session.effort = arg
            await self._say(f"Effort set to '{arg}' (applies if the model supports it).")
        elif cmd == "cookies":
            path = Path(arg).expanduser() if arg else None
            if path is None:
                candidates = self._cookies_candidates(self.session.provider_name)
                await self._say(
                    "Usage: /cookies <path-to-export-file>\n"
                    f"Current provider: {self.session.provider_name}\n"
                    "Found files:\n" + "\n".join(f"  - {c}" for c in candidates)
                )
                return
            if await self._connect_provider(self.session.provider_name, path):
                await self._post_connect()
                await self._say("Cookies updated — reconnected.")
        elif cmd == "history":
            try:
                chats = await self.session.list_chats()
            except Exception as e:
                await self._say(f"Could not list history: {e}", error=True)
                return
            if not chats:
                await self._say("No chats found in provider history.")
            else:
                lines = [
                    f"- {(c.origin.remote_id if hasattr(c, 'origin') else getattr(c, 'id', '?'))[:20]}... | {getattr(c, 'title', '(untitled)')}"
                    for c in chats[:20]
                ]
                await self._say(f"Remote chats ({len(chats)}):\n" + "\n".join(lines))
        elif cmd == "read":
            if not arg:
                await self._say("Usage: /read <cid>", error=True)
                return
            try:
                chat = await self.session.get_chat(arg)
            except Exception as e:
                await self._say(f"Could not read chat: {e}", error=True)
                return
            if not chat:
                await self._say(f"Chat '{arg}' not found.", error=True)
                return
            for msg in list(getattr(chat, "messages", ()) or ())[-10:]:
                role = "You" if getattr(msg, "role", "") == "user" else "AI"
                text = " ".join(
                    c.text for c in getattr(msg, "content", ()) or () if getattr(c, "text", None)
                )
                await self._say(f"{role}:\n{text[:500]}")
        elif cmd == "resume-remote":
            if not arg:
                await self._say("Usage: /resume-remote <cid>", error=True)
                return
            self.session.conversation_id = arg
            await self._say(f"Now continuing remote chat {arg[:20]}...")
        else:
            await self._say(f"Unknown command '/{cmd}'. Try /help.", error=True)

    # ---- messaging ----------------------------------------------------

    async def _send_message(self, raw: str) -> None:
        if self.busy:
            await self._say("Model is still replying — press Ctrl+X to cancel.", error=True)
            return
        if self.session.adapter is None:
            await self._say("Not connected. Set cookies and /provider first.", error=True)
            return
        prompt, names = await self._extract_files(raw)
        attachments: list[dict] = []
        if self.pending_uploads:
            for item in self.pending_uploads:
                path = item.get("_path")
                if not path:
                    continue
                try:
                    attachments.append(await self.session.upload(path))
                except Exception as e:
                    await self._say(f"Upload failed for {path}: {e}", error=True)
            self.pending_uploads.clear()
            self.pending_names.clear()
        if not prompt and not attachments:
            return
        display = raw if not names else f"{prompt}\n[files: {', '.join(names)}]"
        await self._say_user(display)
        self.busy = True
        self._set_status(f"{self.session.provider_name} is thinking...  (Ctrl+X to cancel)")
        md = await self._say_ai_start()
        self._worker = self.run_worker(
            self._stream(md, prompt, tuple(attachments)), group="chat", exclusive=True
        )

    async def _stream(self, md: Markdown, prompt: str, attachments: tuple[dict, ...]) -> None:
        full = ""
        thoughts = ""
        metadata = None
        try:
            async for event in self.session.send_stream(prompt or "Describe the attached files.", attachments):
                from webai_provider import (
                    ActionBegin, ActionDelta, ActionEnd,
                    ConversationBound, MessageMetadata,
                    ReplaceText, ResponseComplete, TextDelta, ThoughtDelta,
                )
                if isinstance(event, TextDelta):
                    if event.channel == "analysis":
                        thoughts += event.text
                    else:
                        full += event.text
                elif isinstance(event, ReplaceText):
                    if event.channel == "analysis":
                        thoughts = event.text
                    else:
                        full = event.text
                elif isinstance(event, ThoughtDelta):
                    thoughts += event.text
                elif isinstance(event, (ActionBegin, ActionDelta, ActionEnd)):
                    pass  # tool activity — could be surfaced later
                elif isinstance(event, MessageMetadata):
                    metadata = event
                elif isinstance(event, ConversationBound):
                    pass
                elif isinstance(event, ResponseComplete):
                    pass
                now = time.monotonic()
                if now - self._last_stream_update > 0.08:
                    self._last_stream_update = now
                    shown = self._truncate_lines(_strip_genui(full))
                    md.update(self._bidi(shown) or "...")
            self.last_response = _strip_genui(full)
            final = self._truncate_lines(self.last_response)
            md.update(self._bidi(final) or "_(empty response)_")
            self.recent_messages.append(f"{self.session.provider_name}: {self.last_response}")
            if thoughts:
                msg = f"_thinking:_\n{thoughts[:2000]}"
                await self._say(msg, cls="msg-meta")
            if metadata is not None:
                files = getattr(metadata, "files", ()) or ()
                if files:
                    names = [f.get("name", f.get("id", "?")) if isinstance(f, dict) else str(f) for f in files]
                    await self._say(f"Files: {', '.join(names)}")
        except asyncio.CancelledError:
            md.update((full + "\n\n_(cancelled)_") if full else "_(cancelled)_")
            raise
        except Exception as e:
            md.update(f"**Error:** {e}")
            await self._say(str(e), error=True)
        finally:
            self.busy = False
            self._set_status(
                f"{self.session.provider_name} | model: {self.session.model or 'default'} | "
                f"cid: {(self.session.conversation_id or 'new')[:12]} | ready"
            )

    # ---- actions ------------------------------------------------------

    async def _print_to_terminal(self) -> None:
        """Suspend TUI and print all recent messages to terminal for selection."""
        if not self.recent_messages:
            await self._say("Nothing to print.", error=True)
            return
        try:
            with self.suspend():
                print("\n" + "=" * 60)
                print("RECENT MESSAGES (select with mouse, then copy):")
                print("=" * 60)
                for msg in self.recent_messages:
                    print(msg)
                print("=" * 60)
                print("Press Enter to return...")
                input()
        except Exception as e:
            await self._say(f"Failed to print: {e}", error=True)

    def action_cancel_worker(self) -> None:
        if self._worker and self.busy:
            self._worker.cancel()
            self.notify("Cancelled")

    def on_unmount(self) -> None:
        try:
            asyncio.get_running_loop().create_task(self.session.close())
        except Exception:
            pass


def run() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="ai-cli")
    parser.add_argument(
        "--cookies",
        default=None,
        help="Path to cookies export file for the initial provider "
        "(Netscape cookies.txt or JSON; e.g. chatgpt.com_cookies.txt)",
    )
    parser.add_argument(
        "--provider",
        default=DEFAULT_PROVIDER,
        choices=PROVIDERS,
        help="Initial provider (default: chatgpt)",
    )
    args = parser.parse_args()

    ChatTUI(cookies_dir=args.cookies, provider=args.provider).run()


if __name__ == "__main__":
    run()
