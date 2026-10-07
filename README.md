# altbot

Personal, non-custodial spot trading engine. Funds never leave your exchange account.

## Quick start (paper mode, no keys)
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q                          # 22 tests: idempotency, recovery, risk, paper fills
python -m altbot strategies        # what can be assigned to a coin
# add coins under `coins:` in config.yaml, then:
python -m altbot validate          # checks each coin is a live spot market + min sizes
python -m altbot run               # Ctrl-C stops cleanly; restart resumes from the DB
python -m altbot status            # open orders, balances, halt flag
```

## Adding a coin
```yaml
coins:
  - symbol: SOL
    strategy: watch      # observe only until a real strategy exists
```

## Adding a strategy
Copy `altbot/strategies/_template.py` to `altbot/strategies/<name>.py`, uncomment `@register`.
It is auto-discovered. Strategies return intents; they cannot touch the exchange.

## Safety layers
| Layer | Mechanism |
|---|---|
| Duplicate orders | Deterministic clientOrderId + DB primary key + exchange-side duplicate rejection |
| Second copy of bot | OS file lock (`data/altbot.lock`) |
| Crash mid-order | Write-ahead PENDING row; reconciled against exchange on boot |
| Timeout on order POST | Marked UNKNOWN, looked up by clientOrderId, never blindly resent |
| Runaway losses | Per-coin cap, total cap, drawdown halt (persists until `altbot resume`) |
| Panic button | `touch KILL` blocks new orders on the next tick |
| Key theft | Trade-only key check (Binance), IP-restriction warning, secrets redacted from logs |
| Accidental live | `mode: live` **and** `ALTBOT_LIVE_CONFIRM` env var both required |

## Modes
`paper` → `testnet` → `live`. Do not skip a stage.

## Deploy
See `deploy/altbot.service` (systemd, `Restart=always`, filesystem-hardened).
