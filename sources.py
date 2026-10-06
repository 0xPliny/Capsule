"""Snapshot sources for Capsule: pump, mint, demo, recorded-file.

Replay never touches pump.fun or TypeSafe. Pump/mint are public GETs only.
"""
from __future__ import annotations

import json
import math
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

PUMP_FRONTEND = "https://frontend-api-v3.pump.fun"
PUMP_SWAP = "https://swap-api.pump.fun"
UA = {"User-Agent": "Mozilla/5.0 (Capsule dry-run)", "Accept": "application/json"}
INITIAL_REAL_TOKEN_RESERVES = 793_100_000 * 10**6  # pump.fun bonding curve sellable supply (raw units)

LEGACY_GATE_NAMES = {
    "toxic_flow<0.55": "toxic_flow",
    "quote_env>=0.55": "quote_env",
    "inventory<1.5_and_not_flatten": "inventory",
    "liquidity>=1.0": "liquidity",
}


class SnapshotSource(Protocol):
    """Load a market snapshot. Optional judgment means the caller may ask TypeSafe."""

    name: str

    def load(self) -> tuple[dict, dict | None]:
        """Return (snapshot, judgment_or_None). None = caller decides whether to ask."""


def _get_json(url: str, timeout: float = 10.0):
    req = urllib.request.Request(url, headers=UA, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def demo_snapshot() -> dict:
    """Stand-in book state. Replace with real venue feed later."""
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": "demo",
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


def demo_judgment() -> dict:
    """Offline stand-in so --demo never needs TypeSafe."""
    return {
        "regime": "chop",
        "regime_conf": 0.72,
        "direction": "long",
        "direction_conf": 0.64,
        "toxic_flow": 0.22,
        "liquidity": 1.4,
        "liquidity_label": "ok",
        "quote_env": 0.71,
        "inventory": 0.1,
        "inventory_label": "fine",
        "raw_available": False,
        "source": "demo",
    }


class DemoSource:
    name = "demo"

    def load(self) -> tuple[dict, dict | None]:
        return demo_snapshot(), demo_judgment()


class PumpSource:
    """Live public GETs. name is 'mint' when a mint is pinned, else 'pump'."""

    def __init__(self, mint: str | None = None):
        self.mint = mint
        self.name = "mint" if mint else "pump"

    def load(self) -> tuple[dict, dict | None]:
        return pump_snapshot(self.mint), None


class ReplaySource:
    """Recorded Decision jsonl / Decision JSON / bare snapshot. No network."""

    name = "replay"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> tuple[dict, dict | None]:
        row = load_replay(self.path)
        snap = row["snapshot"]
        judgment = row.get("judgment")
        if judgment is None:
            judgment = {"error": "replay has no judgment; TypeSafe is not called offline"}
        return snap, judgment


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

    last_px = _f(trades[0].get("priceUsd")) if trades else None
    supply = _f(coin.get("total_supply"), 0) / 10**base_dec
    mcap_usd = _f(coin.get("market_cap_usd"))
    if last_px is None and mcap_usd and supply:
        last_px = mcap_usd / supply

    vtok = _f(coin.get("virtual_token_reserves"), 0) / 10**base_dec
    quote_res_usd = (last_px * vtok) if (last_px and vtok and not complete) else None
    depth_2pct = quote_res_usd * (math.sqrt(1.02) - 1) if quote_res_usd else None
    impact_100_bps = (2 * 100 / quote_res_usd) * 1e4 if quote_res_usd else None
    real_tok = _f(coin.get("real_token_reserves"), 0)
    curve_progress = None if complete else round(max(0.0, min(1.0, 1 - real_tok / INITIAL_REAL_TOKEN_RESERVES)), 4)

    buys = [t for t in trades if t.get("type") == "buy"]
    sells = [t for t in trades if t.get("type") == "sell"]
    buy_usd = sum(_f(t.get("amountUsd"), 0) for t in buys)
    sell_usd = sum(_f(t.get("amountUsd"), 0) for t in sells)
    tot = buy_usd + sell_usd
    span_min = None
    if len(trades) >= 2:
        ts = [datetime.fromisoformat(t["timestamp"].replace("Z", "+00:00")).timestamp() for t in trades]
        span_min = max((max(ts) - min(ts)) / 60, 1 / 60)
    unique_addrs = {t.get("userAddress") for t in trades}
    top_trade = max((_f(t.get("amountUsd"), 0) for t in trades), default=0)

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
            "unique_addrs": len(unique_addrs),
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


def _legacy_gates_to_list(gates: dict) -> list[dict]:
    out = []
    for i, (k, v) in enumerate(gates.items()):
        name = LEGACY_GATE_NAMES.get(k, k)
        out.append({
            "name": name,
            "field": name,
            "op": "legacy",
            "threshold": None,
            "order": i + 1,
            "value": None,
            "passed": bool(v),
        })
    return out


def migrate_row(row: dict) -> dict:
    """Lift a v1 Decision or a pre-schema log row into the versioned shape (replay only)."""
    if not isinstance(row, dict):
        raise ValueError("replay row is not an object")
    if row.get("schema_v"):
        if "snapshot" not in row:
            raise ValueError("Decision record missing snapshot")
        return row
    if "snapshot" in row and ("judgment" in row or "decision" in row or "intent" in row):
        dec = row.get("decision") or {}
        intent = row.get("intent") or {
            "action": dec.get("action"),
            "reason": dec.get("reason"),
            "risk_usd": dec.get("risk_usd", 0),
        }
        if dec.get("take_usd") is not None:
            intent["take_usd"] = dec["take_usd"]
        gates = row.get("gates")
        if not isinstance(gates, list):
            gates = _legacy_gates_to_list(dec.get("gates") or {})
        snap = row["snapshot"]
        mint = ((snap.get("coin") or {}).get("mint") or snap.get("mint") or row.get("mint"))
        return {
            "schema_v": 1,
            "run_id": row.get("run_id") or "migrated",
            "ts": row.get("ts"),
            "source": row.get("source") or "replay",
            "mint": mint,
            "snapshot": snap,
            "judgment": row.get("judgment"),
            "gates": gates,
            "intent": intent,
            "threshold_set_id": row.get("threshold_set_id") or "legacy",
            "live_order": False,
        }
    # Bare snapshot JSON (optionally with a nested/top-level judgment).
    snap = row.get("snapshot") if isinstance(row.get("snapshot"), dict) and "price" in row.get("snapshot", {}) else row
    return {
        "schema_v": 1,
        "run_id": "replay-snapshot",
        "ts": snap.get("ts"),
        "source": "replay",
        "mint": (snap.get("coin") or {}).get("mint") or snap.get("mint"),
        "snapshot": snap,
        "judgment": row.get("judgment"),
        "gates": [],
        "intent": {},
        "threshold_set_id": None,
        "live_order": False,
    }


def load_replay(path: Path | str) -> dict:
    """Load the last usable record from a .jsonl Decision log or a single JSON file."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"replay file not found: {p}")
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"replay file empty: {p}")
    if p.suffix == ".jsonl" or (text.count("\n") and all(
        not line.strip() or line.strip().startswith("{") for line in text.splitlines()
    )):
        last = None
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            last = migrate_row(json.loads(line))
        if last is None:
            raise ValueError(f"replay jsonl had no rows: {p}")
        return last
    return migrate_row(json.loads(text))
