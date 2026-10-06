"""Fail the build if live trading, remote POSTs, or key material show up."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import dashboard.server as dash_server
from desk import build_record
from gates import decide
from sources import DemoSource

ROOT = Path(__file__).resolve().parent.parent
TRADE_MODULES = [
    ROOT / "desk.py",
    ROOT / "gates.py",
    ROOT / "sources.py",
    ROOT / "dashboard" / "server.py",
]

# Trading-path patterns. unique_addrs (public flow count) is not a signing key.
PRIV_RE = re.compile(r"privkey|private_key|secret_key|mnemonic", re.I)
SIGN_RE = re.compile(r"sign_transaction|sign_message|Keypair|solders|nacl\.signing", re.I)
WALLET_RE = re.compile(r"(?<![A-Za-z_])wallet(?!s\b)", re.I)
LIVE_TRUE_RE = re.compile(r"""live_order["']?\s*[:=]\s*True""")
OUTBOUND_POST_RE = re.compile(
    r"""method\s*=\s*["']POST["']|requests\.post|httpx\.post|urllib\.request\.urlopen\([^)]*data\s*="""
)
REMOTE_URL_RE = re.compile(r"https?://(?!127\.0\.0\.1|localhost)[^\s\"']+", re.I)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_live_order_always_false_on_record():
    snap, judgment = DemoSource().load()
    decided = decide(snap, judgment)
    row = build_record(
        source="demo", snapshot=snap, judgment=judgment, decided=decided,
        elapsed_ms=1, fetch_ms=0,
    )
    assert row["live_order"] is False
    assert decided["intent"]["action"].startswith("paper_") or decided["intent"]["action"] in (
        "hold",
        "flatten_or_hold",
    )


def test_live_order_never_assigned_true():
    for path in TRADE_MODULES:
        src = _read(path)
        assert LIVE_TRUE_RE.search(src) is None, f"live_order True in {path.name}"


def test_no_wallet_privkey_sign_in_trading_paths():
    for path in TRADE_MODULES:
        src = _read(path)
        assert PRIV_RE.search(src) is None, f"key material pattern in {path.name}"
        assert SIGN_RE.search(src) is None, f"signing pattern in {path.name}"
        # Allow comments/docs that forbid wallets; fail on identifiers / assignments.
        for i, line in enumerate(src.splitlines(), 1):
            stripped = line.split("#", 1)[0]
            if WALLET_RE.search(stripped):
                raise AssertionError(f"wallet pattern in {path.name}:{i}: {line.strip()}")


def test_no_outbound_post_off_localhost():
    for path in TRADE_MODULES:
        src = _read(path)
        if path.name == "server.py":
            # Inbound same-origin /api/run is local; outbound must stay off.
            assert "HOST = \"127.0.0.1\"" in src or "HOST = '127.0.0.1'" in src
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = ""
                    if isinstance(func, ast.Attribute):
                        name = func.attr
                    elif isinstance(func, ast.Name):
                        name = func.id
                    if name.lower() in {"urlopen", "request"} or name == "post":
                        raise AssertionError(f"outbound HTTP call in dashboard: {ast.dump(node)}")
            continue
        assert OUTBOUND_POST_RE.search(src) is None, f"outbound POST in {path.name}"
        # Public pump.fun GETs are allowed; they must be GET-only Requests.
        if "Request(" in src:
            assert 'method="GET"' in src or "method='GET'" in src


def test_dashboard_binds_localhost_only():
    assert dash_server.HOST in ("127.0.0.1", "localhost")
    assert dash_server.BASE_PORT == 8791 or isinstance(dash_server.BASE_PORT, int)


def test_holder_rpc_rejects_trade_methods():
    import holders

    called = []

    def transport(method, params):
        called.append(method)
        return {}

    try:
        holders.fetch_holder_rows("PaperDemoMint111111111111111111111111111", transport=transport)
    except Exception:
        pass
    assert "sendTransaction" not in called
    assert holders.READ_METHODS.isdisjoint({
        "sendTransaction", "signTransaction", "simulateTransaction",
    })
    src = _read(ROOT / "holders.py")
    assert "sendTransaction" not in src
    assert "signTransaction" not in src
    assert LIVE_TRUE_RE.search(src) is None
    assert "api.mainnet-beta.solana.com" in src
    for line in src.splitlines():
        code = line.split("#", 1)[0]
        if WALLET_RE.search(code):
            raise AssertionError(f"wallet pattern in holders.py: {line.strip()}")


def test_pump_urls_are_get_only():
    src = _read(ROOT / "sources.py")
    for match in REMOTE_URL_RE.finditer(src):
        url = match.group(0)
        assert "pump.fun" in url
    assert 'method="POST"' not in src
    assert "method='POST'" not in src
