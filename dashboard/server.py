#!/usr/bin/env python3
"""Capsule local dashboard (dry-run only).

Serves a single-page UI on 127.0.0.1:8791 (or the next free port). Reads data/decisions.jsonl and can
trigger exactly one dry-run cycle via the project venv (`bin/python` or `Scripts/python.exe`) running `desk.py --pump` (subprocess; desk.py never places
orders, live_order is always false). This server has no trading code and never sends orders.

Watchlist add/remove and one paper watch pass stay on localhost. They append Decisions
and paper marks. They do not place orders.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# `python dashboard/server.py` puts dashboard/ on sys.path, not the repo root.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scoreboard
import watchlist
LOG = ROOT / "data" / "decisions.jsonl"
# Fixed argv. The page cannot swap in another desk flag.
WATCH_ARGV = ["desk.py", "--watch", "--passes", "1"]


def resolve_python() -> Path:
    """Prefer the project venv (Unix or Windows), else this interpreter."""
    for rel in (".venv/bin/python", ".venv/Scripts/python.exe"):
        candidate = ROOT / rel
        if candidate.exists():
            return candidate
    return Path(sys.executable)


PY = resolve_python()
HTML = Path(__file__).resolve().parent / "index.html"
HOST = "127.0.0.1"
BASE_PORT = int(os.environ.get("CAPSULE_DASH_PORT") or os.environ.get("JEV_DASH_PORT") or "8791")
RUN_LOCK = threading.Lock()
RUN_TIMEOUT_S = 90


def _redact(text: str) -> str:
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    return text.replace(key, "[redacted]") if key else text


def log_path() -> Path:
    override = os.environ.get("CAPSULE_DATA_DIR", "").strip()
    base = Path(override) if override else ROOT / "data"
    return base / "decisions.jsonl"


def read_rows(n: int | None = None) -> list[dict]:
    path = log_path()
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-n:] if n else rows


def _intent(row: dict) -> dict:
    return row.get("intent") or row.get("decision") or {}


def dashboard_row(row: dict) -> dict:
    """Expose v1 Decision fields plus the legacy `decision` view the UI already reads."""
    out = dict(row)
    intent = _intent(out)
    gates = out.get("gates")
    if isinstance(gates, list):
        gate_map = {g.get("name"): bool(g.get("passed")) for g in gates if isinstance(g, dict)}
    else:
        gate_map = (out.get("decision") or {}).get("gates") or {}
    out.setdefault("decision", {
        "action": intent.get("action"),
        "reason": intent.get("reason"),
        "risk_usd": intent.get("risk_usd"),
        "take_usd": intent.get("take_usd"),
        "gates": gate_map,
    })
    if "judgment_ms" in out and "jev_ms" not in out:
        out["jev_ms"] = out["judgment_ms"]
    return out


def summarize(row: dict) -> dict:
    snap = row.get("snapshot") or {}
    coin = snap.get("coin") or {}
    dec = _intent(row)
    j = row.get("judgment") or {}
    return {
        "ts": row.get("ts"),
        "symbol": snap.get("symbol"),
        "name": coin.get("name"),
        "mint": coin.get("mint") or snap.get("mint") or row.get("mint"),
        "mid_usd": (snap.get("price") or {}).get("last_usd") or (snap.get("price") or {}).get("mid"),
        "mcap_usd": coin.get("market_cap_usd"),
        "regime": j.get("regime"),
        "direction": j.get("direction"),
        "error": j.get("error") or snap.get("fetch_error"),
        "action": dec.get("action"),
        "reason": dec.get("reason"),
        "elapsed_ms": row.get("elapsed_ms"),
        "live_order": bool(row.get("live_order", False)),
    }


def run_watch_pass() -> dict:
    """One paper refresh of the saved watchlist. Same lock as a single dry-run."""
    if not RUN_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "a dry-run cycle is already running", "live_order": False}
    try:
        before = len(read_rows())
        t0 = time.time()
        try:
            p = subprocess.run(
                [str(PY), *WATCH_ARGV],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=RUN_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": f"desk.py --watch timed out after {RUN_TIMEOUT_S}s",
                "live_order": False,
            }
        added = read_rows()[before:]
        out = {
            "ok": p.returncode == 0,
            "returncode": p.returncode,
            "wall_ms": round((time.time() - t0) * 1000, 1),
            "appended": len(added),
            "live_order": any(bool(row.get("live_order", False)) for row in added),
        }
        if p.returncode != 0:
            out["stderr_tail"] = _redact(p.stderr[-1500:])
        return out
    finally:
        RUN_LOCK.release()


def run_dry_cycle() -> dict:
    if not RUN_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "a dry-run cycle is already running"}
    try:
        before = len(read_rows())
        t0 = time.time()
        try:
            p = subprocess.run([str(PY), "desk.py", "--pump"], cwd=str(ROOT), capture_output=True,
                               text=True, timeout=RUN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"desk.py timed out after {RUN_TIMEOUT_S}s"}
        rows = read_rows()
        out = {"ok": p.returncode == 0 and len(rows) > before, "returncode": p.returncode,
               "wall_ms": round((time.time() - t0) * 1000, 1)}
        if p.returncode != 0:
            out["stderr_tail"] = _redact(p.stderr[-1500:])
        if rows and len(rows) > before:
            out["live_order"] = bool(rows[-1].get("live_order", False))
        return out
    finally:
        RUN_LOCK.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "Capsule/1.0"

    def log_message(self, fmt, *args):  # quiet, no headers/env
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def _origin_ok(self) -> bool:
        # Same-origin guard: only accept requests from this local page.
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return origin.startswith(f"http://{HOST}:") or origin.startswith("http://localhost:")

    def _body_json(self) -> dict | None:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if n < 0 or n > 4096:
            return None
        if n == 0:
            return {}
        try:
            data = json.loads(self.rfile.read(n).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, HTML.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/latest":
            rows = read_rows(1)
            row = dashboard_row(rows[-1]) if rows else None
            self._json({"row": row, "running": RUN_LOCK.locked()})
        elif path == "/api/logs":
            self._json({"rows": [summarize(r) for r in reversed(read_rows(10))]})
        elif path == "/api/watchlist":
            try:
                payload = {"mints": watchlist.load_watchlist()["mints"], "live_order": False}
            except ValueError as e:
                return self._json({"ok": False, "error": str(e), "live_order": False}, 500)
            self._json(payload)
        elif path == "/api/scoreboard":
            try:
                self._json(scoreboard.list_marks())
            except ValueError as e:
                self._json({"ok": False, "error": str(e), "live_order": False}, 500)
        elif path == "/healthz":
            self._json({"ok": True, "mode": "dry_run", "live_order": False})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/run":
            if not self._origin_ok():
                return self._json({"ok": False, "error": "bad origin"}, 403)
            res = run_dry_cycle()
            self._json(res, 200 if res.get("ok") else 500)
        elif path == "/api/watchlist":
            if not self._origin_ok():
                return self._json({"ok": False, "error": "bad origin", "live_order": False}, 403)
            body = self._body_json()
            if body is None:
                return self._json({"ok": False, "error": "expected a json object", "live_order": False}, 400)
            op = body.get("op")
            mint = body.get("mint") or ""
            try:
                if op == "add":
                    result = watchlist.add_mint(str(mint))
                elif op == "remove":
                    result = watchlist.remove_mint(str(mint))
                else:
                    return self._json({"ok": False, "error": "op must be add or remove", "live_order": False}, 400)
            except ValueError as e:
                return self._json({"ok": False, "error": str(e), "live_order": False}, 500)
            self._json(result, 200 if result.get("ok") else 400)
        elif path == "/api/watch":
            if not self._origin_ok():
                return self._json({"ok": False, "error": "bad origin", "live_order": False}, 403)
            res = run_watch_pass()
            self._json(res, 200 if res.get("ok") else 500)
        else:
            self._json({"error": "not found"}, 404)


def free_port(start: int, tries: int = 30) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((HOST, port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"no free port in {start}..{start + tries - 1}")


def main():
    port = free_port(BASE_PORT)
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer((HOST, port), Handler)
    url = f"http://{HOST}:{port}/"
    (Path(__file__).resolve().parent / "url.txt").write_text(url + "\n")
    print(f"Capsule dashboard (dry-run) at {url}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
