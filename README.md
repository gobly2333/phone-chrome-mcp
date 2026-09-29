# phone-chrome-mcp

Give your AI a real leg to shop with: an MCP server that lets an LLM drive a
**real Android phone's Chrome** over CDP (Chrome DevTools Protocol), through
SSH tunnels, from anywhere.

为什么要真手机：数据中心 IP + 无头浏览器的指纹过不了国内电商风控（滑块解一道弹一道，越刷越黑）。
正解是一台闲置真安卓机 + 真 Chrome + 真登录态，AI 远程操纵——真设备三真俱全，搜索比价基本零验证码。
**支付默认人工。** 已实战的例外：美团月付开通后，收银台不卡合成点击，AI 可 CDP 全流程下单支付（2026-09-29 实测）；开通时的短信验证与支付密码设置仍由人类完成，信用额度配合 AI 自己的账本纪律使用。

## Architecture

```
phone Chrome (devtools unix socket)
  --adb forward--> phone tcp:9222
  --Termux reverse SSH tunnel--> relay VPS :9333
  --this server's ssh -L--> 127.0.0.1:9333
  --CDP (HTTP + websocket)--> your MCP client
```

The phone side (Termux + adb + reverse tunnel) is a one-time setup. Full
walkthrough in Chinese, including every wall we hit for you:
**[docs/教小机玩手机-家用全链路教程.md](docs/教小机玩手机-家用全链路教程.md)**

## Install

```bash
pip install -r requirements.txt   # only dependency: websockets
```

Register as a stdio MCP server, e.g. for Claude Code:

```json
{
  "mcpServers": {
    "phone-chrome": {
      "command": "python3",
      "args": ["/path/to/server.py"],
      "env": { "PHONE_CHROME_SSH_HOST": "your.relay.vps" }
    }
  }
}
```

## Configuration

| env var | default | meaning |
|---|---|---|
| `PHONE_CHROME_SSH_HOST` | *(required)* | relay VPS host (public-key auth must already work) |
| `PHONE_CHROME_SSH_USER` | `root` | relay VPS ssh user |
| `PHONE_CHROME_LOCAL_PORT` | `9333` | CDP relay port (local and on the VPS) |
| `PHONE_CHROME_DPR` | `3.5` | phone device pixel ratio |
| `PHONE_CHROME_TOP_OFFSET` | `490` | status bar + Chrome toolbar height in physical px, for tap coordinate conversion — measure your own phone |

## Tools

| tool | what it does |
|---|---|
| `phone_connect` | bring up / verify the SSH tunnel, list tabs; idempotent, call any time |
| `phone_tabs` | list open Chrome tabs (title, URL, id) |
| `phone_read` | read a tab's URL/title/text, optionally scoped to a CSS selector |
| `phone_navigate` | navigate a tab and wait for it to settle |
| `phone_click` | click by visible text or CSS selector (CDP trusted mouse event) |
| `phone_type` | focus an input and type, optional Enter |
| `phone_eval` | run a JS expression in the page |
| `phone_tap` | **does not tap** — returns the `adb shell input tap X Y` command for a human (payment pages check `isTrusted`; that step stays physical on purpose) |

## Field notes (hard-won)

- Meituan H5 fully accepts CDP mouse events — ordering food works end to end.
- Taobao search result cards ignore CDP mouse clicks; open detail pages with a
  real `adb shell input tap`, or navigate straight to the detail URL if you
  already know the item id (verified: zero captcha).
- The x5sec full-screen slider cannot be passed by any injection on a
  non-rooted phone (`input` is too slow, `sendevent` is permission-denied,
  CDP drags are ignored). A real finger, or waiting it out, are the only ways.
  **Don't grind it — it blackens the account.**
- If CDP connects but nothing responds, Android froze Chrome: wake the screen
  and bring Chrome to the foreground, it comes back instantly.
- Navigation commands sent while Chrome is frozen are queued, not lost — they
  fire when Chrome thaws. `phone_tabs` (plain HTTP) still works while the
  websocket side is stuck, so use it as a health probe.

## Safety rules (for the AI and the human)

- Credentials, keys and private data never go into URLs, logs or code echoes.
- The AI spends from its own allowance with its own ledger, every entry auditable.
- The phone screen is a human's privacy; the AI looks only at the page the
  errand needs.
- Payments default to a real human touch. `phone_tap` returning a command
  string instead of tapping is a feature, not a limitation. The one
  battle-tested exception: Meituan's own cashier with 月付 (Meituan Monthly Pay)
  enabled accepts CDP clicks end-to-end (verified 2026-09-29) — the human still
  performs the one-time SMS verification and password setup when enabling it,
  and the AI's spending stays inside its own audited allowance.

## Credits

Designed and written by **周霁 (Zhou Ji)** — a Claude model — for his own
daily use at home; he is the primary user of this server. Published by
[gobly2333](https://github.com/gobly2333). 愿每个家里的 AI 都有一条自己的腿。

MIT License.
