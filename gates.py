"""Declarative decision gates for Capsule (dry-run paper intent only).

Every gate in the active threshold set is evaluated and recorded with the
field, threshold, value, distance, and what would flip it. Intent still follows
inventory → toxic_flow → quote_env → liquidity → direction, but only when the
snapshot and judgment are usable. A failed GET, timeout, stale print, or
missing field yields hold with confident false. live_order is never set.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

POLICY_ORDER = ("inventory", "toxic_flow", "quote_env", "liquidity")

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


def _fmt_num(v) -> str:
    if v is None:
        return "missing"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        text = f"{float(v):.6f}".rstrip("0").rstrip(".")
        return text if text else "0"
    return str(v)


def _source_label(snapshot: dict) -> str:
    return str(snapshot.get("source_id") or snapshot.get("source") or "unknown")


def _endpoint_label(snapshot: dict) -> str:
    failure = snapshot.get("fetch_failure") or {}
    if failure.get("endpoint"):
        return str(failure["endpoint"])
    fresh = snapshot.get("freshness") or {}
    if fresh.get("endpoint"):
        return str(fresh["endpoint"])
    for ep in snapshot.get("endpoints") or []:
        if isinstance(ep, dict) and ep.get("url"):
            return str(ep["url"])
        if isinstance(ep, dict) and ep.get("name"):
            return str(ep["name"])
    return _source_label(snapshot)


def _distance(op: str, value, threshold):
    if value is None or not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if op in ("lt", "le"):
        delta = float(threshold) - float(value)
    elif op in ("gt", "ge"):
        delta = float(value) - float(threshold)
    elif op in ("eq", "ne"):
        delta = abs(float(value) - float(threshold))
    else:
        return None
    return round(delta, 6)


def _cross_to_fail(field: str, op: str, threshold) -> str:
    th = _fmt_num(threshold)
    text = {
        "lt": f"{field} >= {th}",
        "le": f"{field} > {th}",
        "gt": f"{field} <= {th}",
        "ge": f"{field} < {th}",
        "eq": f"{field} != {th}",
        "ne": f"{field} == {th}",
    }
    return text.get(op, f"{field} crosses {th}")


def _cross_to_pass(field: str, op: str, threshold) -> str:
    th = _fmt_num(threshold)
    text = {
        "lt": f"{field} < {th}",
        "le": f"{field} <= {th}",
        "gt": f"{field} > {th}",
        "ge": f"{field} >= {th}",
        "eq": f"{field} == {th}",
        "ne": f"{field} != {th}",
    }
    return text.get(op, f"{field} clears {th}")


def _margin_phrase(distance, passed: bool) -> str:
    if distance is None:
        return "no numeric margin"
    mag = _fmt_num(abs(distance))
    if passed:
        return f"margin {mag}"
    return f"short by {mag}"


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
        distance = _distance(op, value, threshold)
        raw_threshold = spec["threshold"]
        flatten = spec["name"] == "inventory" and judgment.get("inventory_label") == "flatten"
        # Flatten label is inventory pressure even if the score is still "fine".
        if flatten:
            passed = False
        if not comparable:
            reason = f"{field} missing"
            flip = f"insufficient data until {field} is present"
        elif flatten:
            reason = "inventory_label is flatten"
            flip = (
                f"passes if inventory < {_fmt_num(raw_threshold)} "
                f"and inventory_label is not flatten (score {_margin_phrase(distance, True)})"
            )
        elif passed:
            reason = f"{field} {_fmt_num(value)} {op_symbol(op)} {_fmt_num(raw_threshold)}"
            flip = f"{_margin_phrase(distance, True)}; verdict flips if {_cross_to_fail(field, op, raw_threshold)}"
        else:
            reason = f"{field} {_fmt_num(value)} not {op_symbol(op)} {_fmt_num(raw_threshold)}"
            flip = f"{_margin_phrase(distance, False)}; verdict flips if {_cross_to_pass(field, op, raw_threshold)}"
        out.append({
            "name": spec["name"],
            "field": field,
            "op": op,
            "threshold": raw_threshold,
            "order": spec.get("order", 0),
            "value": value,
            "passed": passed,
            "distance": distance,
            "reason": reason,
            "flip": flip,
        })
    _note_blockers(out)
    return out


def op_symbol(op: str) -> str:
    return {"lt": "<", "le": "<=", "gt": ">", "ge": ">=", "eq": "==", "ne": "!="}.get(op, op)


def _note_blockers(outcomes: list[dict]) -> None:
    """Say which failed gate actually blocks, without dropping the others."""
    blocker = None
    for name in POLICY_ORDER:
        gate = next((g for g in outcomes if g["name"] == name), None)
        if gate and not gate["passed"]:
            blocker = name
            break
    if blocker is None:
        return
    for gate in outcomes:
        if gate.get("value") is None and "missing" in (gate.get("reason") or ""):
            continue
        if gate["name"] == blocker:
            gate["flip"] = f"blocking the verdict; {gate['flip']}"
        elif not gate["passed"]:
            gate["flip"] = f"{gate['flip']}; also failed, earlier block is {blocker}"
        else:
            gate["flip"] = f"{gate['flip']}; verdict already blocked by {blocker}"


def _insufficient_reason(snapshot: dict, judgment: dict, outcomes: list[dict]) -> str | None:
    """None when the snapshot and judgment are complete enough for a real intent."""
    src = _source_label(snapshot)
    endpoint = _endpoint_label(snapshot)
    failure = snapshot.get("fetch_failure") or {}
    if snapshot.get("fetch_error") or failure:
        kind = failure.get("kind") or "fetch failed"
        detail = failure.get("detail") or snapshot.get("fetch_error") or kind
        return f"insufficient data: source={src} endpoint={endpoint} ({kind}: {detail})"
    fresh = snapshot.get("freshness") or {}
    if fresh.get("stale"):
        age = fresh.get("last_trade_age_s")
        limit = fresh.get("stale_after_s")
        why = fresh.get("detail") or f"stale last_trade_age_s={age} > {limit}"
        return f"insufficient data: source={src} endpoint={endpoint} ({why})"
    missing_market = snapshot.get("missing_fields") or []
    if missing_market:
        names = ", ".join(str(x) for x in missing_market)
        return f"insufficient data: source={src} endpoint={endpoint} missing {names}"
    if judgment.get("error"):
        return f"insufficient data: source={src} endpoint={endpoint} ({judgment['error']})"
    missing_gates = []
    for gate in outcomes:
        if gate.get("op") == "legacy":
            continue
        if gate.get("value") is None:
            missing_gates.append(gate.get("field") or gate.get("name"))
    if missing_gates:
        names = ", ".join(missing_gates)
        return f"insufficient data: source={src} endpoint={endpoint} missing {names}"
    return None


def _stamp_source(outcomes: list[dict], snapshot: dict) -> None:
    source_id = _source_label(snapshot)
    endpoint = _endpoint_label(snapshot)
    for gate in outcomes:
        gate["source_id"] = source_id
        gate["endpoint"] = endpoint


def _role_present(value) -> bool:
    return value is not None and value != ""


def tag_evidence_roles(gate: dict) -> dict:
    """observed = print, derived = computed, cited = threshold line.

    A missing number is insufficient. That matches the fail-closed desk:
    no value, no confident distance.
    """
    return {
        "threshold": "cited" if _role_present(gate.get("threshold")) else "insufficient",
        "value": "observed" if _role_present(gate.get("value")) else "insufficient",
        "distance": "derived" if gate.get("distance") is not None else "insufficient",
        "endpoint": "observed" if _role_present(gate.get("endpoint")) else "insufficient",
        "source_id": "observed" if _role_present(gate.get("source_id")) else "insufficient",
        "snapshot_hash": "observed" if _role_present(gate.get("snapshot_hash")) else "insufficient",
        "fetched_at": "observed" if _role_present(gate.get("fetched_at")) else "insufficient",
        "decided_at": "observed" if _role_present(gate.get("decided_at")) else "insufficient",
    }


def _stamp_roles(outcomes: list[dict]) -> None:
    for gate in outcomes:
        gate["roles"] = tag_evidence_roles(gate)


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
    """Hard rules in code. Evaluates all gates. Never places an order; paper intent only.

    Unusable input (failed GET, timeout, stale print, missing field, judgment error)
    does not produce a confident intent. Every gate is still recorded.
    """
    tset = threshold_set or load_threshold_set()
    judgment = judgment or {}
    snapshot = snapshot or {}
    outcomes = evaluate_gates(judgment, tset)
    _stamp_source(outcomes, snapshot)
    insuff = _insufficient_reason(snapshot, judgment, outcomes)
    if insuff:
        for gate in outcomes:
            gate["flip"] = f"no confident intent; {gate['flip']}"
        intent = {"action": "hold", "reason": insuff, "risk_usd": 0, "confident": False}
        status = "insufficient"
    else:
        intent = intent_from_gates(snapshot, judgment, outcomes)
        intent["confident"] = True
        status = "ok"
    _stamp_roles(outcomes)
    return {
        "intent": intent,
        "gates": outcomes,
        "threshold_set_id": tset["id"],
        "data_status": status,
    }
