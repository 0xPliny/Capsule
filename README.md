<div align="center">

<pre align="center">
 ██████╗ █████╗ ██████╗ ███████╗██╗   ██╗██╗     ███████╗
██╔════╝██╔══██╗██╔══██╗██╔════╝██║   ██║██║     ██╔════╝
██║     ███████║██████╔╝███████╗██║   ██║██║     █████╗
██║     ██╔══██║██╔═══╝ ╚════██║██║   ██║██║     ██╔══╝
╚██████╗██║  ██║██║     ███████║╚██████╔╝███████╗███████╗
 ╚═════╝╚═╝  ╚═╝╚═╝     ╚══════╝ ╚═════╝ ╚══════╝╚══════╝
              $CAPS · pump desk · dry-run
</pre>

### A live, visual dry-run desk for [pump.fun](https://pump.fun)

`status` &nbsp;🟢 **Shipping** &nbsp;&nbsp; `version` &nbsp;📦 **0.2.0** &nbsp;&nbsp; `license` &nbsp;📄 **Apache-2.0** &nbsp;&nbsp; `mode` &nbsp;🧪 **Dry-run only**

### Built by **[0xPliny](https://x.com/0xPliny)** · tips welcome on X

---

[![Capsule dry-run desk](docs/capsule-desk.png)](https://github.com/0xPliny/Capsule)

</div>

**Capsule** is a local pump.fun desk: public market data in, gated judgment out, a paper graph and a quiet capsule mascot on screen. Deterministic code owns math and risk; optional [TypeSafe](https://typesafe.ai) / Jev calls own the six judgment questions. Fan tool that pays respects to Pump — **not affiliated with, endorsed by, or part of Pump**.

---

## Features

- **Dry-run by design** — no wallet, no trade POSTs, `live_order` is always `false`
- **Public pump.fun data** — coins, trades, and 1m candles over plain GETs
- **Offline demo + replay** — `--demo` and `--replay` never need pump.fun or TypeSafe
- **Paper desk tour** — coin → judgment → gates → action, with a quiet capsule mascot
- **Gate strands** — green PASS / red FAIL on toxic flow, quote environment, liquidity, inventory
- **CA → pump.fun** — mint links open the coin page when a mint is present
- **Header actions** — [Tips](https://x.com/0xPliny) · [GitHub](https://github.com/0xPliny/Capsule) · [X](https://x.com/0xPliny)
- **Apache-2.0** — use it, fork it, build on it; keep secrets out of the repo

---

## Quick start

### First five minutes

```bash
git clone https://github.com/0xPliny/Capsule.git
cd Capsule

# optional — only if you want live Jev judgment calls
export TYPESAFE_API_KEY=your_key

python desk.py --pump              # latest bonding-curve coin (public GETs)
python desk.py --mint <MINT>       # one specific coin (public GETs)
python desk.py --demo              # offline fake snapshot + canned judgment
python desk.py --replay tests/fixtures/replay_paper_long.jsonl

python dashboard/server.py         # local UI at http://127.0.0.1:8791/
```

The dashboard binds **127.0.0.1 only**, default port **8791** (next free port if 8791 is taken). Override with `CAPSULE_DASH_PORT`.

Open the printed URL → **Run dry-run**. Watch Capsule crawl the graph.

Windows `run.bat` is not in this repo yet — use the commands above.

### What “I need to…” maps to

| I need to… | Start here |
| --- | --- |
| See the desk UI | `python dashboard/server.py` |
| Score the latest curve coin | `python desk.py --pump` |
| Score a mint I already have | `python desk.py --mint <MINT>` |
| Demo with no network / no API key | `python desk.py --demo` |
| Replay a stored Decision / snapshot | `python desk.py --replay <path.jsonl\|snapshot.json>` |
| Run the safety + gate tests | `pip install -r requirements-dev.txt && pytest` |
| Tip the author | [x.com/0xPliny](https://x.com/0xPliny) |

---

## How it works

1. Build a compact market snapshot from a **source**: live pump.fun GETs (`--pump` / `--mint`), the offline demo, or a recorded file (`--replay`).
2. Ask six questions (live TypeSafe / Jev on `--pump` / `--mint`; canned or stored judgment offline): regime, direction, toxic flow, liquidity, quote environment, inventory.
3. Evaluate **all** declarative gates from `thresholds.json` (`default_v1`), then apply intent policy: inventory → toxic → quote_env → liquidity → direction → `paper_long` / `paper_short` / hold / flatten — **never** a live order.
4. Log a versioned Decision (`schema_v`, `run_id`, `ts`, `source`, `mint`, `snapshot`, `judgment`, per-gate outcomes, `intent`, `threshold_set_id`) to `data/decisions.jsonl` (gitignored) and animate the tour on the dashboard.

<p align="center">
  <img src="docs/capsule-mascot.png" alt="Capsule mascot poses" width="440" />
</p>

---

## Secrets & safety

- Never commit API keys, wallets, `.env`, or machine paths.
- Set `TYPESAFE_API_KEY` in the environment only.
- This product does **not** place trades. Unlocking live trading is a separate, explicit decision.

---

## Links

| | |
| --- | --- |
| Repository | [github.com/0xPliny/Capsule](https://github.com/0xPliny/Capsule) |
| X / tips | [x.com/0xPliny](https://x.com/0xPliny) |
| License | [Apache-2.0](LICENSE) |

---

## Creator

**Created and maintained by [0xPliny](https://x.com/0xPliny)**.

If Capsule helped you catch a gate snap before real money moved — that’s the point. Issues and PRs welcome.

---

## License

Copyright © 2026 0xPliny.

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).

---

<div align="center">

**Status: Shipping** &nbsp;·&nbsp; **Version 0.2.0** &nbsp;·&nbsp; **Dry-run · live_order false**

*Fan desk for pump.fun. Not Pump. Stay safe.*

</div>
