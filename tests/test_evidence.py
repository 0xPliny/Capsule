"""Evidence fields, proximity, and fail-closed intent. Offline."""
from __future__ import annotations

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import dashboard.server as dash_server
from desk import SCHEMA_V, build_record, canonical_hash, run_cycle
from gates import decide
from sources import DemoSource, FetchError, PumpSource, STALE_AFTER_S, _get_json

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE_KEYS = (
    "field", "threshold", "value", "passed", "distance", "flip", "reason",
    "endpoint", "source_id", "snapshot_hash", "fetched_at", "decided_at",
)


def test_schema_v2_stamps_evidence(demo_snapshot, passing_judgment):
    decided = decide(demo_snapshot, passing_judgment)
    row = build_record(
        source="demo", snapshot=demo_snapshot, judgment=passing_judgment,
        decided=decided, elapsed_ms=1.0, fetch_ms=0.2, run_id="evidence",
    )
    assert row["schema_v"] == SCHEMA_V == 2
    assert row["data_status"] == "ok"
    assert row["intent"]["confident"] is True
    assert row["snapshot_hash"] == canonical_hash(demo_snapshot)
    assert len(row["snapshot_hash"]) == 64
    assert row["live_order"] is False
    for gate in row["gates"]:
        for key in EVIDENCE_KEYS:
            assert key in gate, key
        assert gate["snapshot_hash"] == row["snapshot_hash"]
        assert gate["passed"] is True
        assert gate["fetched_at"] == demo_snapshot["ts"]
        assert gate["decided_at"] == row["ts"]


def test_hash_ignores_key_order():
    a = {"z": 1, "a": {"b": 2}}
    b = {"a": {"b": 2}, "z": 1}
    assert canonical_hash(a) == canonical_hash(b)
    assert canonical_hash(a) != canonical_hash({"z": 2, "a": {"b": 2}})


def test_proximity_on_paper_long(demo_snapshot, passing_judgment):
    out = decide(demo_snapshot, passing_judgment)
    by_name = {g["name"]: g for g in out["gates"]}
    toxic = by_name["toxic_flow"]
    assert toxic["distance"] == 0.35
    assert toxic["reason"] == "toxic_flow 0.2 < 0.55"
    assert "margin 0.35" in toxic["flip"]
    assert "verdict flips if toxic_flow >= 0.55" in toxic["flip"]
    quote = by_name["quote_env"]
    assert quote["distance"] == 0.15
    assert "verdict flips if quote_env < 0.55" in quote["flip"]
    inv = by_name["inventory"]
    assert inv["distance"] == 1.3
    assert inv["reason"] == "inventory 0.2 < 1.5"
    liq = by_name["liquidity"]
    assert liq["distance"] == 0.5
    assert liq["reason"] == "liquidity 1.5 >= 1"
    assert all("blocking the verdict" not in g["flip"] for g in out["gates"])


def test_every_gate_keeps_a_reason_past_the_first_fail(demo_snapshot, passing_judgment):
    judgment = dict(passing_judgment, toxic_flow=0.9, quote_env=0.2, liquidity=0.4)
    out = decide(demo_snapshot, judgment)
    assert out["intent"]["action"] == "hold"
    assert out["intent"]["confident"] is True
    assert "toxic flow" in out["intent"]["reason"]
    by_name = {g["name"]: g for g in out["gates"]}
    assert by_name["toxic_flow"]["passed"] is False
    assert by_name["quote_env"]["passed"] is False
    assert by_name["liquidity"]["passed"] is False
    assert by_name["inventory"]["passed"] is True
    assert "blocking the verdict" in by_name["toxic_flow"]["flip"]
    assert "short by 0.35" in by_name["toxic_flow"]["flip"]
    assert "earlier block is toxic_flow" in by_name["quote_env"]["flip"]
    assert "earlier block is toxic_flow" in by_name["liquidity"]["flip"]
    assert "verdict already blocked by toxic_flow" in by_name["inventory"]["flip"]
    assert all(g["reason"] for g in out["gates"])


def test_flatten_label_distance_still_recorded(demo_snapshot, passing_judgment):
    out = decide(demo_snapshot, dict(passing_judgment, inventory=0.2, inventory_label="flatten"))
    gate = next(g for g in out["gates"] if g["name"] == "inventory")
    assert gate["passed"] is False
    assert gate["distance"] == 1.3
    assert gate["reason"] == "inventory_label is flatten"
    assert "blocking the verdict" in gate["flip"]
    assert "inventory_label is not flatten" in gate["flip"]
    assert out["intent"]["confident"] is True
    assert out["intent"]["action"] == "flatten_or_hold"


def test_fetch_timeout_blocks_confident_intent(demo_snapshot, passing_judgment):
    endpoint = "https://frontend-api-v3.pump.fun/coins-v2/abc"
    snap = dict(demo_snapshot)
    snap["source_id"] = "pump"
    snap["fetch_error"] = f"timeout: {endpoint}: timed out"
    snap["fetch_failure"] = {"kind": "timeout", "endpoint": endpoint, "detail": "timed out"}
    out = decide(snap, passing_judgment)
    assert out["data_status"] == "insufficient"
    assert out["intent"]["action"] == "hold"
    assert out["intent"]["confident"] is False
    assert out["intent"]["risk_usd"] == 0
    assert not str(out["intent"]["action"]).startswith("paper_")
    assert out["intent"]["reason"].startswith("insufficient data:")
    assert "source=pump" in out["intent"]["reason"]
    assert endpoint in out["intent"]["reason"]
    assert "timeout" in out["intent"]["reason"]
    assert all(g["endpoint"] == endpoint for g in out["gates"])
    assert all(g["flip"].startswith("no confident intent") for g in out["gates"])


def test_stale_names_endpoint(demo_snapshot, passing_judgment):
    endpoint = "https://frontend-api-v3.pump.fun/coins-v2/old"
    snap = dict(demo_snapshot)
    snap["source_id"] = "mint"
    snap["freshness"] = {
        "stale": True,
        "stale_after_s": STALE_AFTER_S,
        "last_trade_age_s": 5000,
        "endpoint": endpoint,
        "detail": f"stale last_trade_age_s=5000 > {STALE_AFTER_S}",
    }
    out = decide(snap, passing_judgment)
    assert out["intent"]["confident"] is False
    assert "insufficient data:" in out["intent"]["reason"]
    assert "source=mint" in out["intent"]["reason"]
    assert endpoint in out["intent"]["reason"]
    assert "stale" in out["intent"]["reason"]


def test_missing_gate_field_is_not_inventory_pressure(demo_snapshot, passing_judgment):
    judgment = dict(passing_judgment)
    judgment["inventory"] = None
    out = decide(demo_snapshot, judgment)
    assert out["intent"]["action"] == "hold"
    assert out["intent"]["confident"] is False
    assert out["intent"]["action"] != "flatten_or_hold"
    assert "insufficient data:" in out["intent"]["reason"]
    assert "missing inventory" in out["intent"]["reason"]
    by_name = {g["name"]: g for g in out["gates"]}
    assert by_name["inventory"]["passed"] is False
    assert by_name["inventory"]["reason"] == "inventory missing"
    assert by_name["toxic_flow"]["passed"] is True
    assert by_name["liquidity"]["passed"] is True


def test_missing_market_field_names_source(demo_snapshot, passing_judgment):
    snap = dict(demo_snapshot)
    snap["source_id"] = "pump"
    snap["endpoints"] = [{"name": "trades", "url": "https://swap-api.pump.fun/v2/coins/abc/trades", "ok": True}]
    snap["missing_fields"] = ["price.last_usd", "trades"]
    out = decide(snap, passing_judgment)
    assert out["intent"]["confident"] is False
    assert "source=pump" in out["intent"]["reason"]
    assert "price.last_usd" in out["intent"]["reason"]
    assert "trades" in out["intent"]["reason"]
    assert "swap-api.pump.fun" in out["intent"]["reason"]


def _coin(now_ms, age_s):
    ts = None if age_s is None else now_ms - age_s * 1000
    return {
        "mint": "MintEvidence111",
        "symbol": "EVID",
        "name": "Evidence",
        "complete": False,
        "base_decimals": 6,
        "virtual_token_reserves": 0,
        "real_token_reserves": 0,
        "total_supply": 0,
        "created_timestamp": now_ms,
        "last_trade_timestamp": ts,
        "nsfw": False,
        "is_banned": False,
    }


def _fake_pump(monkeypatch, coin, trades=None, candles=None, fail=None):
    trades = {"trades": [{
        "type": "buy", "priceUsd": "0.001", "amountUsd": "10",
        "timestamp": "2026-10-06T00:00:00Z", "userAddress": "abc",
    }]} if trades is None else trades

    def fake(url, timeout=10.0):
        if fail and fail["match"] in url:
            raise FetchError(fail["kind"], url, fail["detail"])
        if "coins-v2" in url:
            return coin
        if "/trades" in url:
            return trades
        if "/candles" in url:
            if candles == "http":
                raise FetchError("http", url, "HTTP 404")
            return candles or []
        raise AssertionError(url)

    monkeypatch.setattr("sources._get_json", fake)


def test_pump_timeout_names_coins_endpoint(monkeypatch, passing_judgment):
    _fake_pump(monkeypatch, _coin(time.time() * 1000, 1), fail={
        "match": "coins-v2", "kind": "timeout", "detail": "timed out",
    })
    snap, judgment = PumpSource("MintEvidence111").load()
    out = decide(snap, judgment)
    assert out["intent"]["confident"] is False
    assert "insufficient data:" in out["intent"]["reason"]
    assert "source=mint" in out["intent"]["reason"]
    assert "https://frontend-api-v3.pump.fun/coins-v2/MintEvidence111" in out["intent"]["reason"]
    assert "timeout" in out["intent"]["reason"]
    assert judgment is not None
    names = [e["name"] for e in snap["endpoints"]]
    assert names == ["coins-v2"]
    assert snap["endpoints"][0]["ok"] is False


def test_stale_pump_does_not_ask_for_judgment(monkeypatch):
    now_ms = time.time() * 1000
    _fake_pump(monkeypatch, _coin(now_ms, STALE_AFTER_S + 60))
    snap, judgment = PumpSource("MintEvidence111").load()
    assert snap["freshness"]["stale"] is True
    assert "coins-v2/MintEvidence111" in snap["freshness"]["endpoint"]
    out = decide(snap, judgment)
    assert out["intent"]["confident"] is False
    assert "stale" in out["intent"]["reason"]
    assert snap["freshness"]["endpoint"] in out["intent"]["reason"]


def test_fresh_pump_snapshot_can_still_pass(monkeypatch, passing_judgment):
    now_ms = time.time() * 1000
    _fake_pump(monkeypatch, _coin(now_ms, 5))
    snap, judgment = PumpSource("MintEvidence111").load()
    assert judgment is None
    assert snap["freshness"]["stale"] is False
    assert snap["missing_fields"] == []
    assert {e["name"] for e in snap["endpoints"]} == {"coins-v2", "trades", "candles"}
    out = decide(snap, passing_judgment)
    assert out["intent"]["action"] == "paper_long"
    assert out["intent"]["confident"] is True
    assert out["data_status"] == "ok"


def test_missing_trades_is_insufficient(monkeypatch):
    now_ms = time.time() * 1000
    _fake_pump(monkeypatch, _coin(now_ms, 5), trades={"trades": []})
    snap, judgment = PumpSource("MintEvidence111").load()
    assert "trades" in snap["missing_fields"]
    out = decide(snap, judgment)
    assert out["intent"]["confident"] is False
    assert "missing trades" in out["intent"]["reason"] or "trades" in out["intent"]["reason"]


def test_candle_http_miss_is_recorded_not_blocking(monkeypatch, passing_judgment):
    now_ms = time.time() * 1000
    _fake_pump(monkeypatch, _coin(now_ms, 5), candles="http")
    snap, judgment = PumpSource("MintEvidence111").load()
    assert judgment is None
    candle = next(e for e in snap["endpoints"] if e["name"] == "candles")
    assert candle["ok"] is False
    assert candle["error"] == "http"
    out = decide(snap, passing_judgment)
    assert out["intent"]["confident"] is True
    assert out["intent"]["action"] == "paper_long"


def test_candle_timeout_fails_closed(monkeypatch):
    now_ms = time.time() * 1000
    _fake_pump(monkeypatch, _coin(now_ms, 5), fail={
        "match": "/candles", "kind": "timeout", "detail": "timed out",
    })
    snap, judgment = PumpSource("MintEvidence111").load()
    out = decide(snap, judgment)
    assert out["intent"]["confident"] is False
    assert "/candles" in out["intent"]["reason"]
    assert "timeout" in out["intent"]["reason"]


def test_get_json_timeout_kind():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]

    def hold():
        try:
            conn, _ = sock.accept()
            time.sleep(1.0)
            conn.close()
        except OSError:
            return

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    try:
        try:
            _get_json(f"http://127.0.0.1:{port}/coins-v2/x", timeout=0.2)
            raise AssertionError("expected timeout")
        except FetchError as e:
            assert e.kind == "timeout"
            assert f"127.0.0.1:{port}" in e.endpoint
    finally:
        sock.close()


def test_get_json_http_kind():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"error":"nope"}'
            self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        try:
            _get_json(f"http://127.0.0.1:{port}/coins-v2/x", timeout=2)
            raise AssertionError("expected http error")
        except FetchError as e:
            assert e.kind == "http"
            assert "HTTP 404" in e.detail
            assert f"127.0.0.1:{port}" in e.endpoint
    finally:
        httpd.shutdown()


def test_demo_cycle_is_confident_and_offline():
    row = run_cycle(DemoSource())
    assert row["schema_v"] == 2
    assert row["data_status"] == "ok"
    assert row["intent"]["action"] == "paper_long"
    assert row["intent"]["confident"] is True
    assert row["endpoints"][0]["name"] == "demo"
    assert row["snapshot_hash"] == canonical_hash(row["snapshot"])
    assert len(row["gates"]) == 4
    assert all(g["passed"] for g in row["gates"])


def test_dashboard_row_keeps_every_gate(demo_snapshot, passing_judgment):
    decided = decide(demo_snapshot, passing_judgment)
    row = build_record(
        source="demo", snapshot=demo_snapshot, judgment=passing_judgment,
        decided=decided, elapsed_ms=1, fetch_ms=0,
    )
    view = dash_server.dashboard_row(row)
    assert isinstance(view["gates"], list) and len(view["gates"]) == 4
    assert all(g.get("reason") and "flip" in g for g in view["gates"])
    assert view["decision"]["gates"]["inventory"] is True
    assert view["decision"]["gates"]["toxic_flow"] is True
    assert view["live_order"] is False


def test_ui_lists_every_gate_and_keeps_paper_skin():
    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert "DRY RUN · PAPER" in html
    assert "--bg:#0E1311" in html
    assert "--accent:#86efac" in html
    assert 'id="evidence"' in html
    assert "gateReason" in html
    assert "entries.map" in html
    assert "snapshot_hash" in html
    assert "insufficient data" in html
    assert "distance" in html
    server = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
    assert ".venv/bin/python" in server
    assert ".venv/Scripts/python.exe" in server
    assert 'HOST = "127.0.0.1"' in server


def test_run_bat_uses_venv_then_py_then_python():
    text = (ROOT / "run.bat").read_text(encoding="utf-8")
    assert r".venv\Scripts\python.exe" in text
    assert "CAPSULE_DASH_PORT" in text
    assert "8791" in text
    assert "py -3" in text
    assert "python" in text
    assert "dashboard\\server.py" in text or "dashboard/server.py" in text
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "run.bat" in readme
    assert "CAPSULE_DASH_PORT" in readme
