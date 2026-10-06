#!/usr/bin/env python3
"""Dry-run Jev desk: pump.fun public coin data -> six Jev judgments -> gated decision -> log.

Read-only public GETs only (no wallet, no keys for market data, no trade POSTs). live_order is always false.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "data" / "decisions.jsonl"

# Public, unauthenticated pump.fun read endpoints (same ones the pump.fun web app uses).
PUMP_FRONTEND = "https://frontend-api-v3.pump.fun"
PUMP_SWAP = "https://swap-api.pump.fun"
UA = {"User-Agent": "Mozilla/5.0 (jev-hft-desk dry-run)", "Accept": "application/json"}
INITIAL_REAL_TOKEN_RESERVES = 793_100_000 * 10**6  # pump.fun bonding curve sellable supply (raw units)

# Decision gates (Jev outputs are probabilities / expected scores).
TOXIC_HOLD_P = 0.55        # Noul toxic_flow >= this -> hold
QUOTE_ENV_MIN_P = 0.55     # Noul quote_env must be >= this
INVENTORY_FLATTEN = 1.5    # Score inventory (0 fine, 1 watch, 2 flatten) >= this -> flatten
LIQUIDITY_MIN = 1.0        # Score liquidity (0 thin, 1 ok, 2 deep) must be >= this


def demo_snapshot() -> dict:
    """Stand-in book state. Replace with real venue feed later."""
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "symbol": "DEMO-PERP",
        "price": {"mid": 100.0, "microprice": 100.02, "ret_1m": 0.001, "ret_5m": -0.002, "ret_30m": 0.004},
        "book": {"spread_bps": 2.5, "depth_3": 12000, "imbalance": 0.12, "queue_pos": 3},
        "flow": {"agg_buy": 4100, "agg_sell": 3800, "trade_intensity": 1.2, "cancel_intensity": 0.8},
        "vol": {"short": 0.18, "medium": 0.22, "vs_24h": "normal"},
        "cross": {"basis_bps": 1.0, "funding": 0.0001},
        "book_pnl": {"inventory": 0.0, "unrealized": 0.0, "drawdown": 0.0, "position_age_s": 0},
        "health": {"fill_ratio": 0.0, "reject_rate": 0.0, "slippage_bps": 0.0},
        "mode": "dry_run",
        "max_risk_usd": 200,
        "take_usd": 400,
    }


# ---------------------------------------------------------------- pump.fun public data (read-only)

def _get_json(url: str, timeout: float = 10.0):
    req = urllib.request.Request(url, headers=UA, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def pick_active_mint() -> str:
    """Most recently traded coin still on the bonding curve (not graduated, not nsfw)."""
    q = urllib.parse.urlencode(
        {"offset": 0, "limit": 50, "sort": "last_trade_timestamp", "order": "DESC", "includeNsfw": "false"}
    )
    coins = _get_json(f"{PUMP_FRONTEND}/coins?{q}")
    for c in coins:
        if not c.get("complete") and not c.get("nsfw") and not c.get("is_banned"):
            return c["mint"]
    if not coins:
        raise RuntimeError("pump.fun coin list empty")
    return coins[0]["mint"]


def pump_snapshot(mint: str | None = None) -> dict:
    """Compact snapshot in the same shape as demo_snapshot(), built from pump.fun public data.

    pump.fun is a constant-product bonding curve (or PumpSwap AMM after graduation), not a CLOB, so
    "book" fields are curve equivalents: depth = quote needed to move price 2%, spread_bps = round-trip
    price impact of a $100 clip, imbalance = buy/sell USD imbalance of recent trades.
    """
    mint = mint or pick_active_mint()
    coin = _get_json(f"{PUMP_FRONTEND}/coins-v2/{mint}")
    trades = _get_json(f"{PUMP_SWAP}/v2/coins/{mint}/trades?limit=100&cursor=0&minSolAmount=0").get("trades", [])
    try:
        candles = _get_json(f"{PUMP_SWAP}/v1/coins/{mint}/candles?interval=1m&limit=60&currency=USD")
    except urllib.error.HTTPError:
        candles = []

    now_ms = time.time() * 1000
    base_dec = int(coin.get("base_decimals") or 6)
    complete = bool(coin.get("complete"))

    # Price: last trade (USD), fallback to market cap / supply.
    last_px = _f(trades[0].get("priceUsd")) if trades else None
    supply = _f(coin.get("total_supply"), 0) / 10**base_dec
    mcap_usd = _f(coin.get("market_cap_usd"))
    if last_px is None and mcap_usd and supply:
        last_px = mcap_usd / supply

    # Curve depth (only meaningful while on the bonding curve).
    vtok = _f(coin.get("virtual_token_reserves"), 0) / 10**base_dec
    quote_res_usd = (last_px * vtok) if (last_px and vtok and not complete) else None
    depth_2pct = quote_res_usd * (math.sqrt(1.02) - 1) if quote_res_usd else None
    impact_100_bps = (2 * 100 / quote_res_usd) * 1e4 if quote_res_usd else None  # buy then sell $100
    real_tok = _f(coin.get("real_token_reserves"), 0)
    curve_progress = None if complete else round(max(0.0, min(1.0, 1 - real_tok / INITIAL_REAL_TOKEN_RESERVES)), 4)

    # Flow from recent trades.
    buys = [t for t in trades if t.get("type") == "buy"]
    sells = [t for t in trades if t.get("type") == "sell"]
    buy_usd = sum(_f(t.get("amountUsd"), 0) for t in buys)
    sell_usd = sum(_f(t.get("amountUsd"), 0) for t in sells)
    tot = buy_usd + sell_usd
    span_min = None
    if len(trades) >= 2:
        ts = [datetime.fromisoformat(t["timestamp"].replace("Z", "+00:00")).timestamp() for t in trades]
        span_min = max((max(ts) - min(ts)) / 60, 1 / 60)
    wallets = {t.get("userAddress") for t in trades}
    top_trade = max((_f(t.get("amountUsd"), 0) for t in trades), default=0)

    # Returns / vol from 1m candles (oldest -> newest).
    closes = [_f(c.get("close")) for c in sorted(candles, key=lambda c: c["timestamp"]) if _f(c.get("close"))]
    def ret(n):
        return round(closes[-1] / closes[-1 - n] - 1, 5) if len(closes) > n else None
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    vol_short = round(statistics.pstdev(rets[-5:]), 5) if len(rets) >= 2 else None
    vol_med = round(statistics.pstdev(rets[-30:]), 5) if len(rets) >= 2 else None
    vol_1m_usd = sum(_f(c.get("volume"), 0) for c in candles[-5:]) if candles else None

    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": "pump.fun public API (frontend-api-v3 coins-v2, swap-api trades+candles)",
        "venue": "pumpswap_amm" if complete else "pump_bonding_curve",
        "symbol": coin.get("symbol"),
        "coin": {
            "mint": mint,
            "name": coin.get("name"),
            "age_min": round((now_ms - _f(coin.get("created_timestamp"), now_ms)) / 60000, 1),
            "market_cap_usd": round(mcap_usd, 2) if mcap_usd else None,
            "ath_market_cap_usd_hint": coin.get("ath_market_cap"),
            "graduated": complete,
            "curve_progress": curve_progress,
            "reply_count": coin.get("reply_count"),
            "has_socials": bool(coin.get("twitter") or coin.get("telegram") or coin.get("website")),
            "last_trade_age_s": round((now_ms - _f(coin.get("last_trade_timestamp"), now_ms)) / 1000, 1),
        },
        "price": {"last_usd": last_px, "ret_1m": ret(1), "ret_5m": ret(5), "ret_30m": ret(30)},
        "book": {
            "spread_bps": round(impact_100_bps, 1) if impact_100_bps else None,
            "depth_2pct_usd": round(depth_2pct, 2) if depth_2pct else None,
            "curve_quote_reserve_usd": round(quote_res_usd, 2) if quote_res_usd else None,
            "imbalance": round((buy_usd - sell_usd) / tot, 3) + 0.0 if tot else 0.0,
        },
        "flow": {
            "n_trades": len(trades),
            "agg_buy_usd": round(buy_usd, 2),
            "agg_sell_usd": round(sell_usd, 2),
            "trades_per_min": round(len(trades) / span_min, 2) if span_min else None,
            "unique_wallets": len(wallets),
            "largest_trade_usd": round(top_trade, 2),
            "volume_5m_usd": round(vol_1m_usd, 2) if vol_1m_usd is not None else None,
        },
        "vol": {"short_1m_std": vol_short, "medium_1m_std": vol_med},
        "book_pnl": {"inventory": 0.0, "unrealized": 0.0, "drawdown": 0.0, "position_age_s": 0},
        "health": {"fill_ratio": 0.0, "reject_rate": 0.0, "slippage_bps": 0.0},
        "mode": "dry_run",
        "max_risk_usd": 200,
        "take_usd": 400,
    }


def ask_jev(snapshot: dict) -> dict:
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key:
        return {
            "error": "TYPESAFE_API_KEY not set",
            "hint": "Create a key at typesafe.ai and tell Grok Bot to save it, or export TYPESAFE_API_KEY.",
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


def decide(snapshot: dict, judgment: dict) -> dict:
    """Hard rules in code. Never places an order; paper intent only."""
    if judgment.get("error"):
        return {"action": "hold", "reason": judgment["error"], "risk_usd": 0}

    direction = judgment.get("direction")
    toxic = _f(judgment.get("toxic_flow"))
    quote_ok = _f(judgment.get("quote_env"))
    inventory = _f(judgment.get("inventory"))
    liquidity = _f(judgment.get("liquidity"))
    gates = {
        "toxic_flow<0.55": toxic is not None and toxic < TOXIC_HOLD_P,
        "quote_env>=0.55": quote_ok is not None and quote_ok >= QUOTE_ENV_MIN_P,
        "inventory<1.5_and_not_flatten": inventory is not None and inventory < INVENTORY_FLATTEN
        and judgment.get("inventory_label") != "flatten",
        "liquidity>=1.0": liquidity is not None and liquidity >= LIQUIDITY_MIN,
    }

    if not gates["inventory<1.5_and_not_flatten"]:
        return {"action": "flatten_or_hold", "reason": "inventory pressure", "gates": gates, "risk_usd": 0}
    if not gates["toxic_flow<0.55"]:
        return {"action": "hold", "reason": f"toxic flow p={toxic}", "gates": gates, "risk_usd": 0}
    if not gates["quote_env>=0.55"]:
        return {"action": "hold", "reason": f"quote env p={quote_ok} < {QUOTE_ENV_MIN_P}", "gates": gates, "risk_usd": 0}
    if not gates["liquidity>=1.0"]:
        return {"action": "hold", "reason": f"liquidity score {liquidity} < {LIQUIDITY_MIN}", "gates": gates, "risk_usd": 0}
    if direction in ("long", "short"):
        return {
            "action": f"paper_{direction}",
            "reason": "all gates passed; dry-run paper intent only",
            "gates": gates,
            "risk_usd": min(200, int(snapshot.get("max_risk_usd", 200))),
            "take_usd": int(snapshot.get("take_usd", 400)),
        }
    return {"action": "hold", "reason": f"direction={direction}", "gates": gates, "risk_usd": 0}


def log_row(row: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="One dry-run cycle with the fake demo snapshot")
    parser.add_argument("--pump", action="store_true", help="One dry-run cycle on live pump.fun data (default)")
    parser.add_argument("--mint", default=None, help="pump.fun coin mint; default = most recently traded curve coin")
    args = parser.parse_args()

    t0 = time.time()
    if args.demo:
        snap = demo_snapshot()
    else:
        try:
            snap = pump_snapshot(args.mint)
        except Exception as e:  # network / schema errors -> logged hold, never an order
            snap = {"ts": datetime.now(timezone.utc).isoformat(), "source": "pump.fun", "mint": args.mint,
                    "fetch_error": f"{type(e).__name__}: {e}"}
    fetch_ms = round((time.time() - t0) * 1000, 1)

    if "fetch_error" in snap:
        judgment = {"error": f"snapshot fetch failed: {snap['fetch_error']}"}
    else:
        try:
            judgment = ask_jev(snap)
        except Exception as e:
            judgment = {"error": f"Jev call failed: {type(e).__name__}: {e}"}
    decision = decide(snap, judgment)
    elapsed_ms = round((time.time() - t0) * 1000, 1)
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "elapsed_ms": elapsed_ms,
        "fetch_ms": fetch_ms,
        "jev_ms": round(elapsed_ms - fetch_ms, 1),
        "snapshot": snap,
        "judgment": judgment,
        "decision": decision,
        "live_order": False,
    }
    log_row(row)
    print(json.dumps(row, indent=2))
    print(f"\nLogged to {LOG}")


if __name__ == "__main__":
    main()
