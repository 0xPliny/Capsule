#!/usr/bin/env python3
"""Dry-run Capsule desk: pump.fun public coin data -> six judgments -> gated decision -> log.

Read-only public GETs only (no keys for market data, no trade POSTs). live_order is always false.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from gates import decide
from sources import DemoSource, PumpSource, ReplaySource

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "data" / "decisions.jsonl"
SCHEMA_V = 1


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
    mint = ((snapshot.get("coin") or {}).get("mint") or snapshot.get("mint"))
    return {
        "schema_v": SCHEMA_V,
        "run_id": run_id or uuid.uuid4().hex,
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "mint": mint,
        "snapshot": snapshot,
        "judgment": judgment,
        "gates": decided["gates"],
        "intent": decided["intent"],
        "threshold_set_id": decided["threshold_set_id"],
        "live_order": False,
        "elapsed_ms": elapsed_ms,
        "fetch_ms": fetch_ms,
        "judgment_ms": round(elapsed_ms - fetch_ms, 1),
    }


def log_row(row: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def run_cycle(source) -> dict:
    t0 = time.time()
    try:
        snap, judgment = source.load()
    except Exception as e:
        snap = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "source": getattr(source, "name", "unknown"),
            "fetch_error": f"{type(e).__name__}: {e}",
        }
        judgment = {"error": f"snapshot fetch failed: {snap['fetch_error']}"}
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--demo", action="store_true", help="Offline fake snapshot + canned judgment (no network)")
    src.add_argument("--pump", action="store_true", help="Live pump.fun public GETs (default)")
    src.add_argument("--replay", metavar="PATH", help="Replay a Decision jsonl / snapshot JSON (offline)")
    parser.add_argument("--mint", default=None, help="pump.fun coin mint; default = most recently traded curve coin")
    args = parser.parse_args()

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
    log_row(row)
    print(json.dumps(row, indent=2))
    print(f"\nLogged to {LOG}")


if __name__ == "__main__":
    main()
