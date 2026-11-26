# mlai-bridge — use your ML AI Code subscription from 9router / Hermes / any OpenAI client

ML AI Code (`code.ml.ai`) does **not** ship an OpenAI-compatible inference API.
Its public docs only cover **routines** (`/api/routines`), which is why the docs
look incomplete — the inference API you were hoping for does not exist.

What *does* exist is a private workspace RPC that the web app itself uses. This
bridge wraps that RPC in a real `/v1/chat/completions` endpoint, so any
OpenAI-compatible client can talk to your ML AI Code agent.

```
Hermes / Claude Code / Cursor / Cline / any OpenAI client
        |
        v
9router  http://localhost:20128/v1        (provider node "mlai")
        |
        v
mlai-bridge  http://127.0.0.1:20129/v1    (this repo)
        |
        v
POST https://code.ml.ai/api/workspace     (undocumented RPC)
```

---

## Quickstart (fresh clone)

Requirements: **Python 3.9+**. No dependencies, no `pip install` — everything is
standard library. 9router is optional: without it you can still point any
OpenAI-compatible client straight at the bridge.

```bash
git clone https://github.com/clifthanger/ML.ai-Bridge.git
cd ML.ai-Bridge
git config core.hooksPath .githooks      # optional: refuse to commit secrets

python mlai_kit.py get-started           # the whole setup, guided
```

On Windows you can double-click **`ML AI Code.bat`** instead — it finds Python
itself and opens the console. **If no ML AI Code key is configured, the console
opens the Get started wizard automatically**, so a brand-new machine never lands
on an empty dashboard.

The wizard asks for your `mlai_sk_…` key, generates a key for the bridge, starts
it, registers the provider in 9router, wires Hermes, then runs the end-to-end
test until it passes. It finishes with the three lines you actually need, copied
to your clipboard:

```
url      http://127.0.0.1:20129/v1
api key  sk-mlai-bridge-…
models   mlai/code   mlai/agent   mlai/opus-5.5r   mlai/pixis-opus
```

Then use it:

```bash
hermes chat -q "hello" -m mlai/code
```

Your key is written into `.env`, which is committed **empty** in this repo — the
tool fills it in on your machine. Nothing else needs editing by hand.

---

## 1. The reverse-engineered ML AI Code API

Everything below is the real, working surface. Auth is
`Authorization: Bearer mlai_sk_...` on every call.

### Read-only endpoints

| Method | Path | Returns |
|---|---|---|
| GET | `/api/account` | member, workspaces (with access tokens), runs + token usage |
| GET | `/api/billing` | plan, status, daily limits, credits |
| GET | `/api/routines` | your routines |

`/api/machines` exists but is browser-session only — API keys get
`403 machine_browser_only`.

### `POST /api/workspace` — the agent RPC

Body is `{"action": "<op>", "token": "<workspace access token>", ...}`.
The workspace token comes from `/api/account` → `workspaces[].token` and looks
like `mini-<uuid>.<64 hex chars>`.

| action | extra fields | what it does |
|---|---|---|
| `run` | `task` (max **30 000 chars**) | start a task on the workspace's cloud machine |
| `resume` | `task` | queue / continue a run |
| `status` | — | full run state: `status.state`, `status.result.submission` (the answer), `usage`, `events[]`, `conversation[]` |
| `stop` | — | ask the machine to stop (state change is laggy — up to ~15 min) |
| `create` | — | create a brand-new workspace (returns a fresh token) |
| `read` | `path` | read a file from the cloud machine |
| `write` | `path`, content | write a file |
| `terminal` | `command` | run a shell command in the cloud machine |
| `diff` | — | git diff of the workspace |
| `export` | — | export a patch |
| `capabilities` | — | LSP/browser readiness + available skills |
| `preview` | — | preview the app running on the workspace |
| `rename` | `name` | rename the session |
| `history` | `item` | history lookups |
| `sleep` | — | hibernate the machine |

Run state values: `completed`, `failed`, `stopped`, `error`, `interrupted`
(and `running` while in flight). The model behind it reports itself as
`pixis-opus` / "Opus 5.5R", harness `mlai`.

### Errors you must handle

| Status | `code` | Meaning |
|---|---|---|
| 400 | — | `Enter a task` / `Enter a task under 30,000 characters` / `Unknown operation` |
| 409 | `run_locked` | one task per workspace at a time |
| 429 | `active_task_limit` | account-wide concurrency cap reached (2 on Infinite) |
| 403 | `machine_browser_only` | machine management needs a browser session |
| 401 | `api_key_invalid` | bad or revoked key |

> **Security note:** that API key is effectively *full control of the cloud
> workspaces* — it can read and write files and run shell commands on the
> machines. Treat it like an SSH key. It is stored in plaintext in
> `config.json` in this folder. Revoke it in Settings → API keys when done.

---

## 2. Running the bridge

Requirements: Python 3.9+. No dependencies (stdlib only).

### One click

**Desktop → `ML AI Code`** (shortcut, already created).

It opens the console TUI, and if the bridge is down it starts it for you.
Nothing else to do.

Autostart is now **on**: a windowless shortcut in your Startup folder runs
`bridge.py` at login, so the bridge is already up before you click anything.
Toggle it from the console with `[6]`, or:

```powershell
powershell -ExecutionPolicy Bypass -File install-shortcuts.ps1          # desktop icon
powershell -ExecutionPolicy Bypass -File install-shortcuts.ps1 -Startup # + login autostart
```

### The console (TUI)

```
  python mlai_console.py            interactive
  python mlai_console.py --status    one frame, scriptable
  python mlai_console.py --check     run the end-to-end test and exit
```

| key | action |
|---|---|
| `[g]` | **GET STARTED** — first-run wizard. Appears automatically when no ML AI key is set |
| `[1]` `[2]` `[3]` `[4]` | **BRIDGE** — start · stop · restart · autostart at login |
| `[5]` `[6]` | **CONNECT** — connection info (copy base URL / key / curl / 9router form, LAN toggle) · end-to-end test |
| `[7]` `[8]` | **SECRETS** — keys screen (9router · ML AI · bridge) · set the ML AI key |
| `[9]` `[v]` `[l]` | **DIAGNOSE** — refresh · run `verify.py` · tail `bridge.log` |
| `[e]` `[a]` | **MOVE** — export the kit for another PC · usage snippets |
| `[q]` | quit |

### Get started (fresh machine)

If the ML AI Code key is missing, the console opens the wizard on launch
instead of dropping you at the dashboard. Run it any time with `[g]`, or
headless:

```
python mlai_kit.py get-started          # --force to redo everything
```

What it walks through, in order:

```
step 1/5  ML AI Code key   paste mlai_sk_… , validated live against /api/account
step 2/5  bridge key       auto-generated (the key 9router will send)
step 3/5  start bridge     restarts it so it picks up the new key
step 4/5  register         creates the provider node + connection in 9router
step 5/5  wire Hermes      writes the 9router client key into hermes/.env (.bak kept)
test                       runs verify.py, retries up to 3x, and says what failed
```

and it ends with the block you actually need, copied to the clipboard:

```
url      http://127.0.0.1:20129/v1
api key  sk-mlai-bridge-…
listens  this machine only (127.0.0.1)
models   mlai/code   mlai/agent   mlai/opus-5.5r   mlai/pixis-opus
```

Press enter to accept every default. `q` at any prompt stops cleanly.

### Automatic vs manual wiring

Both paths exist on purpose.

**Automatic** (this machine, already done): the console registers the provider
node and connection in 9router through its own admin API, and pushes the bridge
key into the connection.

**Manual** (`[5]` → copy buttons): everything a client needs is on one screen,
already filled in:

```
base url   http://127.0.0.1:20129/v1
api key    sk-mlai-bridge-…            (the bridge's own key)
models     code  agent  opus-5.5r  pixis-opus
```

Paste that into 9router → Providers → Add → OpenAI compatible (Name `ML AI
Code`, Prefix `mlai`, API type `chat`) on any machine, or into any OpenAI
client. `[3]` copies a ready `curl`; `[4]` copies the whole 9router form.

**Reaching the bridge from another PC.** By default it binds `127.0.0.1`, so
only this machine can use it. Press `[n]` in `[5]` to bind `0.0.0.0` and the
screen switches to your LAN address (`http://192.168.x.x:20129/v1`) with the
form already updated. The console **refuses to do that unless a bridge key is
set** — an open `/v1/*` on a network lets anyone spend your ML AI quota.

### Credentials

Two different keys, two different jobs:

| key | who needs it | where it lives |
|---|---|---|
| `mlai_sk_…` (ML AI Code) | **only the bridge** | `mlai-bridge/.env` (console `[3]` writes it) |
| 9router client key `sk-337…` | **anything that calls 9router** — Hermes, Claude Code, Cursor | issued by the 9router dashboard, listed in `hermes/.env` as `HERMES_CUSTOM_LOCALHOST_20128_API_KEY` |

Hermes needs the 9router key because Hermes talks to 9router, not to ML AI.
The bridge itself does not require a client key (it is loopback-only) — so
nothing changes on the Hermes side; it already has everything it needs.

**Where the 9router key actually lives.** 9router's HTTP API
(`GET /api/keys`) only ever returns client keys **masked**, so there is no API
call that hands you the full value. The unmasked key is stored in 9router's
sqlite database:

```
%APPDATA%\9router\db\data.sqlite   →  table apiKeys, column key
```

Menu `[9]` reads it from a *copy* of that file (so the running server keeps its
locks), prints it in your terminal, and copies it to the clipboard. It also
flags which key Hermes is actually using.

Standalone: `python mlai_kit.py keys`

Precedence for the ML.ai key: real env var `MLAI_API_KEY` > `mlai-bridge/.env` > `config.json`.

### Replicating the setup on another PC

Menu `[0]` (or `python mlai_kit.py export --with-keys`) writes `share/`:

```
SETUP.md                 numbered steps for the new machine
push-9router-node.py     registers the provider there through the 9router API
mlai-bridge.config.json  bridge tuning, no secrets
mlai-bridge.env          bridge key placeholder
hermes-provider.yaml     custom_providers entry for Hermes
keys.txt                 the real keys — only if you answer "y"
```

`push-9router-node.py` is idempotent: it detects an existing `mlai` provider
node and connection and says so instead of duplicating them
(`--overwrite` replaces the connection instead).

One thing to know before copying keys around: **9router client keys are
per-install.** A key authenticates only against the 9router instance that
issued it, so on a fresh PC you either read that machine's own key with menu
`[9]`, or create a new one in the dashboard. Only the `mlai_sk_` key is
portable between machines.

```bash
cd ML.ai-Bridge
python bridge.py            # or: start-bridge.bat (no TUI)
```

It listens on `http://127.0.0.1:20129/v1`.

```bash
curl http://127.0.0.1:20129/health
curl http://127.0.0.1:20129/v1/models

curl http://127.0.0.1:20129/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"code","messages":[{"role":"user","content":"Reply with exactly: PONG42"}]}'
```

### config.json

| key | default | meaning |
|---|---|---|
| `api_key` | — | your `mlai_sk_...` key (or set env `MLAI_API_KEY`) |
| `port` / `host` | `20129` / `127.0.0.1` | listen address |
| `workspaces` | `[]` | workspace tokens; auto-filled from `/api/account` on first use |
| `auto_create_workspace` | `true` | create one if the account has none |
| `max_concurrent` | `2` | how many runs to keep in flight |
| `poll_interval_s` | `1.5` | status polling interval |
| `run_timeout_s` | `600` | give up on a run after this long |
| `slot_wait_s` | `900` | how long to wait for a free account slot |
| `model_alias` | `code` | model id the bridge advertises |
| `extra_models` | `["agent","opus-5.5r","pixis-opus"]` | extra ids to advertise (same backend) |
| `max_task_chars` | `28000` | hard cap under ml.ai's 30 000 limit |
| `client_key` | `""` | **incoming** API key for `/v1/*`. Empty = open (loopback only). `MLAI_BRIDGE_KEY` overrides it |

### The bridge's own incoming key

Three keys exist in this setup. Only one of them is the bridge's *front door*:

| key | direction | who checks it |
|---|---|---|
| `mlai_sk_…` | bridge → ML AI Code | ML AI Code checks it |
| 9router client key | client → 9router | 9router checks it |
| **bridge `client_key`** | **9router → bridge** | **the bridge checks it** |

By default `client_key` is empty and `/v1/*` is open. That is safe-ish because
the bridge binds `127.0.0.1`, but any process on this machine could spend your
ML AI quota through it. Set a key to close that:

```
console [9] → 6  set bridge key      # generate or type your own
             → copies it into the 9router connection automatically
```

Or by hand — `config.json` → `"client_key": "sk-mlai-bridge-…"`, then
`console [2]` stop and `[1]` start. The same value must sit on the 9router
provider connection as its `apiKey`, otherwise 9router gets 401s from the
bridge. Option `7` in the keys screen pushes it there for you, and the console
flags `MISMATCH` in red if the two ever drift apart.

**Changing the key restarts the bridge for you.** The running process keeps the
old key in memory, so until it restarts every call 401s. Option `6` therefore
offers to restart immediately, and if you ever get into that state the console
shows `running · key rejected` (yellow) instead of pretending the bridge is
stopped — press `[1]` and it restarts cleanly.

Note: `GET /api/providers` never returns `apiKey`, so the mismatch check reads
9router's own database (`db/data.sqlite` → `providerConnections` → `data` JSON)
rather than trusting that response.

`/health` stays open on purpose, so monitoring and the console can always tell
whether the bridge is alive. The `/health` payload reports
`"requires_api_key": true|false`.

Clients send it as `Authorization: Bearer <key>` or `x-api-key`.

---

## 3. Using it from 9router

Already wired. The bridge is registered as an `openai-compatible` provider
node with prefix `mlai`, so 9router exposes:

```
mlai/code
mlai/agent
mlai/opus-5.5r
mlai/pixis-opus
```

Point any CLI at 9router and pick one of those models:

```
Endpoint: http://localhost:20128/v1
API Key:  <your 9router key from the dashboard>
Model:    mlai/code
```

To re-create or move the node: Dashboard → Providers → Add → OpenAI compatible,
Base URL `http://127.0.0.1:20129/v1`, prefix `mlai`, API type `chat`.

---

## 4. Using it from Hermes

Hermes already points at 9router (`provider: custom`,
`base_url: http://localhost:20128/v1`), so no config change is needed — just
select the model:

```bash
hermes chat -q "hello" -m mlai/code          # one-off
# or in a session:
/model mlai/code
```

To make it the default:

```bash
hermes config set model.default mlai/code
```

Optional aliases:

```bash
hermes config set model.aliases.mlai custom/mlai/code
```

---

## 5. What this is, and what it is not

**Is:** a working OpenAI-compatible endpoint backed by the ML AI Code cloud
agent, on your subscription plan, usable from 9router and Hermes today.

**Is not:** a raw fast chat model. Every call starts a **cloud coding agent run**
on a VM:

- latency is ~10–60 s per reply (measured: 13–15 s for a one-word answer)
- account concurrency is **2** (`active_task_limit` beyond that)
- daily caps apply (`run_start`: 100, `workspace_create`: 40)
- tool-calling is not mapped — it's `messages in → text out`
- the agent sees its own workspace, and it may read/write files there

Good for: delegating real coding tasks, long-form reasoning, "second opinion"
runs. Bad for: fast chat loops, agents that need function calling.

---

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `account concurrency limit never freed` | two runs are stuck. The bridge issues `stop` automatically; ml.ai can take ~15 min to reflect it. Check `https://code.ml.ai` for running tasks. |
| `Enter a task under 30,000 characters` | a client sent a huge prompt. Lower `max_task_chars`, or fix the client's prompt. |
| `Invalid API key` from 9router | wrong 9router key — use the one from the 9router dashboard, not the `mlai_sk_` one. |
| Replies are slow | expected — it's an agent, not a chat model. |
| `403 machine_browser_only` | machine management needs the web UI, not the API. |

---

## 7. Files

```
bridge.py                the shim (stdlib only)
mlai_console.py          the TUI console / launcher
mlai_kit.py              keys, connection info, replication kit, Get started wizard
                         (python mlai_kit.py keys | export | get-started)
ML AI Code.bat           one-click entry point (used by the desktop shortcut)
find-python.bat          shared python finder for both .bat launchers
install-shortcuts.ps1    creates the desktop icon, optional login autostart
config.json              tuning + workspace tokens (committed with empty values)
.env                     MLAI_API_KEY=…  committed EMPTY, filled in by the wizard
.env.example             same thing, as a template
.githooks/pre-commit     refuses to commit anything that looks like a key
verify.py                end-to-end self-test (bridge + 9router + usage)
start-bridge.bat         bridge only, no TUI
LICENSE                  MIT
README.md                this file
```

Runtime state (`bridge.log`, `.bridge.pid`, `share/`, `__pycache__/`) is
gitignored.

---

## 8. Security notes

Read this before you paste anything.

**Your ML AI Code key is powerful.** With `mlai_sk_…` the RPC accepts
`terminal`, `read` and `write` against your cloud workspaces — it can run shell
commands and touch files on those machines. Treat it like an SSH key, not like a
chat API token. Revoke it in **Settings → API keys** if it leaks.

**`.env` is committed empty on purpose.** The tool fills it in locally. The
pre-commit hook blocks a commit if a real key appears in it, in `config.json`,
or in any staged file, but do not rely on that alone:

```bash
git config core.hooksPath .githooks      # enable it once per clone
```

**`config.json` is committed with `client_key: ""` and `workspaces: []`.**
Workspace tokens are credentials too — the bridge discovers them at runtime and
stores them locally, so keep them out of commits.

**The bridge listens on `127.0.0.1` by default.** Exposing it to your LAN is
deliberately awkward: you must set a bridge key first and then type the word
`lan` to confirm. While it is bound wide, the console shows a warning line and
every bind change is appended to `bridge.log`.

**9router credentials are per-install and not protected by its dashboard
password.** Its local admin API authenticates with `x-9r-cli-token`, derived
from `machine-id` + `auth/cli-secret` in `%APPDATA%/9router` — no password
involved. Client keys are also stored unmasked in `%APPDATA%/9router/db/data.sqlite`
(table `apiKeys`), even though `GET /api/keys` masks them. That is why the keys
screen can show them; it is also why file-level access to that machine is the
real boundary.

**Nothing here bypasses anything.** Every call uses your own credentials on your
own machine. The bridge only wraps an RPC that the ML AI Code web app already
uses with your own session.

---

## 9. Contributing

The most useful bug reports include the output of:

```bash
python verify.py
```

which exercises the whole chain (bridge, key enforcement, 9router, streaming,
model listing) and prints `PASS`/`FAIL` per line.
