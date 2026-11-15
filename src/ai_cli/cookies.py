"""Parse browser cookie exports (Netscape cookies.txt / JSON) into dicts.

Also prepares the per-provider session files that webai-provider expects:

- chatgpt: a verified session snapshot produced by BrowserSession.import_cookies
  (requires one network call to chatgpt.com/api/auth/session).
- claude:  a flat JSON object of cookies; sessionKey + lastActiveOrg are the
  cookies the provider actually needs.
"""

from __future__ import annotations

import json
import time
from pathlib import Path


class CookieError(Exception):
    pass


PROVIDER_DOMAINS = {
    "chatgpt": ("chatgpt.com",),
    "claude": ("claude.ai",),
}

PROVIDER_REQUIRED = {
    "chatgpt": "__Secure-next-auth.session-token",
    "claude": "sessionKey",
}


def _domain_matches(domain: str, suffixes: tuple[str, ...]) -> bool:
    d = domain.lstrip(".").lower()
    return any(d == s or d.endswith("." + s) for s in suffixes)


def _iter_cookie_rows(text: str):
    """Yield (domain, name, value) tuples from Netscape or JSON text."""
    stripped = text.lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise CookieError(f"Could not parse cookies file as JSON: {e}") from e
        if isinstance(data, dict):
            for key in ("cookies", "data", "items"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
            else:
                data = [data]
        if not isinstance(data, list):
            raise CookieError("Unexpected JSON cookies format")
        for item in data:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("Name")
            value = item.get("value") or item.get("Value")
            domain = item.get("domain") or item.get("Domain") or ""
            if name and isinstance(value, str):
                yield str(domain), str(name), value
        return

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, _path, _secure, expires, name, value = parts[:7]
        try:
            exp = float(expires)
        except ValueError:
            exp = 0
        if exp > 0 and exp <= time.time():
            continue
        yield domain, name, value


def parse_cookies(path: str | Path, provider: str) -> dict[str, str]:
    """Return {name: value} for cookies belonging to the provider's domain."""
    path = Path(path)
    if not path.is_file():
        raise CookieError(f"Cookies file not found: {path}")
    suffixes = PROVIDER_DOMAINS[provider]
    cookies: dict[str, str] = {}
    for domain, name, value in _iter_cookie_rows(path.read_text(encoding="utf-8", errors="replace")):
        if _domain_matches(domain, suffixes):
            cookies[name] = value
    required = PROVIDER_REQUIRED[provider]
    if provider == "chatgpt":
        # session token is chunked (.0, .1, ...) — accept any chunk
        if not any(name == required or name.startswith(required + ".") for name in cookies):
            raise CookieError(
                f"'{required}' not found in {path}. "
                "Log in to https://chatgpt.com and export cookies for chatgpt.com."
            )
    elif required not in cookies:
        raise CookieError(
            f"'{required}' not found in {path}. "
            "Log in to https://claude.ai and export cookies for claude.ai."
        )
    return cookies


def prepare_chatgpt_session(cookies: dict[str, str], workdir: Path) -> Path:
    """Verify cookies against chatgpt.com and write the session snapshot."""
    from webai_provider import BrowserSession, SessionPaths

    session_file = workdir / "chatgpt_session.json"
    profile_dir = workdir / "chatgpt_profile"
    session = BrowserSession(SessionPaths(session_file, profile_dir))
    session.import_cookies(cookies)
    return session_file


def prepare_claude_session(cookies: dict[str, str], workdir: Path) -> Path:
    """Write the flat cookie dict webai-provider's HttpSession expects."""
    workdir.mkdir(parents=True, exist_ok=True)
    session_file = workdir / "claude_session.json"
    session_file.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")
    try:
        session_file.chmod(0o600)
    except OSError:
        pass
    return session_file
