"""Holder-cluster score for Capsule paper checks.

Public Solana JSON-RPC reads only (no keys, no trade methods). A finished
check with enough rows yields holder_risk.verdict OK, RISKY, or DANGER.
OK means no elevated cluster, not a buy clearance. Missing rows omit the
verdict and set state to error. Demo and fixture scans tag log lines SAMPLE.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone

# Offline mint the desk can try without a network. Not a live coin.
SAMPLE_CA = "PaperDemoMint111111111111111111111111111"

# Score is 0..1. Below OK_LT is not elevated (verdict OK). The gate uses the
# same cut: holder_cluster < 0.35. RISKY_LT and above is DANGER.
OK_LT = 0.35
RISKY_LT = 0.60

SOLANA_RPC = "https://api.mainnet-beta.solana.com"
READ_METHODS = frozenset({
    "getTokenLargestAccounts",
    "getMultipleAccounts",
    "getSignaturesForAddress",
    "getTransaction",
})
_TOP_N = 6
_REASON_LIMIT = 72
_LOG_LIMIT = 160
_NAME_LIMIT = 64
_SYMBOL_LIMIT = 32

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_MD_TARGET = r"\([^()]*(?:\([^()]*\)[^()]*)*\)"
_MD_IMG = re.compile(r"!\[[^\]]*\]" + _MD_TARGET)
_MD_LINK = re.compile(r"\[([^\]]*)\]" + _MD_TARGET)
_SCHEME = re.compile(r"(?i)\b(?:javascript|data|vbscript)\s*:")
_TAG = re.compile(r"<[^>]*>")
_B58 = re.compile(r"[1-9A-HJ-NP-Za-km-z]")
_FORBIDDEN_VERDICTS = frozenset({
    "CLEAN",
    "NO_CLUSTER",
    "NO_CLUSTER_FOUND",
    "WARN",
})


def plain_text(value, limit: int = _LOG_LIMIT) -> str:
    """Plain text only. Drops markup, links, and control characters."""
    if value is None:
        return ""
    text = str(value)
    text = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    text = _CONTROL.sub("", text)
    text = _MD_IMG.sub("", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _SCHEME.sub("", text)
    text = _TAG.sub("", text)
    text = text.replace("<", "").replace(">", "")
    text = "".join(ch for ch in text if ch.isprintable())
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: max(0, limit - 3)].rstrip() + "..."
        text = text[:limit]
    return text


def plain_ca(value, limit: int = 44) -> str:
    """Base58-looking contract text. Shell metacharacters and markup do not survive."""
    return "".join(_B58.findall(plain_text(value, 200)))[:limit]


def _public_line(value, limit: int) -> str:
    """Reason and log text. Markup is already gone; alarm tokens do not get a second life."""
    text = plain_text(value, max(limit, 1) + 32)
    text = re.sub(r"(?i)toxic_flow", "flow signal", text)
    text = re.sub(r"(?i)toxic", "flow signal", text)
    text = re.sub(r"(?i)no_cluster_found", "", text)
    text = re.sub(r"(?i)no_cluster", "", text)
    text = re.sub(r"(?i)\bclean\b", "", text)
    return plain_text(text, limit)


def _stored_label(value, limit: int = 64) -> str:
    """Plain label. If markup was the whole value, keep a stable id so equal rows still cluster."""
    raw = "" if value is None else str(value)
    text = plain_text(raw, limit)
    if raw.strip() and not text:
        text = "redacted-" + hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:8]
    return text


def _ts(snapshot: dict) -> str:
    raw = snapshot.get("ts") if isinstance(snapshot, dict) else None
    text = plain_text(raw, 40) if isinstance(raw, str) else ""
    if text:
        return text
    return datetime.now(timezone.utc).isoformat()


def _event(sample: bool, level: str, message: str, ts: str) -> dict:
    text = _public_line(message, _LOG_LIMIT)
    if sample:
        if not text.startswith("SAMPLE "):
            text = plain_text("SAMPLE " + text, _LOG_LIMIT)
        level = "SAMPLE"
    else:
        if text.startswith("SAMPLE "):
            text = _public_line(text[7:], _LOG_LIMIT)
        if level not in ("info", "warn", "error"):
            level = "info"
    return {"ts": ts, "level": level, "message": text}


def _is_sample(snapshot: dict) -> bool:
    holders = snapshot.get("holders") if isinstance(snapshot.get("holders"), dict) else {}
    if holders.get("sample") is True:
        return True
    source = str(snapshot.get("source_id") or snapshot.get("source") or "")
    if source == "demo":
        return True
    mint = (snapshot.get("coin") or {}).get("mint") or snapshot.get("mint")
    return plain_ca(mint) == SAMPLE_CA


def _owner(row: dict) -> str:
    return str(row.get("owner") or row.get("addr") or "")


def _slot(row: dict):
    slot = row.get("slot")
    if isinstance(slot, bool) or slot is None:
        return None
    if isinstance(slot, int):
        return slot
    if isinstance(slot, float) and slot.is_integer():
        return int(slot)
    return None


def _rows_complete(rows) -> bool:
    if not isinstance(rows, list) or not rows:
        return False
    for row in rows:
        if not isinstance(row, dict) or not _owner(row):
            return False
        if "funder" not in row:
            return False
        if _slot(row) is None and not row.get("ts"):
            return False
    return True


def _short(label: str) -> str:
    text = plain_text(label, 44)
    if len(text) <= 12:
        return text
    return text[:4] + ".." + text[-4:]


def _top10_pct(rows: list[dict], supply) -> int | None:
    try:
        total = float(supply)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    amounts = []
    for row in rows:
        amt = row.get("amount")
        if isinstance(amt, bool) or not isinstance(amt, (int, float)):
            continue
        amounts.append(float(amt))
    if not amounts:
        return None
    top = sorted(amounts, reverse=True)[:10]
    return int(round(sum(top) / total * 100))


def _ok_reason(pct: int | None) -> str:
    # Copy line for a finished check with no elevated cluster. Not a buy clearance.
    if pct is None:
        return "No large holder cluster found."
    return f"No large holder cluster found. Top 10 hold {pct}%."


def _score_rows(rows: list[dict], supply) -> tuple[float, str, str]:
    by_owner: dict[str, dict] = {}
    for row in rows:
        owner = _owner(row)
        if owner not in by_owner:
            by_owner[owner] = {
                "funder": row.get("funder"),
                "slot": _slot(row),
                "ts": row.get("ts"),
                "amount": 0.0,
            }
        amt = row.get("amount")
        if isinstance(amt, (int, float)) and not isinstance(amt, bool):
            by_owner[owner]["amount"] = float(by_owner[owner]["amount"]) + float(amt)
    owners = list(by_owner)
    n = len(owners)
    collapsed = []
    for owner in owners:
        item = dict(by_owner[owner])
        item["owner"] = owner
        item["amount"] = item["amount"]
        collapsed.append(item)

    funder_groups: dict[str, list[str]] = {}
    for owner, item in by_owner.items():
        funder = item.get("funder")
        if funder is None:
            continue
        key = str(funder).strip()
        if not key or key == owner:
            continue
        funder_groups.setdefault(key, []).append(owner)

    block_groups: dict[str, list[str]] = {}
    for owner, item in by_owner.items():
        slot = item.get("slot")
        if slot is not None:
            block_groups.setdefault(f"slot:{slot}", []).append(owner)
        elif item.get("ts"):
            bucket = plain_text(item.get("ts"), 19)
            if bucket:
                block_groups.setdefault(f"ts:{bucket}", []).append(owner)

    parent = {owner: owner for owner in owners}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: str, b: str) -> None:
        if a not in parent or b not in parent:
            return
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for funder, group in funder_groups.items():
        for owner in group[1:]:
            union(group[0], owner)
        if funder in parent:
            union(group[0], funder)

    def share(groups: dict[str, list[str]]) -> tuple[float, str | None, int]:
        if n <= 0 or not groups:
            return 0.0, None, 0
        label, group = max(groups.items(), key=lambda kv: len(kv[1]))
        count = len(group)
        if count < 2:
            return 0.0, None, 0
        return count / n, label, count

    funder_ratio, funder_label, funder_n = share(funder_groups)
    block_ratio, _block_label, _block_n = share(block_groups)
    components: dict[str, int] = {}
    for owner in owners:
        root = find(owner)
        components[root] = components.get(root, 0) + 1
    linked_n = max(components.values()) if components else 0
    linked_ratio = (linked_n / n) if linked_n >= 2 and n else 0.0

    value = max(funder_ratio, block_ratio, linked_ratio)
    value = round(min(1.0, max(0.0, value)), 6)
    if value < OK_LT:
        verdict = "OK"
        reason = _ok_reason(_top10_pct(collapsed, supply))
    else:
        verdict = "RISKY" if value < RISKY_LT else "DANGER"
        # Prefer the strongest observed pattern. Funder text is display-only.
        if funder_ratio >= block_ratio and funder_ratio >= linked_ratio and funder_n >= 2:
            shown = _short(_public_line(funder_label or "", 44))
            if shown:
                reason = f"{funder_n} wallets share funder {shown}"
            else:
                reason = f"{funder_n} wallets share a funder"
        elif block_ratio >= linked_ratio and block_ratio > 0:
            reason = f"same-block cluster {int(round(block_ratio * 100))}%"
        else:
            reason = f"linked wallets {linked_n}/{n}"
    return value, _public_line(reason, _REASON_LIMIT), verdict


def _risk(
    *,
    snapshot: dict,
    state: str,
    verdict: str | None,
    score: int | None,
    value: float | None,
    reason: str,
    endpoint: str,
    logs: list[dict],
) -> dict:
    if verdict in _FORBIDDEN_VERDICTS:
        verdict = None
        state = "error"
    if state != "done":
        verdict = None
        score = None
    if verdict not in ("OK", "RISKY", "DANGER"):
        verdict = None
    reason = _public_line(reason, _REASON_LIMIT)
    # Reason and log stay free of alarm tokens. Field names live on the gate, not here.
    sample = _is_sample(snapshot)
    ts = _ts(snapshot)
    log = []
    for item in logs:
        level = item.get("level") if isinstance(item, dict) else "info"
        message = item.get("message") if isinstance(item, dict) else item
        log.append(_event(sample, str(level or "info"), str(message or ""), ts))
    risk = {
        "verdict": verdict,
        "reason": reason,
        "state": state if state in ("idle", "scanning", "done", "error") else "error",
        "sample_ca": SAMPLE_CA,
        "is_sample": sample,
        "log": log,
    }
    if score is not None and verdict is not None:
        risk["score"] = int(max(0, min(100, score)))
    return {
        "value": value,
        "endpoint": plain_text(endpoint, 160) or SOLANA_RPC,
        "holder_risk": risk,
    }


def assess_holders(snapshot: dict | None) -> dict:
    """Score snapshot['holders'] or fail closed. Does not call the network."""
    snap = snapshot if isinstance(snapshot, dict) else {}
    holders = snap.get("holders") if isinstance(snap.get("holders"), dict) else {}
    if not holders:
        endpoint = "holders-missing"
    else:
        endpoint = str(holders.get("endpoint") or SOLANA_RPC)
    rows = holders.get("rows")
    prior = holders.get("log") if isinstance(holders.get("log"), list) else []
    if not _rows_complete(rows):
        if holders.get("error"):
            reason = "Holder read failed."
            message = "holder read failed"
        else:
            reason = "Holder data missing."
            message = "holder data missing"
        logs = list(prior) + [{"level": "error", "message": message}]
        return _risk(
            snapshot=snap,
            state="error",
            verdict=None,
            score=None,
            value=None,
            reason=reason,
            endpoint=endpoint,
            logs=logs,
        )
    value, reason, verdict = _score_rows(rows, holders.get("supply"))
    score = int(round(value * 100))
    level = "info" if verdict == "OK" else "warn"
    logs = list(prior) + [
        {"level": "info", "message": f"read {len(rows)} holder rows"},
        {"level": level, "message": reason},
    ]
    return _risk(
        snapshot=snap,
        state="done",
        verdict=verdict,
        score=score,
        value=value,
        reason=reason,
        endpoint=endpoint,
        logs=logs,
    )


def sample_holder_rows() -> list[dict]:
    """Eight independent rows. No shared funder and no same-block buys."""
    rows = []
    for i in range(8):
        rows.append({
            "owner": f"Owner{i}Demo11111111111111111111111111",
            "funder": f"Funder{i}Demo111111111111111111111111",
            "slot": 1000 + i,
            "amount": 2,
        })
    return rows


def sample_holder_bundle() -> dict:
    return {
        "rows": sample_holder_rows(),
        "supply": 100,
        "endpoint": "offline:sample-holders",
        "observed": True,
        "sample": True,
        "source_id": "demo",
    }


def sanitize_snapshot(snapshot: dict | None) -> dict:
    """Copy a snapshot with hostile name, symbol, and mint stored as plain text."""
    snap = copy.deepcopy(snapshot) if isinstance(snapshot, dict) else {}
    if "symbol" in snap:
        snap["symbol"] = plain_text(snap.get("symbol"), _SYMBOL_LIMIT)
    if "mint" in snap:
        snap["mint"] = plain_ca(snap.get("mint"))
    coin = snap.get("coin")
    if isinstance(coin, dict):
        if "name" in coin:
            coin["name"] = plain_text(coin.get("name"), _NAME_LIMIT)
        if "symbol" in coin:
            coin["symbol"] = plain_text(coin.get("symbol"), _SYMBOL_LIMIT)
        if "mint" in coin:
            coin["mint"] = plain_ca(coin.get("mint"))
    holders = snap.get("holders")
    if isinstance(holders, dict):
        if "endpoint" in holders:
            holders["endpoint"] = plain_text(holders.get("endpoint"), 160)
        if "error" in holders and holders.get("error") is not None:
            holders["error"] = plain_text(holders.get("error"), 120)
        rows = holders.get("rows")
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict):
                    continue
                # Keep raw funder for a later rescore, but drop markup from stored text.
                if "owner" in row:
                    row["owner"] = _stored_label(row.get("owner"), 64)
                if "funder" in row and row.get("funder") is not None:
                    row["funder"] = _stored_label(row.get("funder"), 64)
                if "addr" in row:
                    row["addr"] = _stored_label(row.get("addr"), 64)
    return snap


def _rpc_request(url: str, method: str, params, timeout: float = 8.0) -> dict:
    if method not in READ_METHODS:
        raise ValueError("rpc method is not a public read")
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Capsule dry-run",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            doc = json.loads(resp.read().decode("utf-8"))
    except TimeoutError as exc:
        raise TimeoutError("timed out") from exc
    except urllib.error.HTTPError as exc:
        raise ValueError(f"http {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ValueError("rpc unreachable") from exc
    if not isinstance(doc, dict):
        raise ValueError("rpc payload was not an object")
    if doc.get("error"):
        raise ValueError("rpc error")
    return doc


def _result(doc: dict):
    if isinstance(doc, dict) and "result" in doc:
        return doc.get("result")
    return doc


def _funder_from_tx(tx: dict, owner: str) -> str:
    if not isinstance(tx, dict):
        return ""
    message = ((tx.get("transaction") or {}).get("message") or {})
    blobs = list(message.get("instructions") or [])
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        if isinstance(inner, dict):
            blobs.extend(inner.get("instructions") or [])
    for ix in blobs:
        parsed = ix.get("parsed") if isinstance(ix, dict) else None
        if not isinstance(parsed, dict):
            continue
        if parsed.get("type") != "transfer":
            continue
        info = parsed.get("info") or {}
        if str(info.get("destination") or "") == owner:
            return str(info.get("source") or "")
    return ""


def _accounts_from_largest(doc: dict) -> list[dict]:
    result = _result(doc) or {}
    value = result.get("value") if isinstance(result, dict) else None
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:_TOP_N]:
        if not isinstance(item, dict) or not item.get("address"):
            continue
        amount = item.get("uiAmount")
        if amount is None and item.get("amount") is not None:
            try:
                decimals = int(item.get("decimals") or 0)
                amount = int(item["amount"]) / (10 ** decimals)
            except (TypeError, ValueError):
                amount = None
        out.append({"address": str(item["address"]), "amount": amount})
    return out


def _owners_from_accounts(doc: dict) -> list[str]:
    result = _result(doc)
    value = result.get("value") if isinstance(result, dict) else result
    if not isinstance(value, list):
        return []
    owners = []
    for item in value:
        info = (((item or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
        owners.append(str(info.get("owner") or ""))
    return owners


def fetch_holder_rows(mint: str, endpoints: list | None = None, transport=None) -> dict:
    """Top-holder read. Failure returns observed false so the gate can fail closed.

    transport(method, params) -> JSON-RPC object. Default is the public RPC.
    """
    url = SOLANA_RPC
    fetched_at = datetime.now(timezone.utc).isoformat()
    ep_list = endpoints if isinstance(endpoints, list) else None
    mint_text = plain_ca(mint)
    logs = [{"level": "info", "message": "rpc getTokenLargestAccounts"}]

    def finish(observed: bool, rows: list, error: str | None) -> dict:
        if ep_list is not None:
            ep_list.append({
                "name": "solana-rpc",
                "url": url,
                "ok": observed,
                "error": None if observed else "error",
                "fetched_at": fetched_at,
            })
        return {
            "rows": rows,
            "supply": None,
            "endpoint": url,
            "observed": observed,
            "sample": False,
            "error": error,
            "log": logs,
        }

    if not mint_text:
        return finish(False, [], "mint missing")

    def call(method: str, params):
        if transport is None:
            return _rpc_request(url, method, params)
        if method not in READ_METHODS:
            raise ValueError("rpc method is not a public read")
        return transport(method, params)

    try:
        largest = call("getTokenLargestAccounts", [mint_text])
        accounts = _accounts_from_largest(largest)
        if not accounts:
            return finish(False, [], "no holder accounts")
        logs.append({"level": "info", "message": "rpc getMultipleAccounts"})
        multi = call("getMultipleAccounts", [[a["address"] for a in accounts], {"encoding": "jsonParsed"}])
        owners = _owners_from_accounts(multi)
        if len(owners) != len(accounts) or not all(owners):
            return finish(False, [], "holder owners missing")
        rows = []
        for account, owner in zip(accounts, owners):
            sigs_doc = call("getSignaturesForAddress", [owner, {"limit": 20}])
            sigs = _result(sigs_doc)
            if not isinstance(sigs, list) or not sigs:
                return finish(False, [], "holder signatures missing")
            oldest = sigs[-1] if isinstance(sigs[-1], dict) else {}
            signature = oldest.get("signature")
            slot = oldest.get("slot")
            if not signature or not isinstance(slot, int):
                return finish(False, [], "holder slot missing")
            tx_doc = call("getTransaction", [signature, {
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": 0,
            }])
            funder = _funder_from_tx(_result(tx_doc) or {}, owner)
            rows.append({
                "owner": owner,
                "funder": funder,
                "slot": slot,
                "amount": account.get("amount"),
            })
        logs.append({"level": "info", "message": f"holder rows {len(rows)}"})
        return finish(True, rows, None)
    except ValueError as exc:
        logs.append({"level": "error", "message": plain_text(exc, 80) or "holder read failed"})
        return finish(False, [], "holder read failed")
    except Exception:
        logs.append({"level": "error", "message": "holder read failed"})
        return finish(False, [], "holder read failed")
