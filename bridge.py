#!/usr/bin/env python3
"""
mlai-bridge - OpenAI-compatible shim for ML AI Code (code.ml.ai)

ML AI Code exposes no OpenAI-compatible inference API. Its private RPC is:

    POST https://code.ml.ai/api/workspace
    Authorization: Bearer mlai_sk_...
    {"action": "run",    "token": "<workspace token>", "task": "..."}
    {"action": "status", "token": "<workspace token>"}   -> run state + final answer
    {"action": "stop",   "token": "<workspace token>"}

This bridge turns that agent RPC into /v1/chat/completions so 9router, Hermes,
Claude Code, Cursor and anything else OpenAI-compatible can talk to it.

Stdlib only. Python 3.9+.
"""

import json
import hmac
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
ENV_PATH = os.path.join(HERE, ".env")

DEFAULT_CONFIG = {
    "api_key": "",
    "port": 20129,
    "host": "127.0.0.1",
    "workspaces": [],
    "auto_create_workspace": True,
    "max_concurrent": 2,
    "poll_interval_s": 1.5,
    "run_timeout_s": 600,
    "slot_wait_s": 900,
    "model_alias": "code",
    "upstream": "https://code.ml.ai",
    "client_key": "",
}

# paths that never require the client key (readiness probes, monitoring)
PUBLIC_PATHS = ("/health", "/v1/health", "/")

_ctx = ssl.create_default_context()


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    # credential precedence: real env var > .env file > config.json
    key = os.environ.get("MLAI_API_KEY") or read_env_key()
    cfg["api_key"] = key or cfg.get("api_key") or ""
    return cfg


def read_env_key():
    """MLAI_API_KEY from the bridge's own .env file (preferred over config.json)."""
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("MLAI_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    except Exception as exc:  # pragma: no cover
        sys.stderr.write("[mlai-bridge] warn: could not read .env: %s\n" % exc)
    return ""


CFG = load_config()


# --------------------------------------------------------------------------
# upstream RPC
# --------------------------------------------------------------------------

class Upstream:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = cfg["upstream"].rstrip("/")
        self.key = cfg["api_key"]
        self._lock = threading.Lock()
        self._slots = threading.Semaphore(max(1, int(cfg.get("max_concurrent", 2))))
        self._ws_lock = threading.Lock()
        self._ws_busy = {}
        self._persist_lock = threading.Lock()

    def rpc(self, payload, timeout=60):
        req = urllib.request.Request(
            self.base + "/api/workspace",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        req.add_header("Authorization", "Bearer " + self.key)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as resp:
            raw = resp.read().decode("utf-8", "replace")
        return json.loads(raw)

    def get(self, path, timeout=60):
        req = urllib.request.Request(self.base + path)
        req.add_header("Authorization", "Bearer " + self.key)
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    # -- workspace pool ----------------------------------------------------
    def _save_workspaces(self):
        with self._persist_lock:
            try:
                disk = {}
                if os.path.exists(CONFIG_PATH):
                    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                        disk = json.load(fh)
                disk["workspaces"] = self.cfg["workspaces"]
                with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                    json.dump(disk, fh, indent=2)
            except Exception as exc:  # pragma: no cover
                log("warn: could not persist workspaces: %s" % exc)

    def workspace_tokens(self, refresh=False):
        with self._ws_lock:
            if self.cfg["workspaces"] and not refresh:
                return list(self.cfg["workspaces"])
        if not self.cfg.get("auto_create_workspace"):
            return list(self.cfg["workspaces"])
        try:
            acct = self.get("/api/account")
            tokens = [w["token"] for w in acct.get("workspaces", []) if w.get("token")]
        except Exception as exc:
            log("warn: /api/account failed: %s" % exc)
            tokens = []
        if not tokens:
            created = self.rpc({"action": "create"})
            tok = created.get("token")
            log("created workspace %s" % str(created.get("name"))[:32])
            if tok:
                tokens = [tok]
        with self._ws_lock:
            self.cfg["workspaces"] = tokens
        self._save_workspaces()
        return tokens

    def acquire_workspace(self, slots=1):
        """Pick a workspace that is not locally busy."""
        deadline = time.time() + float(self.cfg.get("slot_wait_s", 900))
        while True:
            for tok in self.workspace_tokens():
                with self._ws_lock:
                    if not self._ws_busy.get(tok):
                        self._ws_busy[tok] = True
                        return tok
            if time.time() > deadline:
                raise TimeoutError("no free ML AI workspace slot")
            time.sleep(1.0)

    def release_workspace(self, tok):
        with self._ws_lock:
            self._ws_busy[tok] = False


UP = Upstream(CFG)


def log(msg):
    sys.stderr.write("[mlai-bridge] %s\n" % msg)
    sys.stderr.flush()


# --------------------------------------------------------------------------
# task plumbing
# --------------------------------------------------------------------------

TERMINAL = ("completed", "failed", "stopped", "error", "cancelled", "interrupted")


def self_workspaces():
    """Every workspace token we know about (used to clear stuck runs)."""
    try:
        return UP.workspace_tokens()
    except Exception:
        return list(CFG.get("workspaces") or [])


def _as_text(content):
    if isinstance(content, list):  # multimodal -> keep the text parts
        return " ".join(
            p.get("text", "") for p in content if isinstance(p, dict)
        )
    return content or ""


def messages_to_task(messages, model):
    """Flatten an OpenAI message list into one agent task string.

    ML AI Code rejects tasks over 30,000 characters, and coding CLIs send
    enormous system prompts. So the final user turn is protected first and
    surrounding context is dropped oldest-first until it fits.
    """
    limit = int(CFG.get("max_task_chars", 28000))
    system = [_as_text(m.get("content")) for m in messages if m.get("role") == "system"]
    convo = [m for m in messages if m.get("role") != "system"]

    last_user = ""
    for m in reversed(convo):
        if m.get("role") == "user":
            last_user = _as_text(m.get("content"))
            break
    if not last_user:
        last_user = _as_text(convo[-1].get("content")) if convo else ""

    head = ("You are answering through an API bridge. Treat the dialogue below "
            "as the conversation so far and reply to the final USER turn only.\n\n")

    # newest-first history, minus the final user turn
    history = []
    for m in convo[:-1] if convo and convo[-1].get("role") == "user" else convo:
        history.append("%s: %s" % (m.get("role", "user").upper(),
                                   _as_text(m.get("content"))))

    def assemble(sys_part, hist, req):
        parts = []
        if sys_part:
            parts.append("[System instructions]\n" + sys_part)
        if hist:
            parts.append(head + "\n".join(hist))
        parts.append("[Request]\n" + req)
        return "\n\n".join(parts).strip()

    sys_txt = " ".join(system).strip()
    # 1) the ideal: everything
    body = assemble(sys_txt, history, last_user)
    if len(body) <= limit:
        return body

    # 2) drop the system prompt (it is the CLI's scaffolding, not the ask)
    body = assemble("", history, last_user)
    if len(body) <= limit:
        return body

    # 3) keep as much recent history as fits, newest first
    kept = []
    for line in reversed(history):
        trial = assemble("", list(reversed(kept + [line])), last_user)
        if len(trial) > limit:
            break
        kept.append(line)
    body = assemble("", list(reversed(kept)), last_user)
    if len(body) <= limit:
        return body

    # 4) the request alone is oversized: truncate its head
    room = limit - len("[Request]\n") - 40
    return "[Request]\n(...truncated...)\n" + last_user[-room:]


def _usage_of(state, result):
    """Usage lives in a few places depending on how the run ended."""
    for src in (result, state.get("status") or {}, state):
        u = src.get("usage")
        if isinstance(u, dict) and (u.get("prompt") or u.get("completion")):
            return u
    return {}


def extract_answer(state):
    status = state.get("status") or {}
    result = status.get("result") or {}
    usage = _usage_of(state, result)
    text = result.get("submission")
    if text:
        return text, usage
    # fall back to the newest answered turn
    for entry in reversed(state.get("conversation") or []):
        if entry.get("answer"):
            return entry["answer"], usage
    # last resort: assistant-ish events
    chunks = []
    for ev in state.get("events") or []:
        if ev.get("type") in ("message", "final", "answer") and ev.get("content"):
            chunks.append(str(ev["content"]))
    if chunks:
        return chunks[-1], usage
    return "", usage


def run_task(task_text, model, timeout_s=None):
    timeout_s = float(timeout_s or CFG.get("run_timeout_s", 600))
    poll = float(CFG.get("poll_interval_s", 1.5))
    token = UP.acquire_workspace()
    try:
        # wait for a global slot (active_task_limit is account wide)
        start = time.time()
        cleared = False
        while True:
            try:
                UP.rpc({"action": "run", "token": token, "task": task_text})
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                if exc.code == 429 and "active_task_limit" in body:
                    # the account-wide slots are full: try to clear stuck runs once
                    if not cleared:
                        cleared = True
                        for other in self_workspaces():
                            try:
                                UP.rpc({"action": "stop", "token": other})
                            except Exception:
                                pass
                        log("hit active_task_limit for account; issued stop to free slots")
                    if time.time() - start > timeout_s:
                        raise TimeoutError("account concurrency limit never freed")
                    time.sleep(3.0)
                    continue
                if exc.code == 409 and "run_locked" in body:
                    UP.release_workspace(token)
                    token = UP.acquire_workspace()
                    continue
                raise RuntimeError("ml.ai run failed: %s %s" % (exc.code, body[:300]))

        deadline = time.time() + timeout_s
        state = {}
        while time.time() < deadline:
            try:
                state = UP.rpc({"action": "status", "token": token}, timeout=90)
            except Exception as exc:
                log("status poll error: %s" % exc)
                time.sleep(poll)
                continue
            st = ((state.get("status") or {}).get("state") or "").lower()
            if st in TERMINAL:
                text, usage = extract_answer(state)
                if st != "completed" and not text:
                    err = (state.get("status") or {}).get("result", {}).get("error")
                    raise RuntimeError("ml.ai run ended as %s%s" % (st, (": " + str(err)) if err else ""))
                if not text:
                    raise RuntimeError("ml.ai run completed with no answer text")
                return text, usage, state
            time.sleep(poll)
        raise TimeoutError("ml.ai run exceeded %ss" % int(timeout_s))
    finally:
        UP.release_workspace(token)


# --------------------------------------------------------------------------
# OpenAI shape
# --------------------------------------------------------------------------

def model_list():
    alias = CFG.get("model_alias", "code")
    extra = CFG.get("extra_models") or ["agent", "opus-5.5r", "pixis-opus"]
    out = [alias]
    for m in extra:
        if m not in out:
            out.append(m)
    return out


def completion_payload(model, text, usage, stream):
    created = int(time.time())
    cid = "chatcmpl-mlai-%d" % created
    pt = int((usage or {}).get("prompt") or 0)
    ct = int((usage or {}).get("completion") or 0)
    if stream:
        head = {
            "id": cid, "object": "chat.completion.chunk", "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        body = {
            "id": cid, "object": "chat.completion.chunk", "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        }
        tail = {
            "id": cid, "object": "chat.completion.chunk", "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct,
                      "total_tokens": pt + ct},
        }
        return [head, body, tail]
    return {
        "id": cid, "object": "chat.completion", "created": created, "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
    }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mlai-bridge/1.0"

    # -- auth -------------------------------------------------------------
    def _presented_key(self):
        """Accept the key the way OpenAI-compatible clients send it."""
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        for name in ("x-api-key", "api-key", "apikey", "x-goog-api-key"):
            v = self.headers.get(name)
            if v:
                return v.strip()
        return ""

    def _authorized(self):
        """True when the request may proceed. Empty client_key = open, loopback only."""
        want = (CFG.get("client_key") or "").strip()
        if not want:
            return True
        got = self._presented_key()
        if got and hmac.compare_digest(got, want):
            return True
        log("rejected request from %s: bad or missing API key" % self.address_string())
        self._send(401, {"error": {
            "message": "Invalid API key. Send it as 'Authorization: Bearer <key>' "
                       "or 'x-api-key'. The key is in mlai-bridge/config.json "
                       "(see the console, menu [9]).",
            "type": "authentication_error",
            "code": "invalid_api_key"}})
        return False

    def log_message(self, fmt, *args):
        log("%s - %s" % (self.address_string(), fmt % args))

    # -- helpers ----------------------------------------------------------
    def _send(self, code, obj, ctype="application/json"):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(raw)

    def _sse_start(self):
        # no Content-Length is possible for a stream, so the end of the body
        # must be signalled by closing the connection
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _sse_chunk(self, obj):
        self.wfile.write(b"data: " + json.dumps(obj).encode("utf-8") + b"\n\n")
        self.wfile.flush()

    def _sse_done(self):
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8", "replace"))

    # -- routes -----------------------------------------------------------
    def do_OPTIONS(self):
        self._send(204, {})

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path not in PUBLIC_PATHS and not self._authorized():
            return
        if path in ("/v1/models", "/models"):
            now = int(time.time())
            self._send(200, {
                "object": "list",
                "data": [{"id": m, "object": "model", "created": now,
                          "owned_by": "ml-ai-code"} for m in model_list()],
            })
            return
        if path in ("/health", "/", "/v1/health"):
            self._send(200, {"ok": True, "upstream": CFG["upstream"],
                             "workspaces": len(CFG.get("workspaces") or []),
                             "requires_api_key": bool((CFG.get("client_key") or "").strip())})
            return
        self._send(404, {"error": {"message": "not found", "type": "invalid_request_error"}})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if not self._authorized():
            return
        if path not in ("/v1/chat/completions", "/chat/completions", "/v1/completions"):
            self._send(404, {"error": {"message": "not found", "type": "invalid_request_error"}})
            return
        try:
            body = self._json_body()
        except Exception as exc:
            self._send(400, {"error": {"message": "bad json: %s" % exc,
                                       "type": "invalid_request_error"}})
            return

        model = body.get("model") or CFG.get("model_alias", "mlai-code")
        stream = bool(body.get("stream"))
        messages = body.get("messages")
        if not messages:
            prompt = body.get("prompt") or ""
            messages = [{"role": "user", "content": prompt}]

        task = messages_to_task(messages, model)
        log("task (model=%s, stream=%s, %d chars)" % (model, stream, len(task)))

        try:
            text, usage, _state = run_task(task, model,
                                           timeout_s=body.get("timeout_s"))
        except Exception as exc:
            self._send(502, {"error": {"message": "ml.ai bridge: %s" % exc,
                                       "type": "upstream_error"}})
            return

        payload = completion_payload(model, text, usage, stream)
        if stream:
            self._sse_start()
            for chunk in payload:
                self._sse_chunk(chunk)
            self._sse_done()
        else:
            self._send(200, payload)


def main():
    if not CFG.get("api_key"):
        log("ERROR: no api_key. Set MLAI_API_KEY or put it in config.json")
        sys.exit(2)
    host, port = CFG["host"], int(CFG["port"])
    log("listening on http://%s:%d/v1  (upstream %s)" % (host, port, CFG["upstream"]))
    if (CFG.get("client_key") or "").strip():
        log("client API key REQUIRED on /v1/* (health stays open)")
    else:
        log("no client API key set - open on loopback. Set one with the console, menu [9]")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
