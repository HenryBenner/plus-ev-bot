# Prediction Hunt Fade Finder Trader

A lightweight, unattended worker for Prediction Hunt's real-time Fade Finder
WebSocket. It supports every market category, accepts only markets that
expire within the next rolling 72 hours, and stores trades and settlements
in SQLite. Paper mode is the safe default.

There is no dashboard or web server.

## Trading behavior

1. Connects to `wss://ws.predictionhunt.com` and subscribes to `fade_finder`.
2. Stores and deduplicates every valid signal, including reconnect snapshots.
3. Resolves the corresponding Polymarket market and its event/expiry time.
4. Allows events already in progress, but rejects markets that are closed,
   expired, lack a verifiable expiration time, or expire outside the next
   72 hours.
5. Copies the signal's economic direction. `SELL YES` becomes `BUY NO`, and
   vice versa.
6. Attempts up to $10 total cost, including modeled fees.
7. Walks the asks aggressively, but never buys above 10 cents over the
   normalized signal price. Partial immediate fills are allowed; no order is
   left resting.
8. Polls Polymarket for resolution, then records payout, P&L, ROI, win rate,
   fees, and other aggregate statistics.

On startup, the worker also reconsiders signals stored by older versions with
the rejection reason `event_already_started`. These are entered only at the
current order book—not a fabricated historical price—and must still pass the
72-hour expiration, open-market, liquidity, and 10-cent price checks.

## Run locally in paper mode

Python 3.11 or newer is required.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Edit `.env` and set `PREDICTION_HUNT_API_KEY`. Leave these safety defaults:

```dotenv
TRADING_MODE=paper
PAPER_STAKE_USD=10.00
MAX_EVENT_HOURS=72
MAX_PRICE_DRIFT=0.10
LIVE_TRADING_ENABLED=false
```

Start the worker:

```powershell
.\.venv\Scripts\python.exe -m fadebot.main run
```

In another terminal, print current results:

```powershell
.\.venv\Scripts\python.exe -m fadebot.main stats
```

The database is stored at `data/fade_finder.db`. Stop with Ctrl+C.

## Run in a Python environment on a Linux VPS

```bash
sudo apt update
sudo apt install -y git python3 python3-venv
git clone https://github.com/HenryBenner/plus-ev-bot.git
cd plus-ev-bot
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install .
cp .env.example .env
nano .env
```

Add `PREDICTION_HUNT_API_KEY` in `.env`, leave `TRADING_MODE=paper`, then
start the worker:

```bash
./.venv/bin/python -m fadebot.main run
```

To keep it running after disconnecting from SSH, install the included systemd
service. These commands assume the repository is at
`/home/YOUR_USER/plus-ev-bot`:

```bash
sed "s|YOUR_USER|$USER|g" deploy/fade-bot.service.example > fade-bot.service
sudo cp fade-bot.service /etc/systemd/system/fade-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now fade-bot
sudo systemctl status fade-bot
```

Follow the worker logs:

```bash
sudo journalctl -u fade-bot -f
```

Print results at any time:

```bash
cd ~/plus-ev-bot
./.venv/bin/python -m fadebot.main stats
```

Update the VPS later with:

```bash
cd ~/plus-ev-bot
git pull
./.venv/bin/python -m pip install .
sudo systemctl restart fade-bot
```

SQLite uses WAL mode and requires no database server. The worker has no
inbound port, keeping it lightweight enough to run alongside other programs.

## Live mode

Live mode is implemented but intentionally difficult to enable. Paper mode
needs none of the wallet variables. Before eventually testing live mode:

```powershell
python -m pip install -e ".[live]"
```

Then supply the Polymarket credentials in `.env` and set all three gates:

```dotenv
TRADING_MODE=live
LIVE_TRADING_ENABLED=true
LIVE_TRADING_ACK=I_UNDERSTAND_REAL_MONEY_IS_AT_RISK
```

Live entries use Fill-And-Kill limit orders with the same price ceiling, so
available liquidity may fill partially and the remainder is cancelled. Failed
submissions are not automatically retried, avoiding accidental duplicate
real-money orders. Validate with a dedicated low-balance wallet before use.

## Accounting notes

- The $10 amount is a maximum cash outlay, not a guaranteed fill.
- Paper entries use the order book available when the worker handles the
  signal—not the earlier wallet execution price.
- The 10-cent guard is measured from the normalized buy price. For example,
  `SELL YES at 0.70` becomes `BUY NO at 0.30`, with a 0.40 maximum.
- Payout is shares multiplied by the selected outcome's final Polymarket
  price, including split or void resolutions.
- Portfolio ROI is settled P&L divided by settled cost basis.

Prediction Hunt provides a WebSocket channel, not an incoming webhook. The
worker maintains that persistent connection and reconnects automatically.

Sources: [Prediction Hunt Fade Finder WebSocket](https://www.predictionhunt.com/api/docs/websocket/channels/fade-finder),
[Polymarket order books](https://docs.polymarket.com/api-reference/market-data/get-order-book),
[Polymarket order placement](https://docs.polymarket.com/trading/place-orders),
and [Polymarket authentication](https://docs.polymarket.com/api-reference/authentication).
