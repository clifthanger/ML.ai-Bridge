#!/usr/bin/env python3
"""
mlai_kit - 9router client keys and the "replicate to another PC" kit.

Why this exists: 9router's HTTP API (/api/keys) only ever returns client keys
masked, so the full key cannot be obtained that way. The unmasked value lives in
9router's sqlite database:

    %APPDATA%/9router/db/data.sqlite   ->  table apiKeys, column key

This module reads it (from a copy, so the running server keeps its locks),
shows it in YOUR terminal, copies it to the clipboard, and can export a
complete setup kit for a second machine.

Standalone use:
    python mlai_kit.py keys      show the 9router client keys
    python mlai_kit.py export    write the replication kit to ./share
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
ENV_PATH = HERE / ".env"
SHARE = HERE / "share"
NINE_DATA = Path(os.environ.get("APPDATA", "")) / "9router"
NINE_DB = NINE_DATA / "db" / "data.sqlite"
HERMES_ENV = Path.home() / "AppData/Local/hermes/.env"
ROUTER_PORT = 20128


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[38;5;203m"
    GRN = "\033[38;5;114m"
    YEL = "\033[38;5;221m"
    CYA = "\033[38;5;80m"
    GRY = "\033[38;5;245m"
    WHI = "\033[38;5;255m"


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

def read_env_file(path):
    data = {}
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return data


def load_cfg():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def mlai_key():
    return (os.environ.get("MLAI_API_KEY")
            or read_env_file(ENV_PATH).get("MLAI_API_KEY")
            or load_cfg().get("api_key") or "")


LAST_DB_ERROR = ""


def nine_keys():
    """[(name, key, active)] straight from 9router's sqlite. Empty if unavailable.

    Reads the LIVE database read-only first — SQLite handles WAL readers fine and
    it avoids copying a 16 MB file on every screen repaint. The copy fallback uses
    a unique temp name and always closes its connection: reusing one temp path
    while a previous connection was still open is what made this silently return
    [] on Windows (the copy fails with the file in use).
    """
    global LAST_DB_ERROR
    LAST_DB_ERROR = ""
    if not NINE_DB.exists():
        LAST_DB_ERROR = "database not found: %s" % NINE_DB
        return []

    sql = "select name, key, isActive from apiKeys order by createdAt"
    uri = "file:%s?mode=ro" % str(NINE_DB).replace("\\", "/")
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            con.execute("PRAGMA query_only=1")
            return [{"name": r[0], "key": r[1], "active": bool(r[2])}
                    for r in con.execute(sql)]
        finally:
            con.close()
    except Exception as exc:
        first = "%s: %s" % (type(exc).__name__, exc)

    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix="9r_keys_", suffix=".sqlite")
        os.close(fd)
        shutil.copy(NINE_DB, tmp)
        for ext in ("-wal", "-shm"):
            if Path(str(NINE_DB) + ext).exists():
                shutil.copy(str(NINE_DB) + ext, str(tmp) + ext)
        con = sqlite3.connect(tmp, timeout=5)
        try:
            return [{"name": r[0], "key": r[1], "active": bool(r[2])}
                    for r in con.execute(sql)]
        finally:
            con.close()
    except Exception as exc:
        LAST_DB_ERROR = "%s  (copy fallback: %s: %s)" % (first, type(exc).__name__, exc)
        return []
    finally:
        if tmp:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(tmp + suffix)
                except Exception:
                    pass


def hermes_router_key():
    return read_env_file(HERMES_ENV).get("HERMES_CUSTOM_LOCALHOST_20128_API_KEY", "")


def copy_clip(text):
    try:
        if os.name == "nt":
            p = subprocess.Popen(["clip"], stdin=subprocess.PIPE, shell=False)
            p.communicate(text.encode("utf-16le"))
        elif sys.platform == "darwin":
            subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE).communicate(text.encode())
        else:
            subprocess.Popen(["xclip", "-selection", "clipboard"],
                             stdin=subprocess.PIPE).communicate(text.encode())
        return True
    except Exception:
        return False


def nine_cli_token():
    try:
        import hashlib
        mid = (NINE_DATA / "machine-id").read_text().strip()
        sec = (NINE_DATA / "auth" / "cli-secret").read_text().strip()
        return hashlib.sha256((mid + "9r-cli-auth" + sec).encode()).hexdigest()[:16]
    except Exception:
        return ""


# --------------------------------------------------------------------------
# the bridge's own incoming key
# --------------------------------------------------------------------------

def bridge_key():
    cfg = load_cfg()
    return os.environ.get("MLAI_BRIDGE_KEY") or cfg.get("client_key") or ""


def set_bridge_key(key):
    cfg = load_cfg()
    cfg["client_key"] = key
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return key


def new_bridge_key():
    import secrets
    return "sk-mlai-bridge-" + secrets.token_hex(16)


def nine_api(path, body=None, method=None):
    """Talk to the local 9router admin API. -> (status, parsed_or_text)"""
    tok = nine_cli_token()
    if not tok:
        return 0, "no 9router cli token"
    req = urllib.request.Request(
        "http://localhost:%d%s" % (ROUTER_PORT, path),
        data=json.dumps(body).encode() if body is not None else None,
        method=method or ("POST" if body is not None else "GET"))
    req.add_header("x-9r-cli-token", tok)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
        try:
            return r.status, json.loads(raw)
        except Exception:
            return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except Exception as e:
        return 0, type(e).__name__


def mlai_connection():
    """The 9router provider connection wired to this bridge."""
    st, data = nine_api("/api/providers")
    if st != 200 or not isinstance(data, dict):
        return None
    for c in data.get("connections", []):
        spec = c.get("providerSpecificData") or {}
        if spec.get("prefix") == "mlai":
            return c
    return None


def nine_connection_key():
    """The apiKey 9router actually stored for the mlai connection.

    GET /api/providers deliberately omits apiKey, so comparing against that
    response always looks like a mismatch. The real value is in the db:
    table providerConnections, column data (JSON), key apiKey.
    """
    if not NINE_DB.exists():
        return None
    try:
        uri = "file:%s?mode=ro" % str(NINE_DB).replace("\\", "/")
        con = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            con.execute("PRAGMA query_only=1")
            cols = [d[1] for d in con.execute("PRAGMA table_info(providerConnections)")]
            for row in con.execute("select * from providerConnections"):
                d = dict(zip(cols, row))
                try:
                    blob = json.loads(d.get("data") or "{}")
                except Exception:
                    continue
                spec = blob.get("providerSpecificData") or {}
                if spec.get("prefix") == "mlai":
                    return blob.get("apiKey") or ""
        finally:
            con.close()
    except Exception:
        return None
    return None


def sync_key_to_nine(key):
    """Push the bridge key into the 9router connection so both sides agree."""
    conn = mlai_connection()
    if not conn:
        return False, "no mlai connection found in 9router"
    st, res = nine_api("/api/providers/%s" % conn["id"],
                       {"apiKey": key, "name": conn.get("name") or "mlai"},
                       method="PUT")
    if st in (200, 201):
        return True, "9router connection updated"
    return False, "9router said %s %s" % (st, str(res)[:150])


BRIDGE_PID_FILE = HERE / ".bridge.pid"
BRIDGE_LOG = HERE / "bridge.log"


def _pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid],
                                 capture_output=True, text=True, timeout=10).stdout
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _pid_on_port(port):
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, timeout=15).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "LISTENING":
                local = parts[1]
                if local.endswith(":%d" % port) or local.endswith("]:%d" % port):
                    try:
                        return int(parts[4])
                    except ValueError:
                        continue
    except Exception:
        pass
    return None


def bridge_health(port=None):
    port = port or load_cfg().get("port", 20129)
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=4) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def restart_bridge(wait=12):
    """Stop whatever holds the bridge port and start a fresh one.

    Needed after changing the incoming key: the running process still has the
    old one in memory, so every call 401s until it is restarted.
    """
    port = load_cfg().get("port", 20129)
    pid = None
    try:
        pid = int(BRIDGE_PID_FILE.read_text().strip())
    except Exception:
        pass
    if not _pid_alive(pid):
        pid = _pid_on_port(port)
    if _pid_alive(pid):
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                           capture_output=True, timeout=15)
        except Exception:
            pass
        time.sleep(0.6)
    try:
        BRIDGE_PID_FILE.unlink()
    except Exception:
        pass

    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200       # DETACHED_PROCESS | NEW_PROCESS_GROUP
    with open(BRIDGE_LOG, "a", encoding="utf-8") as log:
        proc = subprocess.Popen([sys.executable, str(HERE / "bridge.py")],
                                cwd=str(HERE), stdout=log, stderr=log,
                                creationflags=flags, close_fds=True)
    BRIDGE_PID_FILE.write_text(str(proc.pid))
    for _ in range(int(wait / 0.4)):
        time.sleep(0.4)
        if bridge_health(port):
            return True, "restarted (pid %d)" % proc.pid
    return False, "started but /health never answered — see bridge.log"


# --------------------------------------------------------------------------
# screen: keys
# --------------------------------------------------------------------------

def _show_nine_keys():
    print()
    print(C.CYA + "  9router client keys" + C.RESET)
    print(C.GRY + "  source : " + str(NINE_DB) + C.RESET)
    print(C.GRY + "  note   : /api/keys only ever returns these masked" + C.RESET)
    keys = nine_keys()
    if not keys:
        print(C.RED + "  could not read the database — is 9router installed here?" + C.RESET)
        if LAST_DB_ERROR:
            print(C.YEL + "  reason: " + LAST_DB_ERROR + C.RESET)
        return None
    in_use = hermes_router_key()
    for k in keys:
        used = "   " + C.GRN + "← the one Hermes uses" + C.RESET if k["key"] == in_use else ""
        state = C.GRN + "active" + C.RESET if k["active"] else C.GRY + "inactive" + C.RESET
        print()
        print("  " + C.BOLD + k["name"] + C.RESET + "   (" + state + ")" + used)
        print("  " + C.WHI + C.BOLD + k["key"] + C.RESET)
    match = next((k for k in keys if k["key"] == in_use), keys[0])
    print()
    if copy_clip(match["key"]):
        print(C.GRN + "  ✓ \"" + match["name"] + "\" copied to the clipboard" + C.RESET)
    else:
        print(C.YEL + "  ! clipboard unavailable — select the key above by hand" + C.RESET)
    print(C.GRY + "  per-install: valid only against the 9router that issued it." + C.RESET)
    return match


def _show_bridge_key():
    print()
    print(C.CYA + "  Bridge incoming API key" + C.RESET)
    key = bridge_key()
    env_set = bool(os.environ.get("MLAI_BRIDGE_KEY"))
    if not key:
        print("  " + C.YEL + "● not set — /v1/* is OPEN to anything on this machine" + C.RESET)
        print(C.GRY + "  (loopback only, but any local process can use your ML AI quota)" + C.RESET)
    else:
        print("  " + C.GRN + "● required" + C.RESET
              + ("  " + C.GRY + "from env MLAI_BRIDGE_KEY (read-only here)" + C.RESET
                 if env_set else ""))
        print("  " + C.WHI + C.BOLD + key + C.RESET)
        stored = nine_connection_key()
        if stored is None:
            print("  9router connection: " + C.YEL + "could not read from 9router's db"
                  + C.RESET)
        elif stored == key:
            print("  9router connection: " + C.GRN + "in sync" + C.RESET)
        else:
            print("  9router connection: " + C.RED
                  + "MISMATCH — 9router sends a different key, calls will 401. "
                    "Fix with option 7" + C.RESET)
        if copy_clip(key):
            print(C.GRN + "  ✓ copied to the clipboard" + C.RESET)
    return key


def _set_bridge_key_interactive():
    print()
    print(C.CYA + "  Set the bridge incoming key" + C.RESET)
    print(C.GRY + "  [1] generate a strong key   [2] type my own   [0] disable auth" + C.RESET)
    choice = input("  choice: ").strip()
    if choice == "1":
        key = new_bridge_key()
        print(C.GRY + "  generated: " + C.WHI + key + C.RESET)
    elif choice == "2":
        key = input("  key: ").strip()
        if not key:
            print(C.YEL + "  nothing entered" + C.RESET)
            return
    elif choice == "0":
        set_bridge_key("")
        print(C.YEL + "  auth disabled — /v1/* open on loopback again" + C.RESET)
        print(C.GRY + "  restart the bridge ([2] then [1]) to apply" + C.RESET)
        return
    else:
        return

    if os.environ.get("MLAI_BRIDGE_KEY"):
        print(C.YEL + "  warning: env MLAI_BRIDGE_KEY is set and wins over config.json"
              + C.RESET)
    set_bridge_key(key)
    print(C.GRN + "  ✓ saved to config.json" + C.RESET)
    if input("  also push it into the 9router connection? [Y/n] ").strip().lower() != "n":
        ok, msg = sync_key_to_nine(key)
        print(("  " + (C.GRN if ok else C.RED) + ("✓ " if ok else "✗ ") + msg + C.RESET))
    # the running process still holds the previous key, so restart it now
    if input("  restart the bridge to apply it now? [Y/n] ").strip().lower() != "n":
        print(C.GRY + "  restarting…" + C.RESET)
        ok, msg = restart_bridge()
        print(("  " + (C.GRN if ok else C.RED) + ("✓ " if ok else "✗ ") + msg + C.RESET))
        if ok:
            if nine_connection_key() == key:
                print(C.GRN + "  ✓ bridge and 9router now agree on the key" + C.RESET)
            else:
                print(C.YEL + "  ! 9router connection does not match yet — use option 7"
                      + C.RESET)


def show_keys(pause=True):
    """Interactive key screen: 9router key, ML AI key, bridge key."""
    while True:
        print()
        print(C.BOLD + C.CYA + "  KEYS" + C.RESET)
        print(C.GRY + "  " + "-" * 66 + C.RESET)
        bk = bridge_key()
        nk = nine_keys()                 # one read per repaint, not three
        mk = mlai_key()
        print("  1  bridge incoming key    "
              + (C.GRN + "set" + C.RESET if bk else C.YEL + "NOT SET (open)" + C.RESET))
        print("  2  ML AI Code key         "
              + (C.GRN + "set, " + str(len(mk)) + " chars" + C.RESET
                 if mk else C.RED + "missing" + C.RESET)
              + C.GRY + "   (only the bridge uses it)" + C.RESET)
        print("  3  9router client key     "
              + (C.GRN + str(len(nk)) + " found" + C.RESET
                 if nk else C.RED + "db unreadable" + C.RESET)
              + C.GRY + "   (Hermes + every CLI use it)" + C.RESET)
        if not nk and LAST_DB_ERROR:
            print("     " + C.YEL + LAST_DB_ERROR + C.RESET)
        print()
        print("  4  show/copy 9router key      5  show/copy ML AI key")
        print("  6  set bridge key             7  push bridge key to 9router")
        print("  0  back")
        ch = input(C.GRY + "  choice: " + C.RESET).strip().lower()
        if ch == "1":
            _show_bridge_key()
            input(C.GRY + "  enter…" + C.RESET)
        elif ch == "2":
            print()
            print(C.CYA + "  ML AI Code key" + C.RESET)
            k = mlai_key()
            print("  " + (C.WHI + C.BOLD + k + C.RESET if k else C.RED + "not set" + C.RESET))
            if k:
                print(C.GRY + "  stored in " + str(ENV_PATH) + C.RESET)
                print(C.GRN + "  ✓ copied to the clipboard" + C.RESET if copy_clip(k)
                      else C.YEL + "  ! clipboard unavailable" + C.RESET)
            print(C.GRY + "  change it with console menu [3]" + C.RESET)
            input(C.GRY + "  enter…" + C.RESET)
        elif ch == "3":
            _show_nine_keys()
            input(C.GRY + "  enter…" + C.RESET)
        elif ch == "4":
            m = _show_nine_keys()
            print(C.GRY + "  for another PC use menu [0] → export kit" + C.RESET)
            input(C.GRY + "  enter…" + C.RESET)
        elif ch == "5":
            k = mlai_key()
            print()
            print("  " + (C.WHI + C.BOLD + k + C.RESET if k else C.RED + "not set" + C.RESET))
            print(C.GRN + "  ✓ copied" + C.RESET if k and copy_clip(k)
                  else C.YEL + "  nothing to copy" + C.RESET)
            input(C.GRY + "  enter…" + C.RESET)
        elif ch == "6":
            _set_bridge_key_interactive()
            input(C.GRY + "  enter…" + C.RESET)
        elif ch == "7":
            k = bridge_key()
            if not k:
                print(C.YEL + "  set a bridge key first (option 6)" + C.RESET)
            else:
                ok, msg = sync_key_to_nine(k)
                print(("  " + (C.GRN if ok else C.RED) + ("✓ " if ok else "✗ ") + msg
                       + C.RESET))
            input(C.GRY + "  enter…" + C.RESET)
        elif ch in ("0", "q", ""):
            return 0


# --------------------------------------------------------------------------
# export kit
# --------------------------------------------------------------------------

SETUP_MD = """# ML AI Code -> 9router / Hermes : setup on a new PC

Exported {when} from `{src}`.

Two keys, two jobs. Do not mix them up:

| key | used by | goes in |
|---|---|---|
| ML AI Code key (`mlai_sk_...`) | the bridge only | `mlai-bridge/.env` |
| 9router client key | every client calling 9router | `hermes/.env` |

Real values live in `keys.txt` beside this file (only if you chose to write it).

## 1. Python + the bridge

Python 3.9 or newer. Copy the `mlai-bridge` folder (skip `.bridge.pid`,
`bridge.log`, `__pycache__`, `share`). No pip install - standard library only.

    cd mlai-bridge
    python bridge.py

## 2. ML AI Code key for the bridge

Create `mlai-bridge/.env`:

    MLAI_API_KEY=<the mlai_sk_ key>

Precedence: real env var `MLAI_API_KEY` > `.env` > `config.json`.
Confirm: `curl http://127.0.0.1:{bport}/v1/models`

## 2b. Bridge incoming key (optional but recommended)

Without it, anything on that machine can spend your ML AI quota through
`/v1/*`. Set one in `mlai-bridge.config.json`:

    "client_key": "sk-mlai-bridge-..."

or export `MLAI_BRIDGE_KEY` (that wins). `/health` stays open for monitoring.
Whatever you choose must match the `apiKey` on the 9router provider
connection, or 9router will get 401s.

## 3. Register the provider in that PC's 9router

Dashboard -> Providers -> Add -> OpenAI compatible:

| field | value |
|---|---|
| Name | ML AI Code |
| Prefix | mlai |
| API type | chat |
| Base URL | http://127.0.0.1:{bport}/v1 |
| API key | anything (the bridge is loopback only) |

Or let the bundled script do it through the 9router API:

    python push-9router-node.py

9router auto-discovers the models and exposes them as
`mlai/code`, `mlai/agent`, `mlai/opus-5.5r`, `mlai/pixis-opus`.

## 4. Hermes

`hermes-provider.yaml` has the entry to merge into `custom_providers`, and the
matching line for `hermes/.env`:

    HERMES_CUSTOM_LOCALHOST_{rport}_API_KEY=<that PC's 9router client key>

Then:

    hermes chat -q "hello" -m mlai/code

## 5. Verify

    python verify.py

## Gotchas

- The 9router client key is per-install and is NOT in 9router's HTTP API
  (it returns masked values only). Read it on each machine with
  `python mlai_kit.py keys` - it comes from
  `%APPDATA%/9router/db/data.sqlite`, table `apiKeys`.
- Latency is 10-60 s per reply: each call starts a cloud agent run, not a chat
  completion. Account concurrency 2, daily run cap 100.
- Both keys are secrets. `keys.txt` is plaintext - delete it after setup.
"""

PUSH_SCRIPT = '''#!/usr/bin/env python3
"""Register the ML AI Code bridge in the LOCAL 9router via its HTTP API.

Run this on the new PC with the bridge already listening.
"""
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BRIDGE_PORT = __BRIDGE_PORT__
ROUTER = "http://localhost:__ROUTER_PORT__"
BASE_URL = "http://127.0.0.1:%d/v1" % BRIDGE_PORT


def cli_token():
    data = Path(os.environ["APPDATA"]) / "9router"
    mid = (data / "machine-id").read_text().strip()
    sec = (data / "auth" / "cli-secret").read_text().strip()
    return hashlib.sha256((mid + "9r-cli-auth" + sec).encode()).hexdigest()[:16]


def api(path, body=None):
    req = urllib.request.Request(
        ROUTER + path,
        data=json.dumps(body).encode() if body is not None else None,
        method="POST" if body is not None else "GET")
    req.add_header("x-9r-cli-token", cli_token())
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def main():
    st, nodes = api("/api/provider-nodes")
    if st != 200:
        print("cannot reach 9router at", ROUTER, "->", nodes)
        return 1
    node = next((n for n in nodes.get("nodes", []) if n.get("prefix") == "mlai"), None)
    if node:
        print("provider node already present:", node["id"])
    else:
        st, res = api("/api/provider-nodes", {
            "name": "ML AI Code", "prefix": "mlai", "apiType": "chat",
            "baseUrl": BASE_URL, "type": "openai-compatible"})
        print("create node:", st, json.dumps(res)[:200])
        node = res.get("node") if isinstance(res, dict) else None
    if not node:
        return 1
    payload = {
        "provider": node["id"],
        "apiKey": os.environ.get("MLAI_BRIDGE_KEY", "mlai-local"),
        "name": "mlai",
        "providerSpecificData": {"prefix": "mlai", "apiType": "chat",
                                 "baseUrl": BASE_URL, "nodeName": "ML AI Code"}}
    if "--overwrite" in sys.argv:
        payload["allowOverwrite"] = True
    st, res = api("/api/providers", payload)
    text = res if isinstance(res, str) else json.dumps(res)
    if st == 201:
        print("create connection: created")
    elif "already exists" in text:
        print("connection already present (use --overwrite to replace it)")
    else:
        print("create connection:", st, text[:200])
        return 1
    print("done - models should appear as mlai/<name> in", ROUTER + "/v1/models")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

HERMES_YAML = """# merge this into custom_providers in hermes config.yaml
#   hermes config path      -> prints the file
# then put the key in hermes/.env:
#   HERMES_CUSTOM_LOCALHOST_{rport}_API_KEY=<that PC's 9router client key>
custom_providers:
  - name: Local (localhost:{rport})
    base_url: http://localhost:{rport}/v1
    key_env: HERMES_CUSTOM_LOCALHOST_{rport}_API_KEY
    api_mode: chat_completions
    model: mlai/code
    models:
      mlai/code: {{}}
      mlai/agent: {{}}
      mlai/opus-5.5r: {{}}
      mlai/pixis-opus: {{}}
"""


def export_kit(with_keys=False, pause=True):
    cfg = load_cfg()
    bport = cfg.get("port", 20129)
    rport = 20128
    SHARE.mkdir(exist_ok=True)
    when = time.strftime("%Y-%m-%d %H:%M")

    (SHARE / "SETUP.md").write_text(
        SETUP_MD.format(when=when, src=HERE, bport=bport, rport=rport),
        encoding="utf-8")
    (SHARE / "push-9router-node.py").write_text(
        PUSH_SCRIPT.replace("__BRIDGE_PORT__", str(bport)).replace("__ROUTER_PORT__", str(rport)),
        encoding="utf-8")
    (SHARE / "mlai-bridge.config.json").write_text(
        json.dumps({k: v for k, v in cfg.items()
                    if k not in ("api_key", "client_key")}, indent=2),
        encoding="utf-8")
    (SHARE / "mlai-bridge.env").write_text(
        "MLAI_API_KEY=<paste the mlai_sk_ key here>\n", encoding="utf-8")
    (SHARE / "hermes-provider.yaml").write_text(
        HERMES_YAML.format(rport=rport), encoding="utf-8")

    wrote_keys = False
    if with_keys:
        lines = ["# SECRETS - delete this file once the new PC is set up", ""]
        lines.append("MLAI_API_KEY=" + (mlai_key() or "<not set>"))
        lines.append("MLAI_BRIDGE_KEY=" + (bridge_key() or "<not set - /v1/* open>"))
        lines.append("")
        for k in nine_keys():
            lines.append("9router client key [%s]%s = %s"
                         % (k["name"], "" if k["active"] else " (inactive)", k["key"]))
        lines += ["",
                  "# 9router client keys are per-install and are NOT returned by",
                  "# 9router's HTTP API (masked only). They live in",
                  "# %APPDATA%/9router/db/data.sqlite, table apiKeys.",
                  "# Read them on any machine with:  python mlai_kit.py keys"]
        (SHARE / "keys.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        wrote_keys = True

    return {"dir": SHARE, "keys": wrote_keys, "files": sorted(p.name for p in SHARE.iterdir())}


def show_export(pause=True):
    print()
    print(C.CYA + "  Export replication kit" + C.RESET)
    print(C.GRY + "  target: " + str(SHARE) + C.RESET)
    print()
    for f, why in [("SETUP.md", "step-by-step for the new PC"),
                   ("push-9router-node.py", "auto-registers the provider there"),
                   ("mlai-bridge.config.json", "bridge tuning (no secrets)"),
                   ("mlai-bridge.env", "bridge key placeholder"),
                   ("hermes-provider.yaml", "hermes custom_providers entry")]:
        print("  " + C.GRY + f.ljust(24) + C.RESET + C.DIM + why + C.RESET)
    print()
    ans = input("  " + C.YEL + "also write keys.txt with the REAL keys? [y/N] "
                + C.RESET).strip().lower()
    res = export_kit(with_keys=ans == "y")
    print()
    print(C.GRN + "  ✓ kit written to " + str(res["dir"]) + C.RESET)
    print("  " + C.GRY + ", ".join(res["files"]) + C.RESET)
    if res["keys"]:
        print(C.YEL + "  ! keys.txt holds plaintext secrets — copy it, then delete it"
              + C.RESET)
    if copy_clip(str(res["dir"])):
        print(C.GRN + "  ✓ folder path copied to the clipboard" + C.RESET)
    if pause:
        input(C.GRY + "\n  enter to continue…" + C.RESET)
    return res


# --------------------------------------------------------------------------
# connection info: exactly what to paste where
# --------------------------------------------------------------------------

def lan_ip():
    try:
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "0.0.0.0"


def show_connection(port=None):
    """Everything a client needs, plus a toggle for LAN exposure."""
    while True:
        cfg = load_cfg()
        port = port or cfg.get("port", 20129)
        host = cfg.get("host", "127.0.0.1")
        key = bridge_key()
        lan = host not in ("127.0.0.1", "localhost")
        base = "http://%s:%d/v1" % (lan_ip() if lan else "127.0.0.1", port)
        local = "http://127.0.0.1:%d/v1" % port

        print()
        print(C.BOLD + C.CYA + "  CONNECTION INFO" + C.RESET)
        print(C.GRY + "  " + "-" * 66 + C.RESET)
        print("  base url        " + C.WHI + C.BOLD + base + C.RESET)
        print("  api key         "
              + (C.WHI + C.BOLD + key + C.RESET if key
                 else C.YEL + "<none — the bridge accepts any key>" + C.RESET))
        print("  models          " + C.CYA + "code   agent   opus-5.5r   pixis-opus" + C.RESET)
        print("  health (no key) " + C.GRY + "%s/health" % (base[:-3] if base.endswith("/v1") else base)
              + C.RESET)
        print("  reachable from  "
              + (C.YEL + "this machine + your LAN (host=%s)" % host + C.RESET if lan
                 else C.GRY + "this machine only (host=%s)" % host + C.RESET))
        print()
        print("  " + C.BOLD + "manual setup / other machines" + C.RESET)
        print(C.GRY + "  " + "-" * 66 + C.RESET)
        if lan:
            print("  9router → Providers → Add → OpenAI compatible")
            print("    Name      ML AI Code")
            print("    Prefix    mlai")
            print("    API type  chat")
            print("    Base URL  " + C.WHI + base + C.RESET)
            print("    API key   " + C.WHI + (key or "anything") + C.RESET)
            print()
            print("  curl:")
            print(C.GRY + '  curl %s/chat/completions \\' % base + C.RESET)
            print(C.GRY + '    -H "Authorization: Bearer %s" \\' % (key or "<key>") + C.RESET)
            print(C.GRY + '    -H "Content-Type: application/json" \\' + C.RESET)
            print(C.GRY + '    -d \'{"model":"code","messages":[{"role":"user","content":"hi"}]}\''
                  + C.RESET)
        else:
            print(C.YEL + "  Only this machine can reach the bridge right now." + C.RESET)
            print(C.GRY + "  Press [n] to expose it to your LAN (needs a bridge key)." + C.RESET)
        print()
        print("  " + C.BOLD + "automatic setup" + C.RESET)
        print(C.GRY + "  " + "-" * 66 + C.RESET)
        print("  Already done here: 9router provider node " + C.WHI + "\"mlai\"" + C.RESET
              + C.GRY + " → " + local + C.RESET)
        print(C.GRY + "  Menu [6] tests it, [e] exports the kit for another PC." + C.RESET)
        print()
        print("  1  copy base url      2  copy api key      3  copy curl example")
        print("  4  copy 9router form  5  show/copy ML AI key")
        print("  n  LAN access on/off  0  back")
        ch = input(C.GRY + "  choice: " + C.RESET).strip().lower()

        if ch == "1":
            print(C.GRN + "  ✓ " + base + " copied" + C.RESET if copy_clip(base)
                  else C.YEL + "  clipboard unavailable" + C.RESET)
            input(C.GRY + "  enter…" + C.RESET)
        elif ch == "2":
            if not key:
                print(C.YEL + "  no key set — nothing to copy" + C.RESET)
            else:
                print(C.GRN + "  ✓ api key copied" + C.RESET if copy_clip(key)
                      else C.YEL + "  clipboard unavailable" + C.RESET)
            input(C.GRY + "  enter…" + C.RESET)
        elif ch == "3":
            curl = ('curl %s/chat/completions -H "Authorization: Bearer %s" '
                    '-H "Content-Type: application/json" '
                    '-d \'{"model":"code","messages":[{"role":"user","content":"hi"}]}\''
                    % (base, key or "<key>"))
            print(C.GRN + "  ✓ curl command copied" + C.RESET if copy_clip(curl)
                  else C.YEL + "  clipboard unavailable" + C.RESET)
            input(C.GRY + "  enter…" + C.RESET)
        elif ch == "4":
            form = ("Name: ML AI Code\nPrefix: mlai\nAPI type: chat\n"
                    "Base URL: %s\nAPI key: %s" % (base, key or "anything"))
            print(C.GRN + "  ✓ 9router form copied" + C.RESET if copy_clip(form)
                  else C.YEL + "  clipboard unavailable" + C.RESET)
            input(C.GRY + "  enter…" + C.RESET)
        elif ch == "5":
            k = mlai_key()
            print("  " + (C.WHI + k + C.RESET if k else C.RED + "not set" + C.RESET))
            if k and copy_clip(k):
                print(C.GRN + "  ✓ copied" + C.RESET)
            input(C.GRY + "  enter…" + C.RESET)
        elif ch == "n":
            toggle_lan(port)
            input(C.GRY + "  enter…" + C.RESET)
        elif ch in ("0", "q", ""):
            return


def toggle_lan(port=None):
    """Bind the bridge to the LAN. Refuses without an incoming key."""
    cfg = load_cfg()
    port = port or cfg.get("port", 20129)
    host = cfg.get("host", "127.0.0.1")
    if host in ("127.0.0.1", "localhost"):
        if not bridge_key():
            print(C.RED + "  ✗ set a bridge key first — exposing an open /v1/* to the" + C.RESET)
            print(C.RED + "    whole network would let anyone spend your ML AI quota." + C.RESET)
            print(C.GRY + "    Keys screen, option 6." + C.RESET)
            return False
        print(C.YEL + "  This will make the bridge answer on %s:%d — every device on your"
              % (lan_ip(), port) + C.RESET)
        print(C.YEL + "  network can reach it, and spend your ML AI quota." + C.RESET)
        try:
            confirm = input('  type "lan" to confirm: ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            confirm = ""
        if confirm != "lan":
            print(C.GRN + "  cancelled — still bound to 127.0.0.1" + C.RESET)
            return False
        cfg["host"] = "0.0.0.0"
        print(C.YEL + "  ! binding to every interface." + C.RESET)
        print(C.GRY + "    Windows Firewall will likely prompt — allow private networks" + C.RESET)
        print(C.GRY + "    only. Requests still need the bridge key." + C.RESET)
    else:
        cfg["host"] = "127.0.0.1"
        print(C.GRN + "  ✓ back to this machine only" + C.RESET)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:                      # audit trail: whoever wonders later can see this
        with open(HERE / "bridge.log", "a", encoding="utf-8") as fh:
            fh.write("[mlai-kit] bind host %s -> %s (port %d)\n"
                     % (host, cfg["host"], port))
    except Exception:
        pass
    if input("  restart the bridge now to apply? [Y/n] ").strip().lower() != "n":
        ok, msg = restart_bridge()
        print(("  " + (C.GRN if ok else C.RED) + ("✓ " if ok else "✗ ") + msg + C.RESET))
    return True


# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# GET STARTED - guided first run
# --------------------------------------------------------------------------

def set_mlai_key(key):
    """ML AI Code key -> .env (and blank it out of config.json)."""
    lines, found = [], False
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("MLAI_API_KEY="):
                lines.append("MLAI_API_KEY=" + key)
                found = True
            else:
                lines.append(line)
    if not found:
        lines.insert(0, "MLAI_API_KEY=" + key)
    ENV_PATH.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    cfg = load_cfg()
    if cfg.get("api_key"):
        cfg["api_key"] = ""
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return key


def nine_running():
    try:
        with urllib.request.urlopen("http://localhost:%d/api/health" % ROUTER_PORT,
                                    timeout=4) as r:
            return json.loads(r.read().decode("utf-8", "replace")).get("ok") is True
    except Exception:
        return False


def check_mlai_key(key):
    """-> (ok, message). Validates against the real account endpoint."""
    req = urllib.request.Request("https://code.ml.ai/api/account")
    req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            acct = json.loads(r.read().decode("utf-8", "replace"))
        who = (acct.get("member") or {}).get("email", "?")
        return True, "%s — %d workspace(s)" % (who, len(acct.get("workspaces") or []))
    except urllib.error.HTTPError as e:
        return False, "rejected: HTTP %s %s" % (
            e.code, e.read().decode("utf-8", "replace")[:120])
    except Exception as e:
        return False, "could not reach code.ml.ai (%s)" % type(e).__name__


def ensure_nine_provider(bridge_port, client_key):
    """Create the provider node + connection in the local 9router. Idempotent."""
    base_url = "http://127.0.0.1:%d/v1" % bridge_port
    st, nodes = nine_api("/api/provider-nodes")
    if st != 200 or not isinstance(nodes, dict):
        return False, "9router admin API said %s %s" % (st, str(nodes)[:120])
    node = next((n for n in nodes.get("nodes", []) if n.get("prefix") == "mlai"), None)
    if not node:
        st, res = nine_api("/api/provider-nodes", {
            "name": "ML AI Code", "prefix": "mlai", "apiType": "chat",
            "baseUrl": base_url, "type": "openai-compatible"})
        if st not in (200, 201) or not isinstance(res, dict):
            return False, "could not create the provider node: %s %s" % (st, str(res)[:120])
        node = res.get("node") or {}
    conn = mlai_connection()
    if conn:
        st, res = nine_api("/api/providers/%s" % conn["id"],
                           {"apiKey": client_key, "name": conn.get("name") or "mlai"},
                           method="PUT")
        if st not in (200, 201):
            return False, "could not update the connection: %s %s" % (st, str(res)[:120])
        return True, "provider node + connection ready"
    st, res = nine_api("/api/providers", {
        "provider": node.get("id"), "apiKey": client_key, "name": "mlai",
        "providerSpecificData": {"prefix": "mlai", "apiType": "chat",
                                 "baseUrl": base_url, "nodeName": "ML AI Code"}})
    if st not in (200, 201):
        return False, "could not create the connection: %s %s" % (st, str(res)[:120])
    return True, "provider node + connection created"


def write_hermes_key(key):
    """Put the 9router client key into Hermes' .env, after backing the file up."""
    name = "HERMES_CUSTOM_LOCALHOST_%d_API_KEY" % ROUTER_PORT
    try:
        HERMES_ENV.parent.mkdir(parents=True, exist_ok=True)
        if HERMES_ENV.exists():
            shutil.copy(HERMES_ENV, str(HERMES_ENV) + ".bak")
        lines, found = [], False
        if HERMES_ENV.exists():
            for line in HERMES_ENV.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip().startswith(name + "="):
                    lines.append("%s=%s" % (name, key))
                    found = True
                else:
                    lines.append(line)
        if not found:
            lines.append("%s=%s" % (name, key))
        HERMES_ENV.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        return True, "%s written (backup: .env.bak)" % name
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)


def get_started(force=False):
    """Guided setup for a fresh machine. Auto-shown when the ML AI key is absent."""
    print()
    print(C.BOLD + C.CYA + "  GET STARTED" + C.RESET
          + C.GRY + "   first-run setup for this machine" + C.RESET)
    print(C.GRY + "  " + "-" * 66 + C.RESET)
    print(C.GRY + "  enter = accept the default · q at any prompt = stop" + C.RESET)

    def ask(prompt, default=""):
        try:
            got = input("  " + prompt).strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if got.lower() in ("q", "quit"):
            return None
        return got or default

    # ------------------------------------------------------------- step 1/5
    print()
    print("  " + C.BOLD + "step 1/5 · ML AI Code API key" + C.RESET)
    print(C.GRY + "  https://code.ml.ai → Settings → API keys" + C.RESET)
    key = None
    have = mlai_key()
    if have and not force:
        print("  current: " + C.GRN + have[:12] + "…" + have[-4:] + C.RESET)
        keep = ask("keep the current one? [Y/n] ", "y")
        if keep is None:
            return False
        if keep.lower() != "n":
            key = have
    while not key:
        got = ask("paste mlai_sk_… : ")
        if got is None:
            return False
        if not got:
            print(C.YEL + "  nothing entered — paste the key, or q to stop" + C.RESET)
            continue
        if not got.startswith("mlai_sk_"):
            print(C.YEL + "  ! keys normally start with mlai_sk_" + C.RESET)
        print(C.GRY + "  validating…" + C.RESET)
        ok, msg = check_mlai_key(got)
        if ok:
            print("  " + C.GRN + "✓ " + msg + C.RESET)
            key = got
        else:
            print("  " + C.RED + "✗ " + msg + C.RESET)
            if ask("  use it anyway? [y/N] ", "n").lower() == "y":
                key = got
    set_mlai_key(key)
    print("  " + C.GRN + "✓ saved to " + str(ENV_PATH) + C.RESET)

    # ------------------------------------------------------------- step 2/5
    print()
    print("  " + C.BOLD + "step 2/5 · bridge key" + C.RESET
          + C.GRY + "  (the front door of your bridge — 9router sends this)" + C.RESET)
    bkey = bridge_key()
    if force:
        bkey = None
    if bkey:
        print("  current: " + C.GRN + bkey[:18] + "…" + C.RESET)
        keep = ask("keep it? [Y/n] ", "y")
        if keep is None:
            return False
        if keep.lower() == "n":
            bkey = None
    if not bkey:
        bkey = new_bridge_key()
        print("  generated: " + C.WHI + C.BOLD + bkey + C.RESET)
        if copy_clip(bkey):
            print(C.GRN + "  ✓ copied to the clipboard" + C.RESET)
    set_bridge_key(bkey)
    print("  " + C.GRN + "✓ saved to config.json" + C.RESET)

    # ------------------------------------------------------------- step 3/5
    print()
    print("  " + C.BOLD + "step 3/5 · start the bridge" + C.RESET)
    port = load_cfg().get("port", 20129)
    if bridge_health(port) and not force:
        print(C.GRY + "  running already — restarting so it picks up the key…" + C.RESET)
    ok, msg = restart_bridge()
    print(("  " + (C.GRN if ok else C.RED) + ("✓ " if ok else "✗ ") + msg + C.RESET))
    if not ok:
        print(C.GRY + "  log: " + str(BRIDGE_LOG) + C.RESET)
        if ask("  continue anyway? [y/N] ", "n").lower() != "y":
            return False
    print("  " + C.GRY + "/health: requires_api_key=%s"
          % (bridge_health(port) or {}).get("requires_api_key") + C.RESET)

    # ------------------------------------------------------------- step 4/5
    print()
    print("  " + C.BOLD + "step 4/5 · register the provider in 9router" + C.RESET)
    if not nine_running():
        print("  " + C.YEL + "! 9router is not answering on :%d" % ROUTER_PORT + C.RESET)
        print(C.GRY + "  start 9router and re-run this. You can still point any OpenAI"
              + C.RESET)
        print(C.GRY + "  client straight at the bridge with the url below." + C.RESET)
    else:
        ok, msg = ensure_nine_provider(port, bkey)
        print(("  " + (C.GRN if ok else C.RED) + ("✓ " if ok else "✗ ") + msg + C.RESET))
        if ok:
            stored = nine_connection_key()
            print("  9router connection key: "
                  + (C.GRN + "matches the bridge" + C.RESET if stored == bkey
                     else C.RED + "does NOT match yet" + C.RESET))
        else:
            print(C.GRY + "  by hand: 9router → Providers → Add → OpenAI compatible"
                  + C.RESET)

    # ------------------------------------------------------------- step 5/5
    print()
    print("  " + C.BOLD + "step 5/5 · wire Hermes to 9router" + C.RESET)
    rk = hermes_router_key()
    keys = nine_keys()
    if rk:
        print("  " + C.GRN + "✓ Hermes already has a 9router client key" + C.RESET)
    elif not keys:
        print(C.GRY + "  could not read 9router's keys — skipping" + C.RESET)
    else:
        me = keys[0]
        print("  9router client key \"" + C.BOLD + me["name"] + C.RESET
              + "\" exists; Hermes does not have it yet.")
        if ask("  write it into Hermes' .env? [Y/n] ", "y").lower() != "n":
            ok, msg = write_hermes_key(me["key"])
            print(("  " + (C.GRN if ok else C.RED) + ("✓ " if ok else "✗ ") + msg + C.RESET))

    # ----------------------------------------------------------------- test
    print()
    print("  " + C.BOLD + "test" + C.RESET
          + C.GRY + "  (drives a real agent run, ~20-40 s)" + C.RESET)
    passed = failed = 0
    for attempt in range(1, 4):
        if ask("run the end-to-end test? [Y/n] ", "y").lower() == "n":
            break
        print(C.GRY + "  running verify.py…" + C.RESET)
        try:
            proc = subprocess.run([sys.executable, "-u", str(HERE / "verify.py")],
                                  cwd=str(HERE), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=600)
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        except Exception as exc:
            out = "could not run verify.py: %s" % exc
        for line in out.splitlines():
            col = (C.GRN if line.startswith("PASS")
                   else C.RED if line.startswith("FAIL") else C.GRY)
            print("  " + col + line + C.RESET)
        passed = sum(1 for l in out.splitlines() if l.startswith("PASS"))
        failed = sum(1 for l in out.splitlines() if l.startswith("FAIL"))
        if failed == 0 and passed:
            print("  " + C.GRN + "✓ everything works" + C.RESET)
            break
        print("  " + C.RED + "✗ %d check(s) failed" % failed + C.RESET)
        if attempt == 3:
            print(C.GRY + "  giving up after 3 attempts — fix what failed above" + C.RESET)

    # --------------------------------------------------------------- result
    cfg = load_cfg()
    port = cfg.get("port", 20129)
    lan = cfg.get("host", "127.0.0.1") not in ("127.0.0.1", "localhost")
    url = "http://%s:%d/v1" % (lan_ip() if lan else "127.0.0.1", port)
    print()
    print(C.GRY + "  " + "=" * 66 + C.RESET)
    print("  " + C.BOLD + C.GRN + "YOUR CONNECTION" + C.RESET)
    print(C.GRY + "  " + "=" * 66 + C.RESET)
    print("  url      " + C.WHI + C.BOLD + url + C.RESET)
    print("  api key  " + C.WHI + C.BOLD + bkey + C.RESET)
    print("  listens  "
          + (C.YEL + "all interfaces (%s) — your whole network can reach it"
             % cfg.get("host", "0.0.0.0") + C.RESET if lan
             else C.GRN + "this machine only (127.0.0.1)" + C.RESET))
    print("  models   " + C.CYA
          + "mlai/code   mlai/agent   mlai/opus-5.5r   mlai/pixis-opus" + C.RESET)
    print(C.GRY + "  ------------------------------------------------------------" + C.RESET)
    print(C.GRY + "  9router → Providers → Add → OpenAI compatible" + C.RESET)
    print(C.GRY + "    name ML AI Code · prefix mlai · type chat" + C.RESET)
    print(C.GRY + "    base url " + url + C.RESET)
    print(C.GRY + "    api key  " + bkey + C.RESET)
    print(C.GRY + "  Hermes:  hermes chat -q \"hello\" -m mlai/code" + C.RESET)
    if copy_clip("Base URL: %s\nAPI key: %s\nModel: mlai/code" % (url, bkey)):
        print(C.GRN + "  ✓ url + key + model copied to the clipboard" + C.RESET)
    print(C.GRY + "  %d passed · [5] shows this again, [6] retests, [v] full verify"
          % passed + C.RESET)
    return passed > 0 and failed == 0


def main():
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "keys").lower()
    if cmd in ("get-started", "start", "setup"):
        return 0 if get_started(force="--force" in sys.argv) else 1
    if cmd == "keys":
        return show_keys()
    if cmd == "export":
        res = export_kit(with_keys="--with-keys" in sys.argv)
        print("wrote:", res["dir"])
        for f in res["files"]:
            print("  ", f)
        if res["keys"]:
            print("WARNING: keys.txt contains plaintext secrets.")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
