"""Holder-cluster score, fail-closed verdict, sample mint, hostile text."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from desk import build_record
from gates import decide
from holders import (
    OK_LT,
    RISKY_LT,
    SAMPLE_CA,
    _rpc_request,
    assess_holders,
    fetch_holder_rows,
    plain_ca,
    plain_text,
    sample_holder_bundle,
)
from sources import DemoSource

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _rows(n, *, funder=None, slot=None, amount=2):
    rows = []
    for i in range(n):
        rows.append({
            "owner": f"Owner{i}Test111111111111111111111111111",
            "funder": funder if funder is not None else f"Funder{i}Test111111111111111111111111",
            "slot": slot if slot is not None else 1000 + i,
            "amount": amount,
        })
    return rows


def _snap(rows, **extra):
    supply = extra.pop("supply", 100)
    sample = extra.pop("sample", False)
    snap = {
        "ts": "2026-10-06T00:00:00+00:00",
        "source": extra.pop("source", "mint"),
        "source_id": extra.pop("source_id", "mint"),
        "symbol": "DEMO-PERP",
        "mint": extra.pop("mint", "MintEvidence111111111111111111111111"),
        "holders": {
            "rows": rows,
            "supply": supply,
            "endpoint": "offline:test-holders",
            "observed": True,
            "sample": sample,
        },
        "mode": "dry_run",
        "max_risk_usd": 200,
        "take_usd": 400,
    }
    snap.update(extra)
    return snap


def _assert_public(text: str):
    assert "<" not in text and ">" not in text
    assert "javascript:" not in text.lower()
    assert "](" not in text
    folded = text.casefold()
    assert "toxic" not in folded
    assert "no_cluster" not in folded
    assert "clean" not in folded


def test_band_cuts_match_the_gate():
    raw = json.loads((ROOT / "thresholds.json").read_text(encoding="utf-8"))
    gate = next(g for g in raw["gates"] if g["name"] == "holder_cluster")
    assert gate["op"] == "lt"
    assert gate["threshold"] == OK_LT == 0.35
    assert gate["order"] == 5
    assert RISKY_LT == 0.60
    assert "not a buy clearance" in raw["notes"]["holder_cluster"]


def test_independent_rows_are_ok_not_a_clearance(passing_judgment):
    snap = _snap([], sample=True, source="demo", source_id="demo")
    snap["holders"] = sample_holder_bundle()
    out = decide(snap, passing_judgment)
    risk = out["holder_risk"]
    assert out["intent"]["action"] == "paper_long"
    assert risk["state"] == "done"
    assert risk["verdict"] == "OK"
    assert risk["score"] == 0
    assert risk["reason"] == "No large holder cluster found. Top 10 hold 16%."
    assert risk["no_cluster"] is True
    assert risk["top10_pct"] == 16
    assert risk["max_pct"] == 2
    assert 0 <= risk["top10_pct"] <= 100
    assert 0 <= risk["max_pct"] <= 100
    assert "safe" not in risk["reason"].lower()
    assert risk["is_sample"] is True
    assert risk["sample_ca"] == SAMPLE_CA
    assert all(line["level"] == "SAMPLE" and line["message"].startswith("SAMPLE ") for line in risk["log"])
    gate = next(g for g in out["gates"] if g["name"] == "holder_cluster")
    assert gate["passed"] is True
    assert gate["value"] == 0
    assert gate["threshold"] == 0.35
    assert gate["endpoint"] == "offline:sample-holders"
    assert gate["distance"] == 0.35
    assert gate["source_id"]
    for key in ("field", "threshold", "value", "distance", "flip", "reason"):
        assert key in gate


def test_shared_funder_is_risky_and_blocks_paper(passing_judgment):
    rows = _rows(8)
    for row in rows[:3]:
        row["funder"] = "FunderShared11111111111111111111111111"
    out = decide(_snap(rows), passing_judgment)
    risk = out["holder_risk"]
    assert risk["verdict"] == "RISKY"
    assert risk["state"] == "done"
    assert risk["reason"] == "3 wallets share funder Fund..1111"
    assert risk["no_cluster"] is False
    assert out["intent"]["action"] == "hold"
    assert out["intent"]["confident"] is True
    assert "holder cluster" in out["intent"]["reason"]
    assert out["data_status"] == "ok"
    gate = next(g for g in out["gates"] if g["name"] == "holder_cluster")
    assert gate["passed"] is False
    assert gate["value"] == round(3 / 8, 6)


def test_same_block_cluster_is_danger(passing_judgment):
    out = decide(_snap(_rows(8, slot=42)), passing_judgment)
    risk = out["holder_risk"]
    assert risk["verdict"] == "DANGER"
    assert risk["reason"] == "same-block cluster 100%"
    assert risk["no_cluster"] is False
    assert out["intent"]["action"] == "hold"
    assert risk["is_sample"] is False
    assert all(line["level"] != "SAMPLE" and not line["message"].startswith("SAMPLE ") for line in risk["log"])
    _assert_public(risk["reason"])


def test_linked_owners_score_without_a_shared_label(passing_judgment):
    rows = _rows(4)
    rows[0]["funder"] = rows[1]["owner"]
    rows[1]["funder"] = rows[2]["owner"]
    snap = _snap(rows)
    snap["holders"]["supply"] = None
    out = decide(snap, passing_judgment)
    risk = out["holder_risk"]
    assert risk["verdict"] == "DANGER"
    assert risk["reason"] == "linked wallets 3/4"
    assert risk["no_cluster"] is False
    assert "score" in risk


def test_missing_holder_rows_omit_verdict(demo_snapshot, passing_judgment):
    snap = dict(demo_snapshot)
    snap.pop("holders", None)
    out = decide(snap, passing_judgment)
    risk = out["holder_risk"]
    assert out["data_status"] == "insufficient"
    assert out["intent"]["action"] == "hold"
    assert out["intent"]["confident"] is False
    assert risk["state"] == "error"
    assert risk["verdict"] is None
    assert risk["no_cluster"] is False
    assert "score" not in risk
    assert "top10_pct" not in risk
    blob = json.dumps(risk)
    assert "NO_CLUSTER" not in blob
    assert "NO_CLUSTER_FOUND" not in blob
    assert "CLEAN" not in blob
    gate = next(g for g in out["gates"] if g["name"] == "holder_cluster")
    assert gate["passed"] is False
    assert gate["value"] is None
    assert "missing" in gate["reason"]


def test_incomplete_rows_fail_closed(passing_judgment):
    rows = _rows(4)
    for row in rows:
        row.pop("funder")
    out = decide(_snap(rows), passing_judgment)
    assert out["holder_risk"]["state"] == "error"
    assert out["holder_risk"]["verdict"] is None
    assert out["holder_risk"]["no_cluster"] is False
    assert out["intent"]["confident"] is False
    assert out["intent"]["action"] != "paper_long"


def test_sample_mint_fixture_offline(passing_judgment):
    snap = json.loads((FIXTURES / "sample_mint.json").read_text(encoding="utf-8"))
    judgment = snap.get("judgment") or passing_judgment
    out = decide(snap, judgment)
    risk = out["holder_risk"]
    assert snap["coin"]["mint"] == SAMPLE_CA
    assert risk["sample_ca"] == SAMPLE_CA
    assert risk["is_sample"] is True
    assert risk["state"] == "done"
    assert risk["verdict"] == "OK"
    assert risk["no_cluster"] is True
    assert isinstance(risk["top10_pct"], int) and 0 <= risk["top10_pct"] <= 100
    assert out["intent"]["action"] == "paper_long"
    assert out["intent"]["confident"] is True
    assert all(line["level"] == "SAMPLE" for line in risk["log"])
    row = build_record(
        source="demo", snapshot=snap, judgment=judgment, decided=out,
        elapsed_ms=1, fetch_ms=0, run_id="sample",
    )
    assert row["live_order"] is False
    assert row["holder_risk"]["verdict"] == "OK"
    assert row["holder_risk"]["is_sample"] is True


def test_demo_source_exposes_sample_ca():
    snap, judgment = DemoSource().load()
    out = decide(snap, judgment)
    assert snap["coin"]["mint"] == SAMPLE_CA
    assert out["holder_risk"]["is_sample"] is True
    assert out["holder_risk"]["verdict"] == "OK"
    assert out["holder_risk"]["no_cluster"] is True
    assert out["holder_risk"]["top10_pct"] == 16
    assert out["intent"]["action"] == "paper_long"


def test_hostile_metadata_stays_plain_text(passing_judgment):
    raw = json.loads((FIXTURES / "hostile_metadata.json").read_text(encoding="utf-8"))
    out = decide(raw, passing_judgment)
    row = build_record(
        source="mint", snapshot=raw, judgment=passing_judgment, decided=out,
        elapsed_ms=1, fetch_ms=0, run_id="hostile",
    )
    risk = row["holder_risk"]
    _assert_public(risk["reason"])
    for line in risk["log"]:
        _assert_public(line["message"])
        assert line["level"] in ("SAMPLE", "info", "warn", "error")
    coin = row["snapshot"]["coin"]
    assert "<" not in coin["name"] and ">" not in coin["name"]
    assert "<" not in coin["symbol"] and "javascript:" not in coin["symbol"].lower()
    assert "<" not in (coin.get("mint") or "")
    dumped = json.dumps(row)
    assert "<script" not in dumped.lower()
    assert "onerror" not in dumped.lower()
    assert "javascript:" not in dumped.lower()
    assert "](" not in risk["reason"]
    assert "NO_CLUSTER" not in json.dumps(risk)
    mint = coin["mint"]
    assert all(ch not in mint for ch in ";|&$`'\"<>")


def test_plain_text_strips_markup_and_controls():
    dirty = "A<script>alert(1)</script>\nB\x00[click](javascript:alert(1))"
    text = plain_text(dirty, 80)
    assert text == "Aalert(1) Bclick"
    assert "<" not in text and ">" not in text
    assert "\n" not in text and "\x00" not in text
    assert "javascript:" not in text.lower()
    assert "](" not in text
    assert plain_ca("aa<script>;rm -rf</script>zz") == "aarmrfzz"
    assert plain_ca("So111;rm -rf $(id)") == "So111rmrfid"


def test_rpc_transport_parses_reads_and_skips_trades():
    calls = []
    owner_a = "OwnerA11111111111111111111111111111111"
    owner_b = "OwnerB11111111111111111111111111111111"

    def transport(method, params):
        calls.append(method)
        if method == "getTokenLargestAccounts":
            return {"result": {"value": [
                {"address": "Acct1111111111111111111111111111111111", "uiAmount": 2, "decimals": 6},
                {"address": "Acct2222222222222222222222222222222222", "uiAmount": 2, "decimals": 6},
            ]}}
        if method == "getMultipleAccounts":
            return {"result": {"value": [
                {"data": {"parsed": {"info": {"owner": owner_a}}}},
                {"data": {"parsed": {"info": {"owner": owner_b}}}},
            ]}}
        if method == "getSignaturesForAddress":
            owner = params[0]
            slot = 10 if owner == owner_a else 11
            return {"result": [
                {"signature": "sig-new-" + owner[:6], "slot": slot + 5},
                {"signature": "sig-old-" + owner[:6], "slot": slot},
            ]}
        if method == "getTransaction":
            signature = params[0]
            owner = owner_a if signature.endswith("OwnerA") else owner_b
            funder = "FunderA1111111111111111111111111111111" if owner == owner_a else "FunderB1111111111111111111111111111111"
            return {"result": {
                "slot": 10,
                "transaction": {"message": {"instructions": [{
                    "parsed": {"type": "transfer", "info": {"source": funder, "destination": owner}},
                }]}},
            }}
        raise AssertionError(method)

    endpoints = []
    bundle = fetch_holder_rows(SAMPLE_CA, endpoints=endpoints, transport=transport)
    assert calls
    assert "sendTransaction" not in calls
    assert set(calls) <= {
        "getTokenLargestAccounts",
        "getMultipleAccounts",
        "getSignaturesForAddress",
        "getTransaction",
    }
    assert bundle["observed"] is True
    assert bundle["sample"] is False
    assert len(bundle["rows"]) == 2
    assert {row["funder"] for row in bundle["rows"]} == {
        "FunderA1111111111111111111111111111111",
        "FunderB1111111111111111111111111111111",
    }
    assert bundle["endpoint"] == "https://api.mainnet-beta.solana.com"
    assert any(ep["name"] == "solana-rpc" and ep["ok"] is True for ep in endpoints)
    assessed = assess_holders({
        "ts": "2026-10-06T00:00:00+00:00",
        "source_id": "mint",
        "holders": bundle,
    })
    assert assessed["holder_risk"]["verdict"] == "OK"
    assert assessed["holder_risk"]["is_sample"] is False
    assert all(line["level"] != "SAMPLE" for line in assessed["holder_risk"]["log"])
    for line in assessed["holder_risk"]["log"]:
        _assert_public(line["message"])


def test_rpc_failure_omits_ok(passing_judgment):
    def transport(method, params):
        raise TimeoutError("timed out")

    bundle = fetch_holder_rows("MintEvidence111111111111111111111111", transport=transport)
    assert bundle["observed"] is False
    snap = {
        "ts": "2026-10-06T00:00:00+00:00",
        "source_id": "mint",
        "holders": bundle,
        "mode": "dry_run",
    }
    out = decide(snap, passing_judgment)
    assert out["holder_risk"]["state"] == "error"
    assert out["holder_risk"]["verdict"] is None
    assert out["holder_risk"]["no_cluster"] is False
    assert "score" not in out["holder_risk"]
    assert "top10_pct" not in out["holder_risk"]
    assert out["intent"]["action"] == "hold"
    assert out["intent"]["confident"] is False


def test_small_cluster_stays_ok_but_is_not_no_cluster(passing_judgment):
    rows = _rows(10, amount=1)
    for row in rows[:2]:
        row["funder"] = "FunderShared11111111111111111111111111"
    out = decide(_snap(rows, supply=100), passing_judgment)
    risk = out["holder_risk"]
    assert risk["state"] == "done"
    assert risk["verdict"] == "OK"
    assert risk["no_cluster"] is False
    assert risk["reason"] == "2 wallets share funder Fund..1111"
    assert "No large holder cluster found" not in risk["reason"]
    assert out["intent"]["action"] == "paper_long"
    assert risk["top10_pct"] == 10
    assert risk["max_pct"] == 1


def test_out_of_range_percents_are_omitted(passing_judgment):
    rows = _rows(3, amount=80)
    out = decide(_snap(rows, supply=10), passing_judgment)
    risk = out["holder_risk"]
    assert risk["verdict"] == "OK"
    assert risk["no_cluster"] is True
    assert "top10_pct" not in risk
    assert "max_pct" not in risk
    assert risk["reason"] == "No large holder cluster found."
    # Boundaries 0 and 100 are real figures and stay on the payload.
    zero = decide(_snap(_rows(4, amount=0), supply=100), passing_judgment)["holder_risk"]
    assert zero["no_cluster"] is True
    assert zero["top10_pct"] == 0
    assert zero["max_pct"] == 0
    full = decide(_snap(_rows(1, amount=50), supply=50), passing_judgment)["holder_risk"]
    assert full["top10_pct"] == 100
    assert full["max_pct"] == 100


def test_disallowed_rpc_method_does_not_post():
    with pytest.raises(ValueError):
        _rpc_request("https://api.mainnet-beta.solana.com", "sendTransaction", ["nope"])
