#!/usr/bin/env python3
"""MCP server that wraps CDP (Chrome DevTools Protocol) access to a remote
Android phone's Chrome browser.

Chain: phone Termux -> adb forward -> SSH reverse tunnel -> relay VPS
port 9333 -> this server's local SSH forward on 127.0.0.1:9333.

Protocol: hand-rolled JSON-RPC 2.0 over stdio using only the stdlib plus the
``websockets`` package -- no framework dependency, so it runs anywhere
Python 3.10+ does.

Configuration (environment variables):
    PHONE_CHROME_SSH_HOST   relay VPS host (required)
    PHONE_CHROME_SSH_USER   relay VPS ssh user (default: root)
    PHONE_CHROME_LOCAL_PORT local/remote CDP relay port (default: 9333)
    PHONE_CHROME_DPR        device pixel ratio of the phone (default: 3.5)
    PHONE_CHROME_TOP_OFFSET physical-pixel offset of status bar + Chrome
                            toolbar, used for CSS->physical tap coordinate
                            conversion (default: 490; measure your own phone)

Safety note: ``phone_tap`` never executes a tap itself. It only returns the
``adb shell input tap`` command string for a human to run in Termux. Payment
and other trust-sensitive screens must be confirmed by a real physical touch
event, not a CDP-synthesized one; this tool intentionally does not attempt to
automate that step.
"""


from __future__ import annotations

import asyncio
import itertools
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from typing import Any

import websockets
import websockets.exceptions

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

SSH_HOST = os.environ.get("PHONE_CHROME_SSH_HOST", "")
SSH_USER = os.environ.get("PHONE_CHROME_SSH_USER", "root")
LOCAL_CDP_HOST = "127.0.0.1"
LOCAL_CDP_PORT = int(os.environ.get("PHONE_CHROME_LOCAL_PORT", "9333"))
CDP_BASE_URL = f"http://{LOCAL_CDP_HOST}:{LOCAL_CDP_PORT}"

WS_TIMEOUT = 15.0          # seconds, per CDP websocket round trip
PAGE_LOAD_TIMEOUT = 30.0   # seconds, for phone_navigate to settle
TUNNEL_START_TIMEOUT = 15.0
SCROLL_SETTLE_DELAY = 0.5  # seconds, matches the verified click recipe
DEFAULT_READ_CHARS = 3000

# Phone screen parameters. Defaults fit a 1240x2772 @ DPR 3.5 device;
# measure your own phone once and override via environment variables.
PHONE_DPR = float(os.environ.get("PHONE_CHROME_DPR", "3.5"))
PHONE_TOP_OFFSET_PHYSICAL = int(os.environ.get("PHONE_CHROME_TOP_OFFSET", "490"))

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "phone-chrome-mcp"
SERVER_VERSION = "1.0.0"

_msg_id_counter = itertools.count(1)


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class PhoneToolError(Exception):
    """User-facing tool error. Message is safe to show as isError content."""


class CDPProtocolError(PhoneToolError):
    """CDP returned an error object for a command."""


# --------------------------------------------------------------------------
# SSH relay tunnel management
# --------------------------------------------------------------------------


def _port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


async def ensure_tunnel() -> dict[str, Any]:
    """Make sure local 9333 is forwarded to the phone's Chrome via the
    relay VPS. Starts the SSH tunnel if it is not already up.
    Idempotent and safe to call before every operation (self-heals if the
    tunnel died between tool calls).
    """
    if not SSH_HOST:
        raise PhoneToolError(
            "PHONE_CHROME_SSH_HOST is not set; export it to your relay VPS host first"
        )
    loop = asyncio.get_running_loop()
    already = await loop.run_in_executor(None, _port_open, LOCAL_CDP_HOST, LOCAL_CDP_PORT)
    if already:
        return {"tunnel": "already_up"}

    cmd = [
        "ssh",
        "-f",
        "-N",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-L", f"{LOCAL_CDP_PORT}:127.0.0.1:{LOCAL_CDP_PORT}",
        f"{SSH_USER}@{SSH_HOST}",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise PhoneToolError(f"ssh binary not found: {exc}") from exc

    try:
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=TUNNEL_START_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise PhoneToolError(
            f"ssh -f -N tunnel setup timed out after {TUNNEL_START_TIMEOUT}s "
            "(no response from relay VPS?)"
        )

    if proc.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise PhoneToolError(f"ssh tunnel exited {proc.returncode}: {detail or 'no stderr'}")

    for _ in range(10):
        if await loop.run_in_executor(None, _port_open, LOCAL_CDP_HOST, LOCAL_CDP_PORT):
            return {"tunnel": "started"}
        await asyncio.sleep(0.3)

    raise PhoneToolError(
        "ssh reported success but local port 9333 never opened; "
        "the phone side (Termux adb forward / reverse tunnel) may be down"
    )


# --------------------------------------------------------------------------
# CDP HTTP endpoints (/json/list)
# --------------------------------------------------------------------------


def _http_get_json(path: str, timeout: float = 10.0) -> Any:
    url = f"{CDP_BASE_URL}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ConnectionError, OSError, TimeoutError) as exc:
        # Covers connection refused, DNS/socket errors (URLError), and the
        # remote closing the socket after the tunnel is up but CDP isn't
        # actually serving on the other end (http.client.RemoteDisconnected,
        # a ConnectionError subclass that urllib does not itself wrap).
        raise PhoneToolError(f"CDP HTTP endpoint unreachable ({path}): {exc}") from exc
    except (TypeError, ValueError) as exc:
        raise PhoneToolError(f"CDP HTTP endpoint returned invalid JSON ({path}): {exc}") from exc


async def list_tabs() -> list[dict[str, str]]:
    await ensure_tunnel()
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _http_get_json, "/json/list")
    if not isinstance(data, list):
        raise PhoneToolError("CDP /json/list returned an unexpected shape")
    tabs = []
    for entry in data:
        if not isinstance(entry, dict) or entry.get("type") != "page":
            continue
        tabs.append(
            {
                "id": entry.get("id", ""),
                "title": entry.get("title", ""),
                "url": entry.get("url", ""),
            }
        )
    return tabs


async def _resolve_tab_id(tab_id: str | None) -> str:
    if tab_id:
        return tab_id
    tabs = await list_tabs()
    if not tabs:
        raise PhoneToolError("no open Chrome tabs on the phone")
    return tabs[0]["id"]


# --------------------------------------------------------------------------
# CDP websocket connection manager (persistent across tool calls)
# --------------------------------------------------------------------------


class CDPManager:
    def __init__(self) -> None:
        self._connections: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def _connect(self, tab_id: str):
        await ensure_tunnel()
        url = f"ws://{LOCAL_CDP_HOST}:{LOCAL_CDP_PORT}/devtools/page/{tab_id}"
        try:
            ws = await websockets.connect(
                url,
                open_timeout=WS_TIMEOUT,
                close_timeout=5,
                max_size=16 * 1024 * 1024,
            )
        except (OSError, websockets.exceptions.WebSocketException) as exc:
            raise PhoneToolError(f"could not open CDP websocket for tab {tab_id}: {exc}") from exc
        self._connections[tab_id] = ws
        return ws

    async def _get_ws(self, tab_id: str):
        ws = self._connections.get(tab_id)
        if ws is not None and ws.state is websockets.State.OPEN:
            return ws
        self._connections.pop(tab_id, None)
        return await self._connect(tab_id)

    async def send(
        self, tab_id: str, method: str, params: dict[str, Any] | None = None, timeout: float = WS_TIMEOUT
    ) -> dict[str, Any]:
        lock = self._locks[tab_id]
        async with lock:
            return await asyncio.wait_for(self._send_locked(tab_id, method, params), timeout=timeout)

    async def _send_locked(self, tab_id: str, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        msg_id = next(_msg_id_counter)
        payload = json.dumps({"id": msg_id, "method": method, "params": params or {}})

        async def _attempt(ws) -> dict[str, Any]:
            await ws.send(payload)
            while True:
                raw = await ws.recv()
                try:
                    data = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if data.get("id") == msg_id:
                    if "error" in data:
                        raise CDPProtocolError(f"{method} failed: {data['error']}")
                    return data.get("result", {})
                # else: an event notification for this tab; ignore and keep reading

        ws = await self._get_ws(tab_id)
        try:
            return await _attempt(ws)
        except (websockets.exceptions.ConnectionClosed, OSError):
            # auto-reconnect once on a dropped websocket
            self._connections.pop(tab_id, None)
            ws2 = await self._connect(tab_id)
            return await _attempt(ws2)

    async def close_all(self) -> None:
        for ws in list(self._connections.values()):
            try:
                await ws.close()
            except Exception:
                pass
        self._connections.clear()


# --------------------------------------------------------------------------
# CDP helpers
# --------------------------------------------------------------------------


async def evaluate(mgr: CDPManager, tab_id: str, expression: str, timeout: float = WS_TIMEOUT) -> Any:
    result = await mgr.send(
        tab_id,
        "Runtime.evaluate",
        {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        },
        timeout=timeout,
    )
    exc_details = result.get("exceptionDetails")
    if exc_details:
        text = exc_details.get("exception", {}).get("description") or exc_details.get("text") or str(exc_details)
        raise PhoneToolError(f"page threw during evaluate: {text}")
    return result.get("result", {}).get("value")


def _page_state_expr(max_chars: int, selector: str | None) -> str:
    return f"""
(() => {{
  const sel = {json.dumps(selector)};
  let el = null;
  if (sel) {{
    try {{ el = document.querySelector(sel); }} catch (e) {{ el = null; }}
  }} else {{
    el = document.body;
  }}
  const text = el ? (el.innerText || el.textContent || '') : '';
  return {{
    url: location.href,
    title: document.title,
    selector_matched: !!el,
    text: text.slice(0, {int(max_chars)})
  }};
}})()
"""


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------


async def tool_phone_connect(mgr: CDPManager, args: dict[str, Any]) -> dict[str, Any]:
    tunnel_status = await ensure_tunnel()
    tabs = await list_tabs()
    return {"ok": True, **tunnel_status, "tab_count": len(tabs), "tabs": tabs}


async def tool_phone_tabs(mgr: CDPManager, args: dict[str, Any]) -> dict[str, Any]:
    tabs = await list_tabs()
    return {"ok": True, "tab_count": len(tabs), "tabs": tabs}


async def tool_phone_read(mgr: CDPManager, args: dict[str, Any]) -> dict[str, Any]:
    tab_id = await _resolve_tab_id(args.get("tab_id"))
    selector = args.get("selector")
    max_chars = int(args.get("max_chars") or DEFAULT_READ_CHARS)
    value = await evaluate(mgr, tab_id, _page_state_expr(max_chars, selector))
    if not isinstance(value, dict):
        raise PhoneToolError("unexpected result reading page state")
    value["ok"] = True
    value["tab_id"] = tab_id
    return value


async def tool_phone_navigate(mgr: CDPManager, args: dict[str, Any]) -> dict[str, Any]:
    tab_id = await _resolve_tab_id(args.get("tab_id"))
    url = args.get("url")
    if not url or not isinstance(url, str):
        raise PhoneToolError("phone_navigate requires a non-empty 'url' string")

    await mgr.send(tab_id, "Page.navigate", {"url": url}, timeout=WS_TIMEOUT)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + PAGE_LOAD_TIMEOUT
    ready_state = None
    while loop.time() < deadline:
        await asyncio.sleep(0.5)
        try:
            ready_state = await evaluate(mgr, tab_id, "document.readyState")
        except PhoneToolError:
            # navigation destroyed the execution context; keep polling
            continue
        if ready_state == "complete":
            break

    preview = await evaluate(mgr, tab_id, _page_state_expr(DEFAULT_READ_CHARS, None))
    if not isinstance(preview, dict):
        preview = {}
    return {
        "ok": True,
        "tab_id": tab_id,
        "ready_state": ready_state,
        "settled": ready_state == "complete",
        "url": preview.get("url"),
        "title": preview.get("title"),
        "text": preview.get("text"),
    }


_FIND_ELEMENT_EXPR = """
(() => {{
  const target = {target};
  let el = null;
  try {{ el = document.querySelector(target); }} catch (e) {{ el = null; }}
  if (!el) {{
    const all = document.querySelectorAll('body *');
    for (const cand of all) {{
      if (
        cand.childElementCount <= 2 &&
        cand.textContent &&
        cand.textContent.trim() === target &&
        cand.offsetHeight > 0
      ) {{
        el = cand;
        break;
      }}
    }}
  }}
  if (!el) return null;
  el.scrollIntoView({{block: 'center', inline: 'center'}});
  return {{found: true, tag: el.tagName}};
}})()
"""

_ELEMENT_RECT_EXPR = """
(() => {{
  const target = {target};
  let el = null;
  try {{ el = document.querySelector(target); }} catch (e) {{ el = null; }}
  if (!el) {{
    const all = document.querySelectorAll('body *');
    for (const cand of all) {{
      if (
        cand.childElementCount <= 2 &&
        cand.textContent &&
        cand.textContent.trim() === target &&
        cand.offsetHeight > 0
      ) {{
        el = cand;
        break;
      }}
    }}
  }}
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return {{
    x: r.left + r.width / 2,
    y: r.top + r.height / 2,
    tag: el.tagName,
    text: (el.textContent || '').trim().slice(0, 80)
  }};
}})()
"""


async def tool_phone_click(mgr: CDPManager, args: dict[str, Any]) -> dict[str, Any]:
    tab_id = await _resolve_tab_id(args.get("tab_id"))
    target = args.get("text_or_selector")
    if not target or not isinstance(target, str):
        raise PhoneToolError("phone_click requires a non-empty 'text_or_selector' string")

    found = await evaluate(mgr, tab_id, _FIND_ELEMENT_EXPR.format(target=json.dumps(target)))
    if not found:
        raise PhoneToolError(f"no element found matching {target!r} (selector or exact visible text)")

    await asyncio.sleep(SCROLL_SETTLE_DELAY)

    rect = await evaluate(mgr, tab_id, _ELEMENT_RECT_EXPR.format(target=json.dumps(target)))
    if not isinstance(rect, dict):
        raise PhoneToolError(f"element matching {target!r} disappeared before it could be clicked")
    x, y = rect["x"], rect["y"]

    await mgr.send(
        tab_id,
        "Input.dispatchMouseEvent",
        {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
    )
    await mgr.send(
        tab_id,
        "Input.dispatchMouseEvent",
        {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
    )

    await asyncio.sleep(0.3)
    after = await evaluate(mgr, tab_id, _page_state_expr(500, None))
    if not isinstance(after, dict):
        after = {}

    return {
        "ok": True,
        "tab_id": tab_id,
        "clicked": {"tag": rect.get("tag"), "text": rect.get("text"), "x": x, "y": y},
        "page_after": after,
    }


async def tool_phone_type(mgr: CDPManager, args: dict[str, Any]) -> dict[str, Any]:
    tab_id = await _resolve_tab_id(args.get("tab_id"))
    text = args.get("text")
    if text is None or not isinstance(text, str):
        raise PhoneToolError("phone_type requires a 'text' string")
    selector = args.get("selector")
    submit = bool(args.get("submit", False))

    selector_expr = json.dumps(selector) if selector else "'input, textarea'"
    find_expr = f"""
(() => {{
  const el = document.querySelector({selector_expr});
  if (!el) return null;
  el.scrollIntoView({{block: 'center', inline: 'center'}});
  el.focus();
  const r = el.getBoundingClientRect();
  return {{x: r.left + r.width / 2, y: r.top + r.height / 2, tag: el.tagName}};
}})()
"""
    target = await evaluate(mgr, tab_id, find_expr)
    if not isinstance(target, dict):
        raise PhoneToolError(
            f"no input found ({'selector ' + repr(selector) if selector else 'no selector given, and no input/textarea on page'})"
        )

    await asyncio.sleep(SCROLL_SETTLE_DELAY)
    x, y = target["x"], target["y"]
    await mgr.send(
        tab_id, "Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1}
    )
    await mgr.send(
        tab_id, "Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1}
    )
    await asyncio.sleep(0.2)

    await mgr.send(tab_id, "Input.insertText", {"text": text})

    if submit:
        await asyncio.sleep(0.1)
        for key_type in ("keyDown", "keyUp"):
            await mgr.send(
                tab_id,
                "Input.dispatchKeyEvent",
                {
                    "type": key_type,
                    "key": "Enter",
                    "code": "Enter",
                    "windowsVirtualKeyCode": 13,
                    "nativeVirtualKeyCode": 13,
                },
            )

    await asyncio.sleep(0.2)
    after = await evaluate(mgr, tab_id, _page_state_expr(500, None))
    if not isinstance(after, dict):
        after = {}

    return {
        "ok": True,
        "tab_id": tab_id,
        "typed_into": {"tag": target.get("tag")},
        "submitted": submit,
        "page_after": after,
    }


async def tool_phone_eval(mgr: CDPManager, args: dict[str, Any]) -> dict[str, Any]:
    tab_id = await _resolve_tab_id(args.get("tab_id"))
    expression = args.get("expression")
    if not expression or not isinstance(expression, str):
        raise PhoneToolError("phone_eval requires a non-empty 'expression' string")
    value = await evaluate(mgr, tab_id, expression)
    return {"ok": True, "tab_id": tab_id, "result": value}


def css_to_physical(css_x: float, css_y: float) -> tuple[int, int]:
    physical_x = round(css_x * PHONE_DPR)
    physical_y = round(css_y * PHONE_DPR) + PHONE_TOP_OFFSET_PHYSICAL
    return physical_x, physical_y


async def tool_phone_tap(mgr: CDPManager, args: dict[str, Any]) -> dict[str, Any]:
    css_x, css_y = args.get("css_x"), args.get("css_y")
    physical_x, physical_y = args.get("physical_x"), args.get("physical_y")
    auto_convert = args.get("auto_convert", True)

    have_css = css_x is not None and css_y is not None
    have_physical = physical_x is not None and physical_y is not None

    if not have_css and not have_physical:
        raise PhoneToolError("phone_tap needs either (css_x, css_y) or (physical_x, physical_y)")

    if have_physical and not (have_css and auto_convert):
        px, py = int(round(physical_x)), int(round(physical_y))
        source = "physical"
    else:
        px, py = css_to_physical(float(css_x), float(css_y))
        source = "css_auto_converted"

    command = f"adb shell input tap {px} {py}"
    return {
        "ok": True,
        "note": (
            "This tool does NOT tap the phone. Payment and other trust-sensitive "
            "actions require a real physical touch, not a CDP-synthesized event. "
            "Run the adb_command yourself in Termux on the phone."
        ),
        "source": source,
        "physical_x": px,
        "physical_y": py,
        "adb_command": command,
    }


TOOL_HANDLERS = {
    "phone_connect": tool_phone_connect,
    "phone_tabs": tool_phone_tabs,
    "phone_read": tool_phone_read,
    "phone_navigate": tool_phone_navigate,
    "phone_click": tool_phone_click,
    "phone_type": tool_phone_type,
    "phone_eval": tool_phone_eval,
    "phone_tap": tool_phone_tap,
}


# --------------------------------------------------------------------------
# Tool schemas (MCP tools/list)
# --------------------------------------------------------------------------

TOOLS = [
    {
        "name": "phone_connect",
        "description": (
            "Establish the SSH relay tunnel (local port -> relay VPS -> phone Chrome), "
            "verify CDP connectivity, and list open tabs. Call this first, or any time the "
            "connection may have dropped -- it is idempotent and safe to re-run."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "phone_tabs",
        "description": "List all open Chrome tabs on the phone (title, URL, tab ID).",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "phone_read",
        "description": (
            "Read the current page in a tab: URL, title, and page text content "
            "(first N chars, default 3000). Optional CSS selector to read a specific element "
            "instead of the whole body. tab_id defaults to the first open tab."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab_id": {"type": "string", "description": "Tab ID; defaults to the first open tab."},
                "selector": {"type": "string", "description": "CSS selector to read instead of document.body."},
                "max_chars": {"type": "integer", "minimum": 1, "maximum": 200000, "default": DEFAULT_READ_CHARS},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "phone_navigate",
        "description": (
            "Navigate a tab to a URL and wait for it to settle (up to 30s). "
            "Returns the new page title and a text preview."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab_id": {"type": "string", "description": "Tab ID; defaults to the first open tab."},
                "url": {"type": "string", "minLength": 1},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "phone_click",
        "description": (
            "Find an element by visible text (exact match on a small leaf element) or CSS "
            "selector, scroll it into view, and click it with a trusted CDP mouse event "
            "(mousePressed + mouseReleased). Works on normal product/content pages; CDP "
            "synthesized clicks are NOT trusted on payment pages -- use phone_tap for those."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab_id": {"type": "string", "description": "Tab ID; defaults to the first open tab."},
                "text_or_selector": {"type": "string", "minLength": 1},
            },
            "required": ["text_or_selector"],
            "additionalProperties": False,
        },
    },
    {
        "name": "phone_type",
        "description": (
            "Focus an input (by CSS selector, or the first input/textarea on the page) and "
            "type text into it via Input.insertText. Optionally press Enter after (submit=true)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab_id": {"type": "string", "description": "Tab ID; defaults to the first open tab."},
                "text": {"type": "string"},
                "selector": {"type": "string", "description": "CSS selector for the input; defaults to first input/textarea."},
                "submit": {"type": "boolean", "default": False},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "phone_eval",
        "description": "Run an arbitrary JavaScript expression in the page context and return its result.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab_id": {"type": "string", "description": "Tab ID; defaults to the first open tab."},
                "expression": {"type": "string", "minLength": 1},
            },
            "required": ["expression"],
            "additionalProperties": False,
        },
    },
    {
        "name": "phone_tap",
        "description": (
            "For trust-sensitive screens (e.g. payment pages) that check isTrusted and reject "
            "CDP-synthesized clicks. This does NOT tap the phone itself -- it only returns the "
            "'adb shell input tap' command string for a human to run in Termux, so the actual "
            "touch event is a real physical one. Pass either css_x/css_y (auto-converted to "
            "physical coordinates) or physical_x/physical_y directly."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "css_x": {"type": "number"},
                "css_y": {"type": "number"},
                "physical_x": {"type": "number"},
                "physical_y": {"type": "number"},
                "auto_convert": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
    },
]


# --------------------------------------------------------------------------
# JSON-RPC / MCP stdio loop
# --------------------------------------------------------------------------


def _write(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _reply(request_id: Any, *, result: Any = None, error: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    _write(payload)


def _tool_result(value: Any, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}],
        "isError": bool(is_error),
    }
    if not is_error:
        result["structuredContent"] = value
    return result


async def _handle_tools_call(mgr: CDPManager, request_id: Any, params: dict[str, Any]) -> None:
    name = params.get("name") if isinstance(params, dict) else None
    arguments = params.get("arguments") if isinstance(params, dict) else None
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        _reply(request_id, result=_tool_result({"ok": False, "error": "arguments must be an object"}, True))
        return

    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        _reply(request_id, result=_tool_result({"ok": False, "error": f"unknown tool: {name}"}, True))
        return

    try:
        value = await handler(mgr, arguments)
        _reply(request_id, result=_tool_result(value))
    except PhoneToolError as exc:
        _reply(request_id, result=_tool_result({"ok": False, "error": str(exc)}, True))
    except asyncio.TimeoutError:
        _reply(request_id, result=_tool_result({"ok": False, "error": f"{name} timed out"}, True))
    except Exception as exc:  # noqa: BLE001 - last-resort guard so the stdio loop never dies
        _reply(request_id, result=_tool_result({"ok": False, "error": f"internal error: {exc}"}, True))


async def _read_line(loop: asyncio.AbstractEventLoop) -> str:
    return await loop.run_in_executor(None, sys.stdin.readline)


async def main() -> int:
    loop = asyncio.get_running_loop()
    mgr = CDPManager()
    try:
        while True:
            raw = await _read_line(loop)
            if raw == "":
                break  # EOF
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue

            method = message.get("method")
            request_id = message.get("id")

            if method == "notifications/initialized":
                continue
            if request_id is None:
                continue

            if method == "initialize":
                _reply(
                    request_id,
                    result={
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    },
                )
            elif method == "tools/list":
                _reply(request_id, result={"tools": TOOLS})
            elif method == "ping":
                _reply(request_id, result={})
            elif method == "tools/call":
                await _handle_tools_call(mgr, request_id, message.get("params") or {})
            else:
                _reply(request_id, error={"code": -32601, "message": f"method not found: {method}"})
    finally:
        await mgr.close_all()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(0)
