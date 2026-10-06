#!/usr/bin/env python3
"""Dry-run Capsule desk: pump.fun public coin data -> six judgments -> gated decision -> log.

Read-only public GETs only (no keys for market data, no trade POSTs). live_order is always false.
--watch refreshes a saved mint list and appends Decisions. It does not send orders.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from gates import decide, tag_evidence_roles
from holders import sanitize_snapshot
from sources import DemoSource, PumpSource, ReplaySource
import scoreboard
import watchlist

ROOT = Path(__file__).resolve().parent
SCHEMA_V = 2


def canonical_hash(obj: dict) -> str:
    """sha256 of canonical JSON. Key order does not change the digest."""
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ask_judgment(snapshot: dict) -> dict:
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key:
        return {
            "error": "TYPESAFE_API_KEY not set",
            "hint": "Create a key at typesafe.ai and export TYPESAFE_API_KEY.",
        }

    from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

    with TypeSafeClient() as client:
        response = client.system_one(
            model="jev-latest",
            state={"market": snapshot},
            questions={
                "regime": Choice(
                    instructions="What market regime is this snapshot in for short-horizon trading?",
                    criteria={"trend": None, "mean_revert": None, "chop": None, "shock": None},
                ),
                "direction": Choice(
                    instructions="What is the short-horizon directional bias?",
                    criteria={"long": None, "short": None, "flat": None},
                ),
                "toxic_flow": Noul(
                    instructions="Is aggressive flow toxic against a maker right now?"
                ),
                "liquidity": Score(
                    instructions="How usable is liquidity for quoting or taking?",
                    criteria=["thin", "ok", "deep"],
                ),
                "quote_env": Noul(
                    instructions="Is the quote environment worth posting maker quotes into?"
                ),
                "inventory": Score(
                    instructions="Given inventory and drawdown in the snapshot, how pressured is inventory?",
                    criteria=["fine", "watch", "flatten"],
                ),
            },
        )

    def label(score_answer) -> int:
        probs = score_answer.probabilities
        return max(probs, key=probs.get) if probs else round(score_answer.score)

    inv = response.scores["inventory"]
    liq = response.scores["liquidity"]
    return {
        "regime": response.choices["regime"].choice,
        "regime_conf": response.choices["regime"].confidence,
        "direction": response.choices["direction"].choice,
        "direction_conf": response.choices["direction"].confidence,
        "toxic_flow": response.nouls["toxic_flow"].noul,
        "liquidity": liq.score,
        "liquidity_label": ["thin", "ok", "deep"][label(liq)],
        "quote_env": response.nouls["quote_env"].noul,
        "inventory": inv.score,
        "inventory_label": ["fine", "watch", "flatten"][label(inv)],
        "raw_available": True,
    }


def build_record(
    *,
    source: str,
    snapshot: dict,
    judgment: dict,
    decided: dict,
    elapsed_ms: float,
    fetch_ms: float,
    run_id: str | None = None,
) -> dict:
    snapshot = sanitize_snapshot(snapshot)
    mint = ((snapshot.get("coin") or {}).get("mint") or snapshot.get("mint"))
    decided_at = datetime.now(timezone.utc).isoformat()
    snap_hash = canonical_hash(snapshot)
    fetched_at = snapshot.get("ts")
    endpoints = snapshot.get("endpoints") or [
        {"name": source, "url": None, "ok": not snapshot.get("fetch_error"), "error": snapshot.get("fetch_error"),
         "fetched_at": fetched_at}
    ]
    gates = []
    for gate in decided["gates"]:
        stamped = dict(gate)
        stamped["snapshot_hash"] = snap_hash
        stamped["fetched_at"] = fetched_at
        stamped["decided_at"] = decided_at
        stamped["roles"] = tag_evidence_roles(stamped)
        gates.append(stamped)
    return {
        "schema_v": SCHEMA_V,
        "run_id": run_id or uuid.uuid4().hex,
        "ts": decided_at,
        "source": source,
        "mint": mint,
        "snapshot": snapshot,
        "snapshot_hash": snap_hash,
        "endpoints": endpoints,
        "judgment": judgment,
        "gates": gates,
        "intent": decided["intent"],
        "data_status": decided.get("data_status", "ok"),
        "threshold_set_id": decided["threshold_set_id"],
        "holder_risk": decided.get("holder_risk"),
        "live_order": False,
        "elapsed_ms": elapsed_ms,
        "fetch_ms": fetch_ms,
        "judgment_ms": round(elapsed_ms - fetch_ms, 1),
    }


def log_path() -> Path:
    return watchlist.data_dir() / "decisions.jsonl"


def log_row(row: dict) -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def commit_cycle(row: dict, *, horizon_min: float | None = None) -> dict:
    """Append the Decision, then the paper mark. A later print settles older marks.

    The row that opens a mark cannot settle itself. No order is sent.
    """
    # Log first so a bad scoreboard file cannot drop the Decision.
    log_row(row)
    snap = row.get("snapshot") if isinstance(row.get("snapshot"), dict) else {}
    when = snap.get("ts") or row.get("ts")
    settled = scoreboard.settle_with_print(
        row.get("mint"),
        scoreboard.snapshot_price(snap),
        when,
        exclude_id=row.get("run_id"),
    )
    mark = scoreboard.open_mark(row, horizon_min=horizon_min)
    return {"settled": settled, "mark": mark, "live_order": False}


def watch_once(sources, *, horizon_min: float | None = None) -> list[dict]:
    """One dry-run pass over already-built sources. Appends Decisions. Never executes."""
    rows = []
    for source in sources:
        row = run_cycle(source)
        extra = commit_cycle(row, horizon_min=horizon_min)
        row = dict(row)
        row["_paper"] = {"mark": extra["mark"], "settled": extra["settled"], "live_order": False}
        rows.append(row)
    return rows


def run_cycle(source) -> dict:
    t0 = time.time()
    try:
        snap, judgment = source.load()
    except Exception as e:
        name = getattr(source, "name", "unknown")
        detail = f"{type(e).__name__}: {e}"
        now = datetime.now(timezone.utc).isoformat()
        snap = {
            "ts": now,
            "source": name,
            "source_id": name,
            "fetch_error": detail,
            "fetch_failure": {"kind": "error", "endpoint": name, "detail": detail},
            "endpoints": [{"name": name, "url": None, "ok": False, "error": "error", "fetched_at": now}],
            "mode": "dry_run",
        }
        judgment = {"error": detail}
    fetch_ms = round((time.time() - t0) * 1000, 1)

    if judgment is None:
        if "fetch_error" in snap:
            judgment = {"error": f"snapshot fetch failed: {snap['fetch_error']}"}
        else:
            try:
                judgment = ask_judgment(snap)
            except Exception as e:
                judgment = {"error": f"judgment call failed: {type(e).__name__}: {e}"}

    decided = decide(snap, judgment)
    elapsed_ms = round((time.time() - t0) * 1000, 1)
    return build_record(
        source=getattr(source, "name", "unknown"),
        snapshot=snap,
        judgment=judgment,
        decided=decided,
        elapsed_ms=elapsed_ms,
        fetch_ms=fetch_ms,
    )


def _horizon(args) -> float | None:
    if args.horizon is None:
        return None
    if args.horizon < 0:
        raise SystemExit("--horizon must be >= 0")
    return args.horizon


def _watch_sources(args):
    """Selected mints, or the offline demo. Empty watchlist refreshes nothing."""
    if args.demo:
        if args.mint:
            raise SystemExit("--mint cannot be combined with --demo")
        return [DemoSource()]
    if args.mint:
        if not watchlist.valid_mint(args.mint):
            raise SystemExit("mint must be a pump.fun coin address")
        return [PumpSource(args.mint)]
    chosen = watchlist.mints()
    if not chosen:
        return []
    return [PumpSource(mint) for mint in chosen]


def _print_watch_rows(rows: list[dict]) -> None:
    for row in rows:
        public = {k: v for k, v in row.items() if k != "_paper"}
        print(json.dumps(public))
        paper = row.get("_paper") or {}
        settled = paper.get("settled") or []
        if settled:
            print(json.dumps({"settled": settled, "live_order": False}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--demo", action="store_true", help="Offline fake snapshot + canned judgment (no network)")
    src.add_argument("--pump", action="store_true", help="Live pump.fun public GETs (default)")
    src.add_argument("--replay", metavar="PATH", help="Replay a Decision jsonl / snapshot JSON (offline)")
    parser.add_argument("--mint", default=None, help="pump.fun coin mint; default = most recently traded curve coin")
    parser.add_argument("--watch-add", metavar="MINT", help="Save a mint on the local paper watchlist and exit")
    parser.add_argument("--watch-remove", metavar="MINT", help="Remove a mint from the local paper watchlist and exit")
    parser.add_argument("--watch-list", action="store_true", help="Print the local paper watchlist and exit")
    parser.add_argument("--watch", action="store_true", help="Refresh selected mints and append Decisions (no orders)")
    parser.add_argument("--interval", type=float, default=60.0, help="Seconds between --watch passes (default 60)")
    parser.add_argument("--passes", type=int, default=None, help="--watch passes; omit to run until interrupted")
    parser.add_argument("--horizon", type=float, default=None, help="Paper scoreboard horizon in minutes (default 5)")
    args = parser.parse_args()

    editing = bool(args.watch_add or args.watch_remove or args.watch_list)
    if editing and (args.watch or args.replay or args.demo or args.pump or args.mint):
        parser.error("watchlist edit flags run alone")
    if args.watch_add:
        result = watchlist.add_mint(args.watch_add)
        print(json.dumps(result, indent=2))
        if not result.get("ok"):
            raise SystemExit(2)
        return
    if args.watch_remove:
        result = watchlist.remove_mint(args.watch_remove)
        print(json.dumps(result, indent=2))
        if not result.get("ok"):
            raise SystemExit(2)
        return
    if args.watch_list:
        print(json.dumps({"mints": watchlist.load_watchlist()["mints"], "live_order": False}, indent=2))
        return

    horizon = _horizon(args)
    if args.watch:
        if args.replay:
            parser.error("--watch cannot be combined with --replay")
        if args.passes is not None and args.passes < 1:
            parser.error("--passes must be >= 1")
        if args.interval < 0:
            parser.error("--interval must be >= 0")
        n = 0
        try:
            while args.passes is None or n < args.passes:
                sources = _watch_sources(args)
                if not sources:
                    print("watchlist empty; nothing to refresh (dry-run, live_order false)")
                else:
                    _print_watch_rows(watch_once(sources, horizon_min=horizon))
                n += 1
                if args.passes is not None and n >= args.passes:
                    break
                if args.interval:
                    time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nwatch stopped (dry-run, live_order false)")
        print(f"\nLogged to {log_path()}")
        print("live_order false")
        return

    if args.replay:
        if args.mint:
            parser.error("--mint cannot be combined with --replay")
        source = ReplaySource(args.replay)
    elif args.demo:
        if args.mint:
            parser.error("--mint cannot be combined with --demo")
        source = DemoSource()
    else:
        source = PumpSource(args.mint)

    row = run_cycle(source)
    commit_cycle(row, horizon_min=horizon)
    print(json.dumps(row, indent=2))
    print(f"\nLogged to {log_path()}")
    print("live_order false")


if __name__ == "__main__":
    main()
