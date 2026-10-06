"""Declarative decision gates for Capsule (dry-run paper intent only).

Every gate in the active threshold set is evaluated. Intent still follows
inventory → toxic_flow → quote_env → liquidity → direction. live_order is never set.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Fallback if thresholds.json is missing. Keep in lockstep with that file.
THRESHOLD_SET = {
    "id": "default_v1",
    "gates": [
        {"name": "inventory", "field": "inventory", "op": "lt", "threshold": 1.5, "order": 1},
        {"name": "toxic_flow", "field": "toxic_flow", "op": "lt", "threshold": 0.55, "order": 2},
        {"name": "quote_env", "field": "quote_env", "op": "ge", "threshold": 0.55, "order": 3},
        {"name": "liquidity", "field": "liquidity", "op": "ge", "threshold": 1.0, "order": 4},
    ],
}

OPS = {
    "lt": lambda a, b: a < b,
    "le": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "ge": lambda a, b: a >= b,
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
}


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def load_threshold_set(path: Path | None = None) -> dict:
    """Load a threshold set from JSON, else the inline THRESHOLD_SET."""
    p = path or (ROOT / "thresholds.json")
    if p.is_file():
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("id") and data.get("gates"):
            return data
    return THRESHOLD_SET


def evaluate_gates(judgment: dict, threshold_set: dict | None = None) -> list[dict]:
    """Evaluate every gate. Missing / non-numeric values fail closed."""
    tset = threshold_set or load_threshold_set()
    specs = sorted(tset.get("gates") or [], key=lambda g: g.get("order", 0))
    out = []
    for spec in specs:
        field = spec["field"]
        raw = judgment.get(field)
        op = spec["op"]
        threshold = spec["threshold"]
        if op in ("eq", "ne") and not isinstance(threshold, (int, float)):
            value = raw
            comparable = raw is not None
        else:
            value = _f(raw)
            comparable = value is not None
            threshold = _f(threshold, threshold)
        passed = bool(comparable and OPS[op](value, threshold))
        # Flatten label is inventory pressure even if the score is still "fine".
        if spec["name"] == "inventory" and judgment.get("inventory_label") == "flatten":
            passed = False
        out.append({
            "name": spec["name"],
            "field": field,
            "op": op,
            "threshold": spec["threshold"],
            "order": spec.get("order", 0),
            "value": value,
            "passed": passed,
        })
    return out


def _gate_passed(outcomes: list[dict], name: str) -> bool:
    for g in outcomes:
        if g["name"] == name:
            return bool(g["passed"])
    return False


def intent_from_gates(snapshot: dict, judgment: dict, outcomes: list[dict]) -> dict:
    """Same policy as the original if-chain: inventory → toxic → quote → liq → direction."""
    if not _gate_passed(outcomes, "inventory"):
        return {"action": "flatten_or_hold", "reason": "inventory pressure", "risk_usd": 0}
    toxic = next((g["value"] for g in outcomes if g["name"] == "toxic_flow"), None)
    if not _gate_passed(outcomes, "toxic_flow"):
        return {"action": "hold", "reason": f"toxic flow p={toxic}", "risk_usd": 0}
    quote_ok = next((g["value"] for g in outcomes if g["name"] == "quote_env"), None)
    quote_th = next((g["threshold"] for g in outcomes if g["name"] == "quote_env"), 0.55)
    if not _gate_passed(outcomes, "quote_env"):
        return {"action": "hold", "reason": f"quote env p={quote_ok} < {quote_th}", "risk_usd": 0}
    liq = next((g["value"] for g in outcomes if g["name"] == "liquidity"), None)
    liq_th = next((g["threshold"] for g in outcomes if g["name"] == "liquidity"), 1.0)
    if not _gate_passed(outcomes, "liquidity"):
        return {"action": "hold", "reason": f"liquidity score {liq} < {liq_th}", "risk_usd": 0}
    direction = judgment.get("direction")
    if direction in ("long", "short"):
        return {
            "action": f"paper_{direction}",
            "reason": "all gates passed; dry-run paper intent only",
            "risk_usd": min(200, int(snapshot.get("max_risk_usd", 200))),
            "take_usd": int(snapshot.get("take_usd", 400)),
        }
    return {"action": "hold", "reason": f"direction={direction}", "risk_usd": 0}


def decide(snapshot: dict, judgment: dict, threshold_set: dict | None = None) -> dict:
    """Hard rules in code. Evaluates all gates. Never places an order; paper intent only."""
    tset = threshold_set or load_threshold_set()
    outcomes = evaluate_gates(judgment, tset)
    if judgment.get("error"):
        intent = {"action": "hold", "reason": judgment["error"], "risk_usd": 0}
    else:
        intent = intent_from_gates(snapshot, judgment, outcomes)
    return {
        "intent": intent,
        "gates": outcomes,
        "threshold_set_id": tset["id"],
    }
