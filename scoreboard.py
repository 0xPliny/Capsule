"""Paper outcome scoreboard. Records what a dry-run would have done.

Each decision stores snapshot price and time. After the horizon, a later print
fills price_delta and paper_delta. A missing price stays insufficient.
Nothing here sends an order. live_order on every mark is false.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from watchlist import data_dir

# Later print that closes a mark. Override per call from the CLI.
PAPER_HORIZON_MIN = 5.0
MAX_MARKS = 200
_LOCK = threading.Lock()


def scoreboard_path() -> Path:
    return data_dir() / "scoreboard.json"


def snapshot_price(snapshot: dict):
    """Last trade in USD, else mid. None when the print has no usable price."""
    if not isinstance(snapshot, dict):
        return None
    price = snapshot.get("price") or {}
    if not isinstance(price, dict):
        return None
    for key in ("last_usd", "mid"):
        val = _num(price.get(key))
        if val is not None:
            return val
    return None


def _num(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _parse_ts(ts) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _entry_time(row: dict) -> str | None:
    snap = row.get("snapshot") or {}
    if isinstance(snap, dict) and snap.get("ts"):
        return snap.get("ts")
    return row.get("ts")


def _empty() -> dict:
    return {"horizon_min": PAPER_HORIZON_MIN, "marks": []}


def load_scoreboard() -> dict:
    path = scoreboard_path()
    if not path.is_file():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"scoreboard unreadable: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("marks"), list):
        raise ValueError("scoreboard unreadable: expected marks list")
    marks = []
    for mark in data["marks"]:
        if not isinstance(mark, dict):
            continue
        kept = dict(mark)
        kept["live_order"] = False
        marks.append(kept)
    horizon = _num(data.get("horizon_min"))
    return {"horizon_min": horizon if horizon is not None else PAPER_HORIZON_MIN, "marks": marks}


def _trim(marks: list[dict]) -> list[dict]:
    if len(marks) <= MAX_MARKS:
        return marks
    open_marks = [m for m in marks if m.get("status") == "open"]
    closed = [m for m in marks if m.get("status") != "open"]
    room = MAX_MARKS - len(open_marks)
    if room < 0:
        return open_marks[-MAX_MARKS:]
    return closed[-room:] + open_marks if room else open_marks


def _write(data: dict) -> None:
    path = scoreboard_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"horizon_min": data.get("horizon_min", PAPER_HORIZON_MIN), "marks": _trim(data.get("marks") or [])}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _paper_delta(action: str | None, price_delta: float) -> float:
    if action == "paper_long":
        return price_delta
    if action == "paper_short":
        return -price_delta
    # Hold and flatten are not a paper position. The price move is still recorded.
    return 0.0


def open_mark(row: dict, *, horizon_min: float | None = None) -> dict:
    """Record entry price and time for one Decision. Does not fill the exit."""
    horizon = PAPER_HORIZON_MIN if horizon_min is None else float(horizon_min)
    if horizon < 0:
        raise ValueError("horizon_min must be >= 0")
    snap = row.get("snapshot") if isinstance(row.get("snapshot"), dict) else {}
    when = _entry_time(row)
    px = snapshot_price(snap)
    intent = row.get("intent") or {}
    mark_id = row.get("run_id") or uuid.uuid4().hex
    parsed = _parse_ts(when)
    usable_px = px is not None and px > 0
    if parsed is None or not usable_px:
        missing = []
        if not usable_px:
            missing.append("entry price")
        if parsed is None:
            missing.append("entry time")
        mark = {
            "id": mark_id,
            "mint": row.get("mint"),
            "action": intent.get("action"),
            "entry_ts": when,
            "entry_px": px if usable_px else None,
            "horizon_min": horizon,
            "due_ts": None,
            "exit_ts": None,
            "exit_px": None,
            "price_delta": None,
            "paper_delta": None,
            "status": "insufficient",
            "reason": "insufficient data: missing " + " and ".join(missing),
            "source": row.get("source"),
            "live_order": False,
        }
    else:
        mark = {
            "id": mark_id,
            "mint": row.get("mint"),
            "action": intent.get("action"),
            "entry_ts": when,
            "entry_px": px,
            "horizon_min": horizon,
            "due_ts": (parsed + timedelta(minutes=horizon)).isoformat(),
            "exit_ts": None,
            "exit_px": None,
            "price_delta": None,
            "paper_delta": None,
            "status": "open",
            "source": row.get("source"),
            "live_order": False,
        }
    with _LOCK:
        data = load_scoreboard()
        for existing in data["marks"]:
            if existing.get("id") == mark_id:
                existing["live_order"] = False
                return existing
        data["marks"].append(mark)
        data["horizon_min"] = horizon
        _write(data)
    return mark


def settle_with_print(mint, price, observed_ts: str | None, *, exclude_id: str | None = None) -> list[dict]:
    """Fill open marks for this mint whose horizon has elapsed. No price, no delta."""
    px = _num(price)
    obs = _parse_ts(observed_ts)
    if px is None or obs is None:
        return []
    with _LOCK:
        data = load_scoreboard()
        settled = []
        changed = False
        for mark in data["marks"]:
            if mark.get("status") != "open":
                continue
            if exclude_id and mark.get("id") == exclude_id:
                continue
            if mark.get("mint") != mint:
                continue
            due = _parse_ts(mark.get("due_ts"))
            if due is None or obs < due:
                continue
            changed = True
            entry = _num(mark.get("entry_px"))
            mark["live_order"] = False
            if entry is None or entry <= 0:
                mark["status"] = "insufficient"
                mark["reason"] = "insufficient data: missing entry price"
                mark["price_delta"] = None
                mark["paper_delta"] = None
                settled.append(dict(mark))
                continue
            price_delta = (px - entry) / entry
            mark["exit_px"] = px
            mark["exit_ts"] = observed_ts
            mark["price_delta"] = round(price_delta, 6)
            mark["paper_delta"] = round(_paper_delta(mark.get("action"), price_delta), 6)
            mark["status"] = "marked"
            settled.append(dict(mark))
        if changed:
            _write(data)
        return settled


def list_marks() -> dict:
    data = load_scoreboard()
    marks = list(reversed(data["marks"]))
    for mark in marks:
        mark["live_order"] = False
    return {"horizon_min": data["horizon_min"], "marks": marks, "live_order": False}
