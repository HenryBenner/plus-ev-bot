# Prediction Hunt Fade Finder Trader

A lightweight, unattended worker for Prediction Hunt's real-time Fade Finder
WebSocket. It supports every market category, accepts only markets that
start or expire within the next rolling 72 hours, and stores trades and
settlements in SQLite. Paper mode is the safe default.

There is no dashboard or web server.

## Trading behavior

1. Connects to `wss://ws.predictionhunt.com` and subscribes to `fade_finder`.
2. Stores and deduplicates every valid signal, including reconnect snapshots.
3. Resolves the corresponding Polymarket market and its event/expiry time.
4. Rejects markets that have started, are closed, lack a verifiable time, or
   fall outside the next 72 hours.
5. Copies the signal's economic direction. `SELL YES` becomes `BUY NO`, and
   vice versa.
6. Attempts up to $10 total cost, including modeled fees.
7. Walks the asks aggressively, but never buys above 10 cents over the
   normalized signal price. Partial immediate fills are allowed; no order is
   left resting.
8. Polls Polymarket for resolution, then records payout, P&L, ROI, win rate,
   fees, and other aggregate statistics.

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

## Run continuously on a VPS

```bash
git clone https://github.com/HenryBenner/plus-ev-bot.git
cd plus-ev-bot
cp .env.example .env
# Edit .env and add PREDICTION_HUNT_API_KEY.
docker compose up -d --build
docker compose logs -f
```

Print results without stopping the worker:

```bash
docker compose exec fade-bot fade-bot stats
```

SQLite uses WAL mode and the container has no inbound port, keeping resource
usage low enough to run alongside other small workers. The Compose volume
preserves results across rebuilds.

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
