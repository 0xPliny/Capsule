"""Local paper watchlist. Mints only — no keys, no orders.

Persists data/watchlist.json (or $CAPSULE_DATA_DIR). live_order is not a field here.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MAX_MINTS = 32
# Same shape the dashboard already accepts for a pump.fun coin address.
MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_LOCK = threading.Lock()


def data_dir() -> Path:
    override = os.environ.get("CAPSULE_DATA_DIR", "").strip()
    return Path(override) if override else ROOT / "data"


def watchlist_path() -> Path:
    return data_dir() / "watchlist.json"


def valid_mint(mint: str | None) -> bool:
    return bool(mint) and bool(MINT_RE.fullmatch(mint.strip()))


def _empty() -> dict:
    return {"mints": []}


def _clean_items(raw) -> list[dict]:
    if not isinstance(raw, list):
        raise ValueError("watchlist unreadable: expected mints list")
    out = []
    seen = set()
    for item in raw:
        if isinstance(item, str):
            mint, added = item.strip(), None
        elif isinstance(item, dict):
            mint = str(item.get("mint") or "").strip()
            added = item.get("added_at") if isinstance(item.get("added_at"), str) else None
        else:
            continue
        if not valid_mint(mint) or mint in seen:
            continue
        seen.add(mint)
        out.append({"mint": mint, "added_at": added})
    return out


def load_watchlist() -> dict:
    path = watchlist_path()
    if not path.is_file():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"watchlist unreadable: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("watchlist unreadable: expected an object")
    return {"mints": _clean_items(data.get("mints"))}


def mints() -> list[str]:
    return [item["mint"] for item in load_watchlist()["mints"]]


def _write(data: dict) -> None:
    path = watchlist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def add_mint(mint: str) -> dict:
    text = (mint or "").strip()
    if not valid_mint(text):
        return {"ok": False, "error": "mint must be a pump.fun coin address", "mints": [], "live_order": False}
    with _LOCK:
        data = load_watchlist()
        for item in data["mints"]:
            if item["mint"] == text:
                return {"ok": True, "added": False, "mints": data["mints"], "live_order": False}
        if len(data["mints"]) >= MAX_MINTS:
            return {
                "ok": False,
                "error": f"watchlist holds {MAX_MINTS} mints",
                "mints": data["mints"],
                "live_order": False,
            }
        data["mints"].append({
            "mint": text,
            "added_at": datetime.now(timezone.utc).isoformat(),
        })
        _write(data)
        return {"ok": True, "added": True, "mints": data["mints"], "live_order": False}


def remove_mint(mint: str) -> dict:
    text = (mint or "").strip()
    if not valid_mint(text):
        return {"ok": False, "error": "mint must be a pump.fun coin address", "mints": [], "live_order": False}
    with _LOCK:
        data = load_watchlist()
        kept = [item for item in data["mints"] if item["mint"] != text]
        removed = len(kept) != len(data["mints"])
        data["mints"] = kept
        if removed:
            _write(data)
        return {"ok": True, "removed": removed, "mints": kept, "live_order": False}
