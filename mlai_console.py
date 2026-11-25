#!/usr/bin/env python3
"""
ML AI Code - bridge console
One screen to run, watch and test the ML AI Code <-> OpenAI bridge.

    python mlai_console.py            interactive TUI (arrow-free, key driven)
    python mlai_console.py --status   print one frame and exit (scriptable)
    python mlai_console.py --check    run the end-to-end test and exit

Stdlib only. Windows-first, degrades gracefully elsewhere.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
ENV_PATH = HERE / ".env"
BRIDGE = HERE / "bridge.py"
PID_FILE = HERE / ".bridge.pid"
LOG_PATH = HERE / "bridge.log"
VERIFY = HERE / "verify.py"
ROUTER = "http://localhost:20128/v1"
HERMES_ENV = Path.home() / "AppData/Local/hermes/.env"
STARTUP = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"
SHORTCUT = STARTUP / "ML AI Code Console.lnk"
NINE_DATA = Path(os.environ.get("APPDATA", "")) / "9router"

IS_WIN = os.name == "nt"
SHOW_STATUS_ONLY = "--status" in sys.argv
CHECK_ONLY = "--check" in sys.argv

try:                                    # key display + replication kit
    import mlai_kit
except Exception:                       # pragma: no cover
    mlai_kit = None

VERSION = "1.1"


# --------------------------------------------------------------------------
# terminal helpers
# --------------------------------------------------------------------------

def enable_ansi():
    if not IS_WIN:
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VT_PROCESSING
        k.SetConsoleOutputCP(65001)
    except Exception:
        pass


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[38;5;203m"
    GRN = "\033[38;5;114m"
    YEL = "\033[38;5;221m"
    BLU = "\033[38;5;75m"
    MAG = "\033[38;5;176m"
    CYA = "\033[38;5;80m"
    GRY = "\033[38;5;245m"
    WHI = "\033[38;5;255m"


def clear():
    if SHOW_STATUS_ONLY:
        return
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def wide():
    try:
        return max(72, min(100, os.get_terminal_size().columns - 2))
    except Exception:
        return 84


W = wide()


def visible(s):
    return re.sub(r"\033\[[0-9;]*m", "", s)


def rule(ch="─", color=C.GRY):
    return color + ch * W + C.RESET


def row(label, value, ok=None, note=""):
    mark = ""
    if ok is True:
        mark = C.GRN + "● " + C.RESET
    elif ok is False:
        mark = C.RED + "● " + C.RESET
    elif ok is None and ok is not None:
        mark = C.GRY + "○ " + C.RESET
    left = "  " + C.GRY + label.ljust(16) + C.RESET
    body = mark + value
    if note:
        body += "   " + C.GRY + note + C.RESET
    return left + body


def box_top(title=""):
    t = (" " + C.BOLD + C.CYA + "ML AI CODE" + C.RESET + C.GRY + " · " + title + " ",
         visible("ML AI CODE · " + title))
    pad = W - len(t[1]) - 3
    return (C.GRY + "╔═" + t[0] + C.GRY + "═" * max(0, pad) + "╗" + C.RESET)


def box_bottom():
    return C.GRY + "╚" + "═" * (W - 2) + "╝" + C.RESET


# --------------------------------------------------------------------------
# config / credentials
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


def current_key():
    return (os.environ.get("MLAI_API_KEY")
            or read_env_file(ENV_PATH).get("MLAI_API_KEY")
            or load_cfg().get("api_key") or "")


def bridge_client_key():
    """The key the bridge demands on /v1/* (empty = open)."""
    return os.environ.get("MLAI_BRIDGE_KEY") or load_cfg().get("client_key") or ""


def mask(key):
    if not key:
        return "not set"
    return key[:12] + "…" + key[-4:] if len(key) > 20 else key


def save_key(key):
    """Write the ML.ai key to .env (preferred) and blank it from config.json."""
    lines, found = [], False
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("MLAI_API_KEY="):
                lines.append("MLAI_API_KEY=" + key)
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append("MLAI_API_KEY=" + key)
    ENV_PATH.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    cfg = load_cfg()
    if cfg.get("api_key"):
        cfg["api_key"] = ""
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def router_key():
    return read_env_file(HERMES_ENV).get("HERMES_CUSTOM_LOCALHOST_20128_API_KEY", "")


def nine_cli_token():
    """9router derives its local admin token from machine-id + cli-secret."""
    try:
        import hashlib
        mid = (NINE_DATA / "machine-id").read_text().strip()
        sec = (NINE_DATA / "auth" / "cli-secret").read_text().strip()
        return hashlib.sha256((mid + "9r-cli-auth" + sec).encode()).hexdigest()[:16]
    except Exception:
        return ""


def nine_node(cfg):
    """Confirm the mlai provider node really exists in 9router."""
    tok = nine_cli_token()
    if not tok:
        return None
    try:
        req = urllib.request.Request("http://localhost:20128/api/provider-nodes")
        req.add_header("x-9r-cli-token", tok)
        with urllib.request.urlopen(req, timeout=15) as resp:
            nodes = json.loads(resp.read().decode("utf-8", "replace")).get("nodes", [])
        for n in nodes:
            if n.get("prefix") == "mlai":
                return n
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------
# probes
# --------------------------------------------------------------------------

def http_json(url, key=None, timeout=8):
    req = urllib.request.Request(url)
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.bridge_ok = False
        self.bridge_auth_rejected = False
        self.bridge_models = []
        self.workspaces = None
        self.router_ok = False
        self.router_models = []
        self.node_ok = False
        self.node_info = None
        self.account = None
        self.billing = None
        self.key_ok = None
        self.key_error = ""
        self.last_test = "not run yet"
        self.verify_running = False
        self.verify_text = ""
        self.busy = ""
        self.autostart = SHORTCUT.exists()

    def snapshot(self):
        with self.lock:
            return dict(self.__dict__)


S = State()


def refresh():
    cfg = load_cfg()
    port = cfg.get("port", 20129)
    key = current_key()

    # --- local bridge first: it is instant, and it gives us the model list ---
    models, ok, ws = [], False, None
    auth_rejected = False
    try:
        d = http_json("http://127.0.0.1:%d/v1/models" % port, timeout=4,
                      key=bridge_client_key())
        models = [m["id"] for m in d["data"]]
        ok = True
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            # the process is alive and answering — it just wants a different key,
            # which happens right after the key was changed without a restart
            ok, auth_rejected = True, True
            models = S.bridge_models
    except Exception:
        pass
    try:
        ws = http_json("http://127.0.0.1:%d/health" % port, timeout=4).get("workspaces")
    except Exception:
        pass

    # --- everything remote runs in parallel; 9router's 200+ model list is
    #     never fetched (7s) because the model ids follow from the node prefix ---
    box = {}
    err = {}

    def work(name, fn):
        try:
            box[name] = fn()
        except urllib.error.HTTPError as e:
            err[name] = "HTTP %s" % e.code
            try:
                err[name] += " " + e.read().decode("utf-8", "replace")[:70]
            except Exception:
                pass
        except Exception as e:
            err[name] = type(e).__name__

    jobs = [("node", lambda: nine_node(cfg))]
    if key:
        jobs.append(("acct", lambda: http_json("https://code.ml.ai/api/account", key, timeout=25)))
        jobs.append(("bill", lambda: http_json("https://code.ml.ai/api/billing", key, timeout=25)))
    threads = [threading.Thread(target=work, args=j, daemon=True) for j in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(28)

    node = box.get("node")
    prefix = (node or {}).get("prefix") or "mlai"
    rmodels = ["%s/%s" % (prefix, m) for m in models] if node and models else []
    rok = bool(rmodels)
    acct, bill = box.get("acct"), box.get("bill")
    if not key:
        key_ok, key_err = False, "no key"
    elif acct is not None:
        key_ok, key_err = True, ""
    else:
        key_ok, key_err = False, err.get("acct", "unreachable")

    with S.lock:
        S.bridge_ok = ok
        S.bridge_auth_rejected = auth_rejected
        S.bridge_models = models
        S.workspaces = ws
        S.router_ok = rok
        S.router_models = rmodels
        S.node_ok = node is not None
        S.node_info = node
        S.account = acct
        S.billing = bill
        S.key_ok = key_ok
        S.key_error = key_err
        S.autostart = SHORTCUT.exists()


# --------------------------------------------------------------------------
# bridge process control
# --------------------------------------------------------------------------

def bridge_pid():
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:
        return None


def pid_alive(pid):
    if not pid:
        return False
    if IS_WIN:
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


def pid_on_port(port):
    """Whoever is listening on the port (windows) — used to adopt a stray bridge."""
    if not IS_WIN:
        return None
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, timeout=15).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 5 or parts[3] != "LISTENING":
                continue
            local = parts[1]
            if local.endswith(":%d" % port) or local.endswith("]:%d" % port):
                try:
                    return int(parts[4])
                except ValueError:
                    continue
    except Exception:
        pass
    return None


def start_bridge():
    with S.lock:
        S.busy = "starting bridge…"
    pid = bridge_pid()
    if pid_alive(pid):
        with S.lock:
            S.busy = ""
        return True, "already running (pid %s)" % pid
    # a healthy bridge may exist without a pid file (started by hand) — adopt it
    cfg = load_cfg()
    port = cfg.get("port", 20129)
    try:
        http_json("http://127.0.0.1:%d/health" % port, timeout=4)
        adopted = None
        for _ in range(6):          # netstat can lag a fresh listener
            adopted = pid_on_port(port)
            if adopted:
                PID_FILE.write_text(str(adopted))
                break
            time.sleep(0.4)
        with S.lock:
            S.busy = ""
        if not adopted:
            return True, ("already listening on :%d (pid unknown — "
                          "stop it with taskkill from Task Manager)" % port)
        return True, "already listening on :%d (adopted pid %s)" % (port, adopted)
    except Exception:
        pass
    log = open(LOG_PATH, "a", encoding="utf-8")
    flags = 0
    if IS_WIN:
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    proc = subprocess.Popen([sys.executable, str(BRIDGE)], cwd=str(HERE),
                            stdout=log, stderr=log, creationflags=flags,
                            close_fds=True)
    PID_FILE.write_text(str(proc.pid))
    for _ in range(20):
        time.sleep(0.4)
        if pid_alive(proc.pid):
            time.sleep(0.6)
            refresh()
            if S.bridge_ok:
                with S.lock:
                    S.busy = ""
                return True, "started (pid %d)" % proc.pid
    with S.lock:
        S.busy = ""
    return False, "did not come up — see bridge.log"


def stop_bridge():
    pid = bridge_pid()
    if not pid_alive(pid):
        # a bridge may be running without a pid file (started by hand, or the
        # file was cleaned) — find whoever holds the port and stop that
        pid = pid_on_port(load_cfg().get("port", 20129))
    if not pid_alive(pid):
        return True, "not running"
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                           capture_output=True, timeout=15)
        else:
            os.kill(pid, 15)
    except Exception as exc:
        return False, str(exc)
    try:
        PID_FILE.unlink()
    except Exception:
        pass
    time.sleep(0.5)
    return True, "stopped (pid %s)" % pid


# --------------------------------------------------------------------------
# end-to-end test (background so the TUI stays alive)
# --------------------------------------------------------------------------

def run_test_bg():
    def work():
        with S.lock:
            S.busy = "testing through 9router…"
        key = router_key()
        if not key:
            with S.lock:
                S.last_test = C.RED + "no 9router key in hermes .env" + C.RESET
                S.busy = ""
            return
        body = json.dumps({
            "model": S.snapshot()["router_models"][0] if S.snapshot()["router_models"] else "mlai/code",
            "messages": [{"role": "user",
                          "content": "Reply with exactly this token and nothing else: VERIFY-OK"}],
        }).encode()
        req = urllib.request.Request(ROUTER + "/chat/completions", data=body)
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", "Bearer " + key)
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=240) as resp:
                j = json.loads(resp.read().decode("utf-8", "replace"))
            el = time.time() - t0
            txt = j["choices"][0]["message"]["content"].strip()
            u = j.get("usage") or {}
            good = "VERIFY-OK" in txt
            col = C.GRN if good else C.YEL
            with S.lock:
                S.last_test = ("%s%.1fs   text=%r   prompt=%s completion=%s%s"
                               % (col, el, txt[:40], u.get("prompt_tokens"),
                                  u.get("completion_tokens"), C.RESET))
        except Exception as exc:
            with S.lock:
                S.last_test = C.RED + "%s: %s" % (type(exc).__name__, str(exc)[:90]) + C.RESET
        finally:
            with S.lock:
                S.busy = ""
    threading.Thread(target=work, daemon=True).start()


def refresh_bg():
    def work():
        with S.lock:
            S.busy = "refreshing…"
        try:
            refresh()
        finally:
            with S.lock:
                S.busy = ""
    threading.Thread(target=work, daemon=True).start()


def run_verify_bg():
    """Full end-to-end check in the background; ~30-40 s because it drives the agent."""
    def work():
        with S.lock:
            if S.verify_running:
                return
            S.verify_running = True
            S.verify_text = ""
        try:
            proc = subprocess.run([sys.executable, "-u", str(VERIFY)], cwd=str(HERE),
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=600)
            out = (proc.stdout or "") + (proc.stderr or "")
        except Exception as exc:
            out = "verify failed to run: %s: %s" % (type(exc).__name__, exc)
        with S.lock:
            S.verify_text = out.strip()
            S.verify_running = False
    threading.Thread(target=work, daemon=True).start()


# --------------------------------------------------------------------------
# autostart shortcut
# --------------------------------------------------------------------------

def set_autostart(on):
    """Autostart the BRIDGE (windowless) at login, not the console."""
    if not IS_WIN:
        return False, "windows only"
    if on:
        pyw = sys.executable
        for cand in (Path(sys.executable).with_name("pythonw.exe"),
                     Path(os.environ.get("LOCALAPPDATA", "")) /
                     "Programs/Python/Python311/pythonw.exe"):
            if cand.exists():
                pyw = cand
                break
        ps = (
            "$W=New-Object -ComObject WScript.Shell;"
            "$s=$W.CreateShortcut('%s');"
            "$s.TargetPath='%s';"
            "$s.Arguments='%s';"
            "$s.WorkingDirectory='%s';"
            "$s.Description='ML AI Code bridge (runs hidden at login)';"
            "$s.Save()"
            % (SHORTCUT, pyw, '"%s"' % (HERE / "bridge.py"), HERE)
        )
    else:
        ps = "Remove-Item -Force '%s' -ErrorAction SilentlyContinue" % SHORTCUT
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, text=True, timeout=45)
    except Exception as exc:
        return False, str(exc)
    with S.lock:
        S.autostart = SHORTCUT.exists()
    return S.autostart if on else not S.autostart, ("autostart on" if on else "autostart off")


# --------------------------------------------------------------------------
# render
# --------------------------------------------------------------------------

def render():
    cfg = load_cfg()
    st = S.snapshot()
    key = current_key()
    out = []
    out.append(box_top("bridge console"))
    out.append(C.GRY + "║" + C.RESET)

    # credentials block
    acct = st["account"] or {}
    member = (acct.get("member") or {}).get("email") or ""
    work = len(acct.get("workspaces") or [])
    kv = mask(key)
    kcolor = C.GRN if st["key_ok"] else (C.RED if key else C.YEL)
    out.append("  " + C.GRY + "ML.ai key".ljust(16) + C.RESET
               + kcolor + "● " + C.WHI + kv + C.RESET
               + ("   " + C.GRY + member + C.RESET if member else "")
               + ("   " + C.RED + st["key_error"] + C.RESET if not st["key_ok"] and st["key_error"] else ""))
    bill = st["billing"] or {}
    if bill:
        out.append("  " + C.GRY + "plan".ljust(16) + C.RESET
                   + C.WHI + str(bill.get("plan")) + C.RESET
                   + C.GRY + " · " + str(bill.get("status")) + C.RESET
                   + (" · " + C.YEL + "not unlimited" + C.RESET if not bill.get("unlimited") else "")
                   + C.GRY + " · daily runs " + str((bill.get("dailyLimits") or {}).get("run_start"))
                   + " · concurrency " + str((bill.get("credits") or {}).get("activeLimit")) + C.RESET)
    out.append(rule())

    # services
    bport = cfg.get("port", 20129)
    lan_bound = cfg.get("host", "127.0.0.1") not in ("127.0.0.1", "localhost")
    if st["bridge_auth_rejected"]:
        bridge_txt = C.YEL + "running · key rejected" + C.RESET
        bridge_note = C.YEL + "restart it to apply the new bridge key ([2] then [1])" + C.RESET
    elif st["bridge_ok"]:
        bridge_txt = C.GRN + "running" + C.RESET
        bridge_note = ("host=%s :%d" % (cfg.get("host"), bport) if lan_bound
                       else "127.0.0.1:%d" % bport)
    else:
        bridge_txt = C.RED + "stopped" + C.RESET
        bridge_note = "127.0.0.1:%d" % bport if not lan_bound else "host=%s :%d" % (cfg.get("host"), bport)
    out.append(row("bridge", bridge_txt, st["bridge_ok"] and not st["bridge_auth_rejected"],
                   bridge_note))
    if lan_bound:
        out.append("  " + C.YEL
                   + "         ! bound to %s — reachable from your network. "
                     "Turn it off in [5] → n" % cfg.get("host") + C.RESET)
    out.append(row("workspaces",
                   C.WHI + str(st["workspaces"] if st["workspaces"] is not None else work) + C.RESET))
    bkey_set = bool((cfg.get("client_key") or "").strip() or os.environ.get("MLAI_BRIDGE_KEY"))
    out.append(row("bridge auth",
                   (C.GRN + "api key required" + C.RESET) if bkey_set
                   else (C.YEL + "open on loopback" + C.RESET),
                   bkey_set, "set it with [9]" if not bkey_set else ""))
    node = "mlai → 127.0.0.1:%d" % bport
    ninfo = st.get("node_info") or {}
    if ninfo:
        node = "node \"mlai\" → %s" % ninfo.get("baseUrl", "")
    out.append(row("9router node", C.WHI + node + C.RESET, st["node_ok"], "localhost:20128"))
    out.append(row("models",
                   (C.CYA + "  ".join(st["router_models"]) + C.RESET) if st["router_models"]
                   else C.RED + "none visible" + C.RESET, bool(st["router_models"])))
    if st["router_models"]:
        out.append("  " + C.GRY + "use in hermes".ljust(16) + C.RESET
                   + C.WHI + "hermes chat -q \"…\" -m " + st["router_models"][0] + C.RESET)
    out.append(rule())

    # test
    out.append("  " + C.GRY + "last test".ljust(16) + C.RESET + st["last_test"])
    if st["verify_running"]:
        vt = C.YEL + "⟳ running verify.py…  (about 30-40 s)" + C.RESET
    elif st["verify_text"]:
        passed = sum(1 for l in st["verify_text"].splitlines() if l.startswith("PASS"))
        failed = sum(1 for l in st["verify_text"].splitlines() if l.startswith("FAIL"))
        vt = ((C.GRN if not failed else C.RED) + "PASS %d · FAIL %d" % (passed, failed)
              + C.RESET + C.GRY + "   press [v] for the full output" + C.RESET)
    else:
        vt = C.GRY + "not run — press [v]" + C.RESET
    out.append("  " + C.GRY + "verify".ljust(16) + C.RESET + vt)
    out.append("  " + C.GRY + "autostart".ljust(16) + C.RESET
               + (C.GRN + "● on" + C.RESET if st["autostart"] else C.GRY + "○ off" + C.RESET)
               + C.GRY + "   (bridge runs hidden at login)" + C.RESET)
    if st["busy"]:
        out.append("  " + C.YEL + "⟳ " + st["busy"] + C.RESET)
    out.append(C.GRY + "║" + C.RESET)
    out.append(rule())

    # menu, grouped by what kind of action it is
    k = C.BOLD + C.WHI
    def key(kk):
        return k + "[" + kk + "]" + C.RESET

    g = C.GRY
    gs_ready = bool(mlai_kit and not mlai_kit.mlai_key())
    out.append("  " + g + "START    " + C.RESET
               + key("g") + " get started  "
               + (C.YEL + "— no ML AI key yet, run this first" + C.RESET if gs_ready
                  else C.GRY + "first-run setup: paste key → wire → test" + C.RESET))
    out.append("  " + g + "BRIDGE   " + C.RESET
               + key("1") + " start    " + key("2") + " stop    "
               + key("3") + " restart    " + key("4") + " autostart")
    out.append("  " + g + "CONNECT  " + C.RESET
               + key("5") + " connection info / copy    " + key("6") + " test end-to-end")
    out.append("  " + g + "SECRETS  " + C.RESET
               + key("7") + " keys (9router · ml.ai · bridge)    "
               + key("8") + " set ML.ai key")
    out.append("  " + g + "DIAGNOSE " + C.RESET
               + key("9") + " refresh    " + key("v") + " run verify.py    "
               + key("l") + " bridge.log")
    out.append("  " + g + "MOVE     " + C.RESET
               + key("e") + " export kit for another PC    " + key("a") + " usage snippets")
    out.append("  " + g + "         " + C.RESET + key("q") + " quit")
    out.append(box_bottom())
    out.append(C.GRY + "  v%s   %s" % (VERSION, HERE) + C.RESET)
    print("\n".join(out))


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------

def do_set_key():
    print()
    print(C.CYA + "  Set ML.ai API key" + C.RESET)
    print(C.GRY + "  get it from https://code.ml.ai → Settings → API keys" + C.RESET)
    print(C.GRY + "  stored in %s (config.json is not used for it)" % ENV_PATH + C.RESET)
    try:
        val = input("  mlai_sk_…: ").strip()
    except (EOFError, KeyboardInterrupt):
        return
    if not val:
        return
    if not val.startswith("mlai_sk_"):
        print(C.YEL + "  warning: keys normally start with mlai_sk_" + C.RESET)
        if input("  save anyway? [y/N] ").strip().lower() != "y":
            return
    print(C.GRY + "  validating against code.ml.ai…" + C.RESET)
    try:
        acct = http_json("https://code.ml.ai/api/account", val, timeout=20)
        who = (acct.get("member") or {}).get("email", "?")
        n = len(acct.get("workspaces") or [])
        print(C.GRN + "  ✓ valid — %s, %d workspace(s)" % (who, n) + C.RESET)
    except urllib.error.HTTPError as e:
        print(C.RED + "  ✗ rejected: HTTP %s %s" % (e.code, e.read().decode("utf-8", "replace")[:120]) + C.RESET)
        if input("  save anyway? [y/N] ").strip().lower() != "y":
            return
    except Exception as e:
        print(C.YEL + "  ! could not reach code.ml.ai (%s)" % type(e).__name__ + C.RESET)
        if input("  save anyway? [y/N] ").strip().lower() != "y":
            return
    save_key(val)
    print(C.GRN + "  saved to .env" + C.RESET)
    print(C.GRY + "  restart the bridge for it to take effect: [2] then [1]" + C.RESET)
    input(C.GRY + "  enter to continue…" + C.RESET)


def do_aliases():
    print()
    print(C.CYA + "  Aliases & usage" + C.RESET)
    st = S.snapshot()
    model = st["router_models"][0] if st["router_models"] else "mlai/code"
    for line in [
        "hermes chat -q \"hello\" -m %s" % model,
        "/model %s                      (inside a session)" % model,
        "hermes config set model.default %s" % model,
        "hermes config set model.aliases.mlai custom/%s" % model,
        "",
        "Any OpenAI CLI (Claude Code, Cursor, Cline):",
        "  base url : http://localhost:20128/v1",
        "  api key  : 9router dashboard key (sk-337…)",
        "  model    : %s" % model,
    ]:
        print("  " + C.WHI + line + C.RESET)
    input(C.GRY + "\n  enter to continue…" + C.RESET)


def do_log():
    print()
    print(C.CYA + "  bridge.log (tail)" + C.RESET)
    try:
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-18:]
        for l in lines:
            print("  " + C.GRY + l + C.RESET)
    except Exception:
        print("  " + C.GRY + "no log yet" + C.RESET)
    input(C.GRY + "\n  enter to continue…" + C.RESET)


def handle(ch):
    if ch in ("g", "G"):
        if mlai_kit:
            mlai_kit.get_started()
            refresh()
        return True
    if ch == "1":
        if S.bridge_auth_rejected:
            # the key was changed while this process kept the old one in memory
            print("\n  " + C.YEL + "restarting to pick up the new bridge key…" + C.RESET)
            stop_bridge()
            ok, msg = start_bridge()
            with S.lock:
                S.busy = ""
            if ok:
                refresh()
                ok = S.bridge_ok and not S.bridge_auth_rejected
                msg = "restarted — key accepted" if ok else "restarted but the key still mismatches"
        else:
            ok, msg = start_bridge()
            with S.lock:
                S.busy = ""
        print(("\n  " + (C.GRN if ok else C.RED) + msg + C.RESET))
        time.sleep(1.2)
    elif ch == "2":
        ok, msg = stop_bridge()
        print(("\n  " + (C.GRN if ok else C.RED) + msg + C.RESET))
        time.sleep(1.0)
        refresh()
    elif ch == "3":
        print("\n  " + C.GRY + "restarting the bridge…" + C.RESET)
        ok, msg = stop_bridge()
        ok2, msg2 = start_bridge()
        refresh()
        print(("  " + (C.GRN if ok2 and S.bridge_ok else C.RED)
               + ("✓ " if ok2 else "✗ ") + msg2 + C.RESET))
        time.sleep(1.2)
    elif ch == "4":
        ok, msg = set_autostart(not S.autostart)
        print(("\n  " + (C.GRN if ok else C.RED) + msg + C.RESET))
        time.sleep(1.0)
    elif ch == "5":
        if mlai_kit:
            mlai_kit.show_connection(load_cfg().get("port", 20129))
    elif ch == "6":
        run_test_bg()
    elif ch == "7":
        if mlai_kit:
            mlai_kit.show_keys()
    elif ch == "8":
        do_set_key()
    elif ch == "9":
        refresh_bg()
    elif ch in ("v", "V"):
        if S.verify_running:
            print("\n  " + C.YEL + "verify is already running…" + C.RESET)
            time.sleep(1.0)
        elif S.verify_text:
            print()
            print(C.CYA + "  last verify.py output" + C.RESET)
            for line in S.verify_text.splitlines():
                col = C.GRN if line.startswith("PASS") else (C.RED if line.startswith("FAIL") else C.GRY)
                print("  " + col + line + C.RESET)
            if input(C.GRY + "\n  run it again? [y/N] " + C.RESET).strip().lower() == "y":
                run_verify_bg()
        else:
            run_verify_bg()
    elif ch in ("a", "A"):
        do_aliases()
    elif ch in ("l", "L"):
        do_log()
    elif ch in ("e", "E"):
        if mlai_kit:
            mlai_kit.show_export()
    elif ch in ("q", "Q", "\x1b"):
        return False
    return True


def main():
    enable_ansi()
    try:                      # never leave the window blank
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    if CHECK_ONLY:
        os.execv(sys.executable, [sys.executable, str(VERIFY)])
    # paint something immediately — the probes below touch the network
    clear()
    print()
    print(box_top("bridge console"))
    print(C.GRY + "║" + C.RESET)
    print("  " + C.YEL + "⟳ connecting to bridge, 9router and code.ml.ai…" + C.RESET)
    print(C.GRY + "║" + C.RESET)
    print(box_bottom())
    sys.stdout.flush()

    # fresh machine: no ML AI Code key yet — walk the user through it
    if mlai_kit and not mlai_kit.mlai_key() and not SHOW_STATUS_ONLY:
        clear()
        mlai_kit.get_started()
        input(C.GRY + "\n  enter to open the console…" + C.RESET)
        clear()

    refresh()
    if SHOW_STATUS_ONLY:
        if not S.bridge_ok:
            start_bridge()
        render()
        return 0
    # one click should be enough: bring the bridge up if it is down
    if not S.bridge_ok:
        threading.Thread(target=start_bridge, daemon=True).start()

    # msvcrt only works on a real console; when stdin is piped, fall back to
    # line input so the TUI stays scriptable (and testable)
    interactive_tty = False
    try:
        interactive_tty = sys.stdin.isatty()
    except Exception:
        pass
    if IS_WIN and interactive_tty:
        import msvcrt
    else:
        msvcrt = None

    refresh_bg()
    last = 0.0
    try:
        while True:
            clear()
            print()
            render()
            if time.time() - last > 20:
                refresh_bg()
                last = time.time()
            if msvcrt:
                t0 = time.time()
                ch = None
                while time.time() - t0 < 0.4:
                    if msvcrt.kbhit():
                        ch = msvcrt.getwch()
                        break
                    time.sleep(0.05)
                if ch:
                    if ch in ("\x00", "\xe0"):   # arrow/function key prefix
                        msvcrt.getwch()
                        continue
                    if not handle(ch):
                        break
            else:
                try:
                    ch = input("  > ").strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not handle(ch):
                    break
    except KeyboardInterrupt:
        pass
    clear()
    print("\n  " + C.GRY + "bridge keeps running in the background." + C.RESET + "\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except SystemExit:
        raise
    except BaseException:
        import traceback
        tb = traceback.format_exc()
        try:
            with open(HERE / "crash.log", "a", encoding="utf-8") as fh:
                fh.write("\n" + time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + tb)
        except Exception:
            pass
        print(tb)
        print(C.RED + "  crash written to " + str(HERE / "crash.log") + C.RESET)
        try:
            input("  press enter to close…")
        except Exception:
            pass
        sys.exit(1)
