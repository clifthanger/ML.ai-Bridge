#!/usr/bin/env python3
"""End-to-end self-test: bridge -> 9router -> ML AI Code, plus streaming.

    python verify.py            # uses config.json
    python verify.py --quick    # only check /health and /v1/models
"""
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CFG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
PORT = CFG.get("port", 20129)
ROUTER = "http://localhost:20128/v1"
HERMES_ENV = Path.home() / "AppData/Local/hermes/.env"


def bridge_client_key():
    """The bridge's own incoming key, if it requires one."""
    if os.environ.get("MLAI_BRIDGE_KEY"):
        return os.environ["MLAI_BRIDGE_KEY"]
    try:
        for line in (HERE / ".env").read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("MLAI_BRIDGE_KEY="):
                return line.split("=", 1)[1].strip()
    except FileNotFoundError:
        pass
    return CFG.get("client_key") or ""

OK, BAD = "PASS", "FAIL"


def get(url, timeout=10, key=None):
    req = urllib.request.Request(url)
    if key:
        req.add_header("Authorization", "Bearer " + key)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def chat(base, key, model, stream=False, timeout=180):
    body = json.dumps({
        "model": model,
        "stream": stream,
        "messages": [{"role": "user",
                      "content": "Reply with exactly this token and nothing else: VERIFY-OK"}],
    }).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body)
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", "Bearer " + key)
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return time.time() - t0, raw


def main():
    quick = "--quick" in sys.argv
    results = []

    try:
        _, body = get("http://127.0.0.1:%d/health" % PORT)
        results.append((OK, "bridge /health", body[:80]))
    except Exception as exc:
        results.append((BAD, "bridge /health", "not running? %s" % exc))
        report(results)
        return 1

    bkey = bridge_client_key()
    try:
        _, body = get("http://127.0.0.1:%d/v1/models" % PORT, key=bkey)
        ids = [m["id"] for m in json.loads(body)["data"]]
        tag = "auth on" if bkey else "auth off"
        results.append((OK, "bridge /v1/models (%s)" % tag, ", ".join(ids)))
    except Exception as exc:
        results.append((BAD, "bridge /v1/models", str(exc)))

    if bkey:
        # the key must actually be enforced
        try:
            get("http://127.0.0.1:%d/v1/models" % PORT)
            results.append((BAD, "bridge rejects a wrong key", "got 200, auth not enforced"))
        except urllib.error.HTTPError as e:
            results.append((OK if e.code == 401 else BAD,
                            "bridge rejects a wrong key", "HTTP %s" % e.code))
        except Exception as exc:
            results.append((BAD, "bridge rejects a wrong key", str(exc)))

    if quick:
        report(results)
        return 0

    # --- router key from the Hermes env -----------------------------------
    router_key = ""
    if HERMES_ENV.exists():
        m = re.search(r"^HERMES_CUSTOM_LOCALHOST_20128_API_KEY=(.+)$",
                      HERMES_ENV.read_text(encoding="utf-8", errors="replace"), re.M)
        router_key = m.group(1).strip() if m else ""
    results.append((OK if router_key else BAD, "9router api key found", "len %d" % len(router_key)))

    # --- non-streaming through 9router ------------------------------------
    if router_key:
        try:
            el, raw = chat(ROUTER, router_key, "mlai/code")
            j = json.loads(raw)
            txt = j["choices"][0]["message"]["content"].strip()
            u = j.get("usage") or {}
            results.append((OK, "9router non-stream",
                            "%.1fs text=%r prompt=%s completion=%s"
                            % (el, txt[:30], u.get("prompt_tokens"), u.get("completion_tokens"))))
        except Exception as exc:
            results.append((BAD, "9router non-stream", str(exc)))

        # --- streaming through 9router -----------------------------------
        try:
            el, raw = chat(ROUTER, router_key, "mlai/code", stream=True)
            chunks = [l for l in raw.splitlines() if l.startswith("data: ") and l != "data: [DONE]"]
            got = "".join(
                json.loads(c[6:])["choices"][0].get("delta", {}).get("content") or ""
                for c in chunks)
            results.append((OK if got else BAD, "9router stream",
                            "%.1fs %d chunks text=%r" % (el, len(chunks), got[:30])))
        except Exception as exc:
            results.append((BAD, "9router stream", str(exc)))

        # --- 9router model list ------------------------------------------
        try:
            _, body = get(ROUTER + "/models", timeout=20)
            ids = [m["id"] for m in json.loads(body)["data"] if m["id"].startswith("mlai")]
            results.append((OK if ids else BAD, "9router mlai models", ", ".join(ids)))
        except Exception as exc:
            results.append((BAD, "9router mlai models", str(exc)))

    return report(results)


def report(results):
    width = max(len(r[1]) for r in results)
    for status, name, detail in results:
        print("%-4s  %-*s  %s" % (status, width, name, detail))
    bad = sum(1 for r in results if r[0] == BAD)
    print("\n%d passed, %d failed" % (len(results) - bad, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
