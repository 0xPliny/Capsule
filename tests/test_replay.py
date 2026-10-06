"""Replay a stored Decision through gates and get the same Intent. Offline CLI."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from desk import SCHEMA_V, build_record, run_cycle
from gates import decide
from sources import DemoSource, ReplaySource, load_replay, migrate_row

ROOT = Path(__file__).resolve().parent.parent


def test_replay_decision_reproduces_intent(demo_snapshot, passing_judgment):
    first = decide(demo_snapshot, passing_judgment)
    record = build_record(
        source="demo",
        snapshot=demo_snapshot,
        judgment=passing_judgment,
        decided=first,
        elapsed_ms=1.0,
        fetch_ms=0.0,
        run_id="replay-test",
    )
    replayed = decide(record["snapshot"], record["judgment"])
    assert replayed["intent"] == first["intent"]
    assert replayed["threshold_set_id"] == first["threshold_set_id"]
    assert [g["passed"] for g in replayed["gates"]] == [g["passed"] for g in first["gates"]]


def test_replay_each_intent_path(demo_snapshot, passing_judgment):
    variants = [
        ({}, "paper_long"),
        ({"direction": "short"}, "paper_short"),
        ({"direction": "flat"}, "hold"),
        ({"inventory": 2.0}, "flatten_or_hold"),
        ({"toxic_flow": 0.9}, "hold"),
        ({"quote_env": 0.1}, "hold"),
        ({"liquidity": 0.0}, "hold"),
    ]
    for extra, action in variants:
        j = dict(passing_judgment, **extra)
        first = decide(demo_snapshot, j)
        assert first["intent"]["action"] == action
        again = decide(demo_snapshot, j)
        assert again["intent"] == first["intent"]


def test_load_replay_jsonl(fixtures_dir):
    row = load_replay(fixtures_dir / "replay_paper_long.jsonl")
    assert row["schema_v"] == 1
    assert row["intent"]["action"] == "paper_long"
    again = decide(row["snapshot"], row["judgment"])
    assert again["intent"]["action"] == "paper_long"
    assert again["intent"]["reason"] == row["intent"]["reason"]


def test_replay_source_offline(fixtures_dir):
    src = ReplaySource(fixtures_dir / "replay_paper_long.jsonl")
    snap, judgment = src.load()
    assert src.name == "replay"
    assert snap["symbol"] == "DEMO-PERP"
    assert judgment["direction"] == "long"
    out = decide(snap, judgment)
    assert out["intent"]["action"] == "paper_long"


def test_migrate_legacy_row(fixtures_dir):
    raw = json.loads((fixtures_dir / "legacy_row.jsonl").read_text(encoding="utf-8").splitlines()[0])
    row = migrate_row(raw)
    assert row["schema_v"] == 1
    assert row["live_order"] is False
    assert row["mint"]
    assert row["intent"]["action"] == "paper_short"
    again = decide(row["snapshot"], row["judgment"])
    assert again["intent"]["action"] == "paper_short"


def test_replay_snapshot_only_no_typesafe(demo_snapshot, tmp_path):
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(demo_snapshot), encoding="utf-8")
    snap, judgment = ReplaySource(path).load()
    assert snap["symbol"] == "DEMO-PERP"
    assert judgment.get("error")
    out = decide(snap, judgment)
    assert out["intent"]["action"] == "hold"


def test_demo_source_offline():
    snap, judgment = DemoSource().load()
    out = decide(snap, judgment)
    assert out["intent"]["action"] == "paper_long"
    assert snap["source"] == "demo"


def test_run_cycle_demo_record_shape():
    row = run_cycle(DemoSource())
    assert row["schema_v"] == SCHEMA_V
    assert row["run_id"]
    assert row["source"] == "demo"
    assert row["live_order"] is False
    assert row["threshold_set_id"] == "default_v1"
    assert isinstance(row["gates"], list) and len(row["gates"]) == 4
    assert row["intent"]["action"] == "paper_long"
    assert "snapshot" in row and "judgment" in row


def test_cli_demo_offline():
    env = {k: v for k, v in os.environ.items() if k != "TYPESAFE_API_KEY"}
    p = subprocess.run(
        [sys.executable, "desk.py", "--demo"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert p.returncode == 0, p.stderr
    assert "paper_long" in p.stdout
    assert '"live_order": false' in p.stdout
    assert '"schema_v": 2' in p.stdout


def test_cli_replay_offline(fixtures_dir):
    env = {k: v for k, v in os.environ.items() if k != "TYPESAFE_API_KEY"}
    p = subprocess.run(
        [sys.executable, "desk.py", "--replay", str(fixtures_dir / "replay_paper_long.jsonl")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert p.returncode == 0, p.stderr
    assert '"source": "replay"' in p.stdout
    assert "paper_long" in p.stdout
    assert '"live_order": false' in p.stdout
