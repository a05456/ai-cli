# ai-cli

A full terminal chat client for **ChatGPT** and **Claude** web apps (not the
official API) with a [Textual](https://textual.textualize.io/) TUI —
streaming replies, markdown rendering, file attachments, model switching,
and persistent chats.

Backends:
- **ChatGPT** — [`webai-provider`](https://pypi.org/project/webai-provider/)
- **Claude** — [`claude-webapi`](https://pypi.org/project/claude-webapi/)
  (aiohttp-based; chosen because `webai-provider`'s `curl_cffi` transport gets
  Cloudflare-challenged with HTTP 403 on claude.ai)

## Install

Requires **Python 3.11+**.

```sh
git clone <this-repo>
cd ai-cli
pip install -e .
```

## Cookies

Each provider needs a browser cookie export:

- **ChatGPT**: log in to <https://chatgpt.com>, export cookies for
  `chatgpt.com` (Netscape `cookies.txt` or JSON). Must contain
  `__Secure-next-auth.session-token` (chunked `.0`/`.1` is fine).
- **Claude**: log in to <https://claude.ai>, export cookies for `claude.ai`.
  Must contain `sessionKey` (and ideally `lastActiveOrg`).

Place the files next to where you run the CLI with the conventional names:

```
chatgpt.com_cookies.txt
claude.ai_cookies.txt
```

…or point at them explicitly:

```sh
ai-cli --provider chatgpt --cookies /path/to/chatgpt.com_cookies.txt
# or per-provider env vars:
export AI_CLI_CHATGPT_COOKIES=/path/to/chatgpt.com_cookies.txt
export AI_CLI_CLAUDE_COOKIES=/path/to/claude.ai_cookies.txt
```

Inside the app, `/cookies <path>` re-points the current provider at a file and
reconnects. `/provider chatgpt|claude` switches providers (each keeps its own
session under `~/.ai-cli/`).

ChatGPT cookies are verified once against `chatgpt.com/api/auth/session` and a
session snapshot is cached in `~/.ai-cli/chatgpt_session.json`.

## Usage

```sh
ai-cli                      # chatgpt by default
ai-cli --provider claude
```

Type a message and press **Enter**. Replies stream as markdown.

### Commands

| Command | Description |
| --- | --- |
| `/provider [name]` | Show or switch provider (`chatgpt`, `claude`) |
| `/new` | Start a new conversation |
| `/save [name]` | Persist the current chat locally (`~/.ai-cli/chats.json`) |
| `/resume <name>` | Resume a saved chat (`/chats` lists them) |
| `/model [name]` | Show or switch the model (`/models` lists them) |
| `/effort [level]` | Set reasoning effort (where supported) |
| `/cookies [path]` | Set the cookies file for the current provider |
| `/history` | List chats from the provider's server history |
| `/read <cid>` | Read a remote chat's history |
| `/resume-remote <cid>` | Continue a remote chat by id |
| `/tmp [on\|off]` | Toggle temporary mode |
| `/attach <paths>` | Attach files to the next message |
| `/lines <n>` | Limit displayed reply lines (`0` = unlimited) |
| `/copy` | Copy all recent messages |
| `/print` | Print all recent messages to terminal for selection |
| `/status` | Show session info |
| `/clear` | Clear the screen, keep the conversation |
| `/exit` | Quit |

### Attaching files

- `/attach report.pdf image.png`
- `@report.pdf` inline in your message
- paste a bare existing file path in your message

Files are uploaded via the provider's `upload_file` when the next message is
sent.

### Keyboard

- **Enter** — send message
- **Shift+Enter** or **Ctrl+J** — insert newline
- **Ctrl+C** — copy selected text (click on response first)
- **Ctrl+X** — cancel the in-flight request

### Copying text

- **Click** on the response text to select it, then **Ctrl+C**
- `/copy` — copy recent messages (pyperclip → OSC 52 → `/print` fallback)
- `/print` — suspend TUI and print to terminal for native selection
  (works over SSH / Azure Cloud Shell)

## Project layout

```
src/ai_cli/
  cookies.py        # Netscape + JSON cookie export parser + session prep
  client.py         # ChatGPT session wrapper around webai-provider
  claude_backend.py # Claude session wrapper around claude-webapi
  clipboard.py      # OSC 52 clipboard with tmux/screen/SSH handling
  app.py            # Textual TUI with BiDi support
```

## BiDi (Hebrew/Arabic) support

The app applies the Unicode Bidirectional Algorithm (UAX #9) via
`python-bidi` to all displayed text.

## Corporate proxy / TLS inspection (e.g. NetFree)

If your network intercepts TLS (Israeli NetFree, Zscaler, corporate proxies),
Python's HTTP clients will fail certificate verification. Export the
intercepting root CA from the Windows cert store, append it to a PEM bundle,
and point both env vars at it:

```powershell
$env:CURL_CA_BUNDLE = "C:\path\to\bundle.pem"   # certifi roots + NetFree root
$env:SSL_CERT_FILE  = "C:\path\to\bundle.pem"
```

Note: NetFree may also block `claude.ai` API calls entirely until the site is
approved in the filter — in that case Claude won't work from that network.

## Disclaimer

This tool uses unofficial web sessions. It is not affiliated with OpenAI or
Anthropic. Use responsibly and at your own risk — sessions can expire or be
revoked, and automated access may violate provider terms of service.
