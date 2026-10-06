"""decide() paths: every gate outcome, all gates recorded, policy order."""
from __future__ import annotations

from gates import decide, evaluate_gates, load_threshold_set


def _judgment(base: dict, **overrides) -> dict:
    j = dict(base)
    j.update(overrides)
    return j


def test_threshold_set_id_default():
    tset = load_threshold_set()
    assert tset["id"] == "default_v1"
    names = [g["name"] for g in sorted(tset["gates"], key=lambda g: g["order"])]
    assert names == ["inventory", "toxic_flow", "quote_env", "liquidity"]


def test_paper_long(demo_snapshot, passing_judgment):
    out = decide(demo_snapshot, passing_judgment)
    assert out["threshold_set_id"] == "default_v1"
    assert out["intent"]["action"] == "paper_long"
    assert out["intent"]["risk_usd"] == 200
    assert all(g["passed"] for g in out["gates"])
    assert [g["name"] for g in out["gates"]] == ["inventory", "toxic_flow", "quote_env", "liquidity"]


def test_paper_short(demo_snapshot, passing_judgment):
    out = decide(demo_snapshot, _judgment(passing_judgment, direction="short"))
    assert out["intent"]["action"] == "paper_short"


def test_direction_flat_holds(demo_snapshot, passing_judgment):
    out = decide(demo_snapshot, _judgment(passing_judgment, direction="flat"))
    assert out["intent"]["action"] == "hold"
    assert "direction=" in out["intent"]["reason"]
    assert all(g["passed"] for g in out["gates"])


def test_inventory_score_flattens(demo_snapshot, passing_judgment):
    out = decide(demo_snapshot, _judgment(passing_judgment, inventory=1.8, inventory_label="watch"))
    assert out["intent"]["action"] == "flatten_or_hold"
    assert out["intent"]["reason"] == "inventory pressure"
    by_name = {g["name"]: g for g in out["gates"]}
    assert by_name["inventory"]["passed"] is False
    assert by_name["toxic_flow"]["passed"] is True


def test_inventory_flatten_label(demo_snapshot, passing_judgment):
    out = decide(demo_snapshot, _judgment(passing_judgment, inventory=0.2, inventory_label="flatten"))
    assert out["intent"]["action"] == "flatten_or_hold"
    assert next(g for g in out["gates"] if g["name"] == "inventory")["passed"] is False


def test_toxic_hold(demo_snapshot, passing_judgment):
    out = decide(demo_snapshot, _judgment(passing_judgment, toxic_flow=0.9))
    assert out["intent"]["action"] == "hold"
    assert "toxic flow" in out["intent"]["reason"]
    by_name = {g["name"]: g for g in out["gates"]}
    assert by_name["toxic_flow"]["passed"] is False
    assert by_name["inventory"]["passed"] is True


def test_quote_env_hold(demo_snapshot, passing_judgment):
    out = decide(demo_snapshot, _judgment(passing_judgment, quote_env=0.2))
    assert out["intent"]["action"] == "hold"
    assert "quote env" in out["intent"]["reason"]
    assert next(g for g in out["gates"] if g["name"] == "quote_env")["passed"] is False


def test_liquidity_hold(demo_snapshot, passing_judgment):
    out = decide(demo_snapshot, _judgment(passing_judgment, liquidity=0.4, liquidity_label="thin"))
    assert out["intent"]["action"] == "hold"
    assert "liquidity" in out["intent"]["reason"]
    assert next(g for g in out["gates"] if g["name"] == "liquidity")["passed"] is False


def test_judgment_error_holds_and_still_records_gates(demo_snapshot):
    out = decide(demo_snapshot, {"error": "TYPESAFE_API_KEY not set"})
    assert out["intent"]["action"] == "hold"
    assert out["intent"]["confident"] is False
    assert out["data_status"] == "insufficient"
    assert out["intent"]["reason"].startswith("insufficient data:")
    assert "TYPESAFE_API_KEY not set" in out["intent"]["reason"]
    assert "source=demo" in out["intent"]["reason"]
    assert len(out["gates"]) == 4
    assert all(g["passed"] is False for g in out["gates"])
    assert all(g["reason"] for g in out["gates"])


def test_inventory_wins_policy_order(demo_snapshot, passing_judgment):
    """Inventory is first even when toxic would also fail."""
    out = decide(
        demo_snapshot,
        _judgment(passing_judgment, inventory=2.0, inventory_label="flatten", toxic_flow=0.99),
    )
    assert out["intent"]["action"] == "flatten_or_hold"
    by_name = {g["name"]: g for g in out["gates"]}
    assert by_name["inventory"]["passed"] is False
    assert by_name["toxic_flow"]["passed"] is False


def test_evaluate_all_gates_on_early_fail(demo_snapshot, passing_judgment):
    outcomes = evaluate_gates(_judgment(passing_judgment, inventory=2.0, quote_env=0.1, liquidity=0.0))
    assert len(outcomes) == 4
    assert {g["name"] for g in outcomes} == {"inventory", "toxic_flow", "quote_env", "liquidity"}


def test_custom_threshold_set(demo_snapshot, passing_judgment):
    tset = {
        "id": "strict_v1",
        "gates": [
            {"name": "inventory", "field": "inventory", "op": "lt", "threshold": 0.05, "order": 1},
            {"name": "toxic_flow", "field": "toxic_flow", "op": "lt", "threshold": 0.55, "order": 2},
            {"name": "quote_env", "field": "quote_env", "op": "ge", "threshold": 0.55, "order": 3},
            {"name": "liquidity", "field": "liquidity", "op": "ge", "threshold": 1.0, "order": 4},
        ],
    }
    out = decide(demo_snapshot, passing_judgment, tset)
    assert out["threshold_set_id"] == "strict_v1"
    assert out["intent"]["action"] == "flatten_or_hold"
    assert next(g for g in out["gates"] if g["name"] == "inventory")["threshold"] == 0.05
