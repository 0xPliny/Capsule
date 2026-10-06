"""Phase 4 paper desk: watchlist, watch loop, scoreboard. Offline. live_order stays false."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import dashboard.server as dash_server
import scoreboard
import watchlist
from desk import watch_once
from gates import decide
from holders import sample_holder_rows

ROOT = Path(__file__).resolve().parent.parent
MINT = "So11111111111111111111111111111111111111112"
MINT_B = "11111111111111111111111111111111"
T0 = datetime(2026, 10, 6, tzinfo=timezone.utc)


def _iso(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


@pytest.fixture
def paper_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPSULE_DATA_DIR", str(tmp_path))
    return tmp_path


def _src(judgment, ts, price, direction=None, mint=MINT):
    class Src:
        name = "mint"

        def load(self):
            j = dict(judgment)
            if direction:
                j["direction"] = direction
            snap = {
                "ts": ts,
                "source": "mint",
                "source_id": "mint",
                "symbol": "PAP",
                "coin": {"mint": mint},
                "price": {"last_usd": price} if price is not None else {},
                "mode": "dry_run",
                "max_risk_usd": 200,
                "take_usd": 400,
                "endpoints": [{
                    "name": "trades",
                    "url": "https://swap-api.pump.fun/v2/coins/x/trades",
                    "ok": True,
                    "error": None,
                    "fetched_at": ts,
                }],
                # Independent rows so holder_cluster can finish OK. A missing
                # read still fail-closes; these paper marks need a real pass.
                "holders": {
                    "rows": sample_holder_rows(),
                    "supply": 100,
                    "endpoint": "offline:paper-holders",
                    "observed": True,
                    "sample": False,
                    "source_id": "mint",
                },
            }
            if price is None:
                snap["missing_fields"] = ["price.last_usd"]
            return snap, j

    return Src()


def _run(paper_dir, *args):
    env = {k: v for k, v in os.environ.items() if k != "TYPESAFE_API_KEY"}
    env["CAPSULE_DATA_DIR"] = str(paper_dir)
    return subprocess.run(
        [sys.executable, "desk.py", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_watchlist_add_remove_persists(paper_dir):
    added = watchlist.add_mint(MINT)
    assert added["ok"] is True and added["added"] is True
    assert added["live_order"] is False
    again = watchlist.add_mint(MINT)
    assert again["added"] is False
    assert [item["mint"] for item in again["mints"]] == [MINT]
    on_disk = json.loads((paper_dir / "watchlist.json").read_text(encoding="utf-8"))
    assert on_disk["mints"][0]["mint"] == MINT
    assert "live_order" not in on_disk or on_disk.get("live_order") is False
    removed = watchlist.remove_mint(MINT)
    assert removed["removed"] is True and removed["mints"] == []
    missing = watchlist.remove_mint(MINT_B)
    assert missing["ok"] is True and missing["removed"] is False
    assert watchlist.mints() == []


def test_watchlist_rejects_bad_mint_and_caps(paper_dir, monkeypatch):
    bad = watchlist.add_mint("not-a-mint")
    assert bad["ok"] is False
    assert bad["live_order"] is False
    assert not (paper_dir / "watchlist.json").exists()
    monkeypatch.setattr(watchlist, "MAX_MINTS", 1)
    assert watchlist.add_mint(MINT)["added"] is True
    capped = watchlist.add_mint(MINT_B)
    assert capped["ok"] is False
    assert capped["error"] == "watchlist holds 1 mints"
    assert [item["mint"] for item in watchlist.load_watchlist()["mints"]] == [MINT]


def test_watchlist_corrupt_file_is_not_wiped(paper_dir):
    path = paper_dir / "watchlist.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable"):
        watchlist.load_watchlist()
    assert path.read_text(encoding="utf-8") == "{"


def test_cli_watchlist_roundtrip(paper_dir):
    add = _run(paper_dir, "--watch-add", MINT)
    assert add.returncode == 0, add.stderr
    assert '"live_order": false' in add.stdout
    listed = _run(paper_dir, "--watch-list")
    assert listed.returncode == 0
    assert MINT in listed.stdout
    assert '"live_order": false' in listed.stdout
    gone = _run(paper_dir, "--watch-remove", MINT)
    assert gone.returncode == 0
    assert '"removed": true' in gone.stdout
    bad = _run(paper_dir, "--watch-add", "nope")
    assert bad.returncode == 2
    empty = _run(paper_dir, "--watch", "--passes", "1", "--interval", "0")
    assert empty.returncode == 0, empty.stderr
    assert "watchlist empty" in empty.stdout
    assert "live_order false" in empty.stdout
    assert not (paper_dir / "decisions.jsonl").exists()


def test_cli_demo_watch_is_offline_and_paper(paper_dir):
    p = _run(paper_dir, "--demo", "--watch", "--passes", "1", "--interval", "0")
    assert p.returncode == 0, p.stderr
    assert '"schema_v": 2' in p.stdout
    assert '"live_order": false' in p.stdout
    assert "paper_long" in p.stdout
    row = json.loads((paper_dir / "decisions.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert row["schema_v"] == 2
    assert row["live_order"] is False
    board = json.loads((paper_dir / "scoreboard.json").read_text(encoding="utf-8"))
    assert board["marks"][0]["status"] == "open"
    assert board["marks"][0]["entry_px"] == 100.0
    assert board["marks"][0]["paper_delta"] is None
    assert board["marks"][0]["live_order"] is False


def test_evidence_roles_observed_derived_cited(demo_snapshot, passing_judgment):
    out = decide(demo_snapshot, passing_judgment)
    toxic = next(g for g in out["gates"] if g["name"] == "toxic_flow")
    assert toxic["roles"]["value"] == "observed"
    assert toxic["roles"]["distance"] == "derived"
    assert toxic["roles"]["threshold"] == "cited"
    assert toxic["roles"]["snapshot_hash"] == "insufficient"
    judgment = dict(passing_judgment)
    judgment["liquidity"] = None
    missing = decide(demo_snapshot, judgment)
    liq = next(g for g in missing["gates"] if g["name"] == "liquidity")
    assert liq["roles"]["value"] == "insufficient"
    assert liq["roles"]["distance"] == "insufficient"
    assert liq["roles"]["threshold"] == "cited"
    assert missing["intent"]["confident"] is False


def test_watch_loop_settles_paper_long_and_short(paper_dir, passing_judgment):
    rows = watch_once(
        [
            _src(passing_judgment, _iso(0), 100.0),
            _src(passing_judgment, _iso(4), 110.0),
        ],
        horizon_min=5,
    )
    assert all(row["schema_v"] == 2 and row["live_order"] is False for row in rows)
    assert all(row["_paper"]["live_order"] is False for row in rows)
    early = scoreboard.list_marks()
    assert early["live_order"] is False
    assert [m["status"] for m in early["marks"]] == ["open", "open"]
    assert all(m["paper_delta"] is None and m["live_order"] is False for m in early["marks"])
    toxic = next(g for g in rows[0]["gates"] if g["name"] == "toxic_flow")
    assert toxic["roles"]["value"] == "observed"
    assert toxic["roles"]["distance"] == "derived"
    assert toxic["roles"]["threshold"] == "cited"
    assert toxic["roles"]["snapshot_hash"] == "observed"
    assert toxic["roles"]["fetched_at"] == "observed"

    watch_once(
        [
            _src(passing_judgment, _iso(5), 110.0, direction="short"),
            _src(passing_judgment, _iso(10), 90.0, direction="short"),
        ],
        horizon_min=5,
    )
    marks = {m["entry_ts"]: m for m in scoreboard.list_marks()["marks"]}
    first = marks[_iso(0)]
    assert first["status"] == "marked"
    assert first["action"] == "paper_long"
    assert first["entry_px"] == 100.0
    assert first["exit_px"] == 110.0
    assert first["price_delta"] == 0.1
    assert first["paper_delta"] == 0.1
    assert first["live_order"] is False
    second = marks[_iso(4)]
    assert second["status"] == "marked"
    assert second["action"] == "paper_long"
    assert second["exit_px"] == 90.0
    assert second["live_order"] is False
    short = marks[_iso(5)]
    assert short["status"] == "marked"
    assert short["action"] == "paper_short"
    assert short["exit_px"] == 90.0
    assert short["price_delta"] == pytest.approx(-0.181818)
    assert short["paper_delta"] == pytest.approx(0.181818)
    assert short["live_order"] is False
    still_open = marks[_iso(10)]
    assert still_open["status"] == "open"
    assert still_open["paper_delta"] is None
    assert still_open["live_order"] is False


def test_hold_records_price_move_with_zero_paper_delta(paper_dir, passing_judgment):
    watch_once(
        [
            _src(passing_judgment, _iso(0), 100.0, direction="flat"),
            _src(passing_judgment, _iso(5), 110.0, direction="flat"),
        ],
        horizon_min=5,
    )
    mark = next(m for m in scoreboard.list_marks()["marks"] if m["entry_ts"] == _iso(0))
    assert mark["action"] == "hold"
    assert mark["status"] == "marked"
    assert mark["price_delta"] == 0.1
    assert mark["paper_delta"] == 0.0
    assert mark["live_order"] is False


def test_missing_price_stays_insufficient(paper_dir, passing_judgment):
    watch_once(
        [
            _src(passing_judgment, _iso(0), None),
            _src(passing_judgment, _iso(10), 110.0),
        ],
        horizon_min=5,
    )
    marks = scoreboard.list_marks()["marks"]
    insuff = next(m for m in marks if m["entry_ts"] == _iso(0))
    assert insuff["status"] == "insufficient"
    assert insuff["paper_delta"] is None
    assert insuff["price_delta"] is None
    assert insuff["reason"].startswith("insufficient data:")
    assert insuff["live_order"] is False
    later = next(m for m in marks if m["entry_ts"] == _iso(10))
    assert later["status"] == "open"
    assert later["paper_delta"] is None


def test_mark_ignores_a_true_flag_on_the_decision(paper_dir, passing_judgment):
    rows = watch_once([_src(passing_judgment, _iso(0), 50.0)], horizon_min=5)
    dirty = dict(rows[0])
    dirty.pop("_paper", None)
    dirty["run_id"] = "forced-false"
    dirty["live_order"] = True
    mark = scoreboard.open_mark(dirty, horizon_min=5)
    assert mark["live_order"] is False
    assert mark["status"] == "open"
    saved = json.loads((paper_dir / "scoreboard.json").read_text(encoding="utf-8"))
    assert all(item["live_order"] is False for item in saved["marks"])


def test_trim_drops_old_marked_marks():
    marks = [{"status": "marked", "id": str(i)} for i in range(5)]
    marks.append({"status": "open", "id": "keep"})
    original = scoreboard.MAX_MARKS
    try:
        scoreboard.MAX_MARKS = 2
        out = scoreboard._trim(marks)
    finally:
        scoreboard.MAX_MARKS = original
    assert [m["id"] for m in out] == ["4", "keep"]


def test_dashboard_script_imports_from_repo_root():
    p = subprocess.run(
        [sys.executable, "-c",
         "import runpy; runpy.run_path('dashboard/server.py', run_name='capsule_import_check')"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert p.returncode == 0, p.stderr


def test_watch_argv_is_paper_only():
    assert dash_server.WATCH_ARGV == ["desk.py", "--watch", "--passes", "1"]
    assert dash_server.HOST == "127.0.0.1"
    text = (ROOT / "dashboard" / "server.py").read_text(encoding="utf-8")
    assert "0.0.0.0" not in text
    for name in ("watchlist.py", "scoreboard.py", "desk.py"):
        assert "0.0.0.0" not in (ROOT / name).read_text(encoding="utf-8")


def test_watch_pass_empty_does_not_order(paper_dir):
    res = dash_server.run_watch_pass()
    assert res["ok"] is True
    assert res["appended"] == 0
    assert res["live_order"] is False


def test_dashboard_watchlist_and_scoreboard_http(paper_dir, passing_judgment):
    watch_once([_src(passing_judgment, _iso(0), 100.0)], horizon_min=5)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), dash_server.Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        def call(method, path, body=None, origin=None):
            data = None if body is None else json.dumps(body).encode("utf-8")
            headers = {}
            if origin is not None:
                headers["Origin"] = origin
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}",
                data=data,
                method=method,
                headers=headers,
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode("utf-8"))

        code, health = call("GET", "/healthz")
        assert code == 200 and health["live_order"] is False and health["mode"] == "dry_run"
        code, board = call("GET", "/api/scoreboard")
        assert code == 200 and board["live_order"] is False
        assert board["marks"][0]["status"] == "open"
        assert board["marks"][0]["entry_px"] == 100.0
        assert board["marks"][0]["live_order"] is False
        code, added = call(
            "POST", "/api/watchlist",
            {"op": "add", "mint": MINT},
            origin=f"http://127.0.0.1:{port}",
        )
        assert code == 200 and added["added"] is True and added["live_order"] is False
        code, listed = call("GET", "/api/watchlist")
        assert [item["mint"] for item in listed["mints"]] == [MINT]
        assert listed["live_order"] is False
        code, blocked = call(
            "POST", "/api/watchlist",
            {"op": "add", "mint": MINT_B},
            origin="http://evil.example",
        )
        assert code == 403 and blocked["live_order"] is False
        code, removed = call(
            "POST", "/api/watchlist",
            {"op": "remove", "mint": MINT},
            origin=f"http://localhost:{port}",
        )
        assert code == 200 and removed["removed"] is True
        code, bad = call(
            "POST", "/api/watchlist",
            {"op": "buy", "mint": MINT},
            origin=f"http://127.0.0.1:{port}",
        )
        assert code == 400
        assert bad["live_order"] is False
    finally:
        httpd.shutdown()


def test_ui_keeps_honesty_strip_and_paper_hooks():
    html = (ROOT / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert html.count("DRY RUN · PAPER") >= 3
    assert "https://x.com/0xPliny" in html
    assert "Not affiliated with Pump" in html
    assert 'id="watchlist"' in html
    assert 'id="scoreboard"' in html
    assert 'id="watch-timer"' in html
    assert "observed" in html and "derived" in html and "cited" in html
    assert "insufficient" in html
    assert "--bg:#0E1311" in html
    assert "--accent:#86efac" in html
    assert "--panel:#161C19" in html
    assert "live trading" not in html.lower()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Phase 4" in readme
    assert "live_order" in readme
    assert "does **not** post a trade" in readme
    assert "Chase" not in readme
