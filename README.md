# Prediction Hunt Fade Finder Trader: International Signals to Polymarket US

A lightweight, unattended worker for Prediction Hunt's real-time Fade Finder
WebSocket. Paper mode tracks every eligible Polymarket International alert.
Live mode maps selected International markets to Polymarket US before placing
an order. Markets must expire within the next rolling 72 hours, and all signals,
trades, mappings, and settlements stay in one SQLite database.

There is no dashboard or web server.

## Trading behavior

1. Connects to `wss://ws.predictionhunt.com` and subscribes to `fade_finder`.
2. Stores and deduplicates every valid signal, including reconnect snapshots.
3. Resolves the source Polymarket International market for paper pricing and
   classification.
4. Allows events already in progress, but rejects markets that are closed,
   expired, lack a verifiable expiration time, or expire outside the next
   72 hours.
5. Copies the signal's economic direction. `SELL YES` becomes `BUY NO`, and
   vice versa.
6. Paper mode attempts up to $10 total cost, including modeled fees, across
   every category and market type.
7. Walks the asks aggressively, but never buys above 10 cents over the
   normalized signal price. Partial immediate fills are allowed; no order is
   left resting.
8. Live mode applies optional category and market-type filters, strictly maps
   the International market to a US market, and submits a configurable fixed
   share count (10 by default).
9. Polls the platform used for each trade, then records payout, P&L, ROI, win
   rate, fees, and other aggregate statistics.

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

The database remains at `data/fade_finder.db`, so upgrading does not discard
the existing paper-trade history. New schema columns and the mapping table are
added automatically in place. Stop with Ctrl+C.

Run a read-only connectivity check (it never places an order):

```powershell
.\.venv\Scripts\python.exe -m fadebot.check_api
```

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

Live mode is implemented with the official `polymarket-us` Python SDK but is
intentionally difficult to enable. Paper mode needs no credentials. Put the
Key ID and Base64 Ed25519 Secret Key from `polymarket.us/developer` in `.env`,
then set all three gates:

```dotenv
TRADING_MODE=live
LIVE_TRADING_ENABLED=true
LIVE_TRADING_ACK=I_UNDERSTAND_REAL_MONEY_IS_AT_RISK
POLYMARKET_KEY_ID=your-key-id
POLYMARKET_SECRET_KEY=your-secret-key
LIVE_SHARES_PER_TRADE=10
# Empty means every category; comma-separated values are allowed.
LIVE_CATEGORY_FILTERS=sports
# team_winner means sports moneyline/winner markets.
LIVE_MARKET_TYPE_FILTERS=team_winner
```

Live entries use immediate-or-cancel limit orders with the same price ceiling, so
available liquidity may fill partially and the remainder is cancelled. Failed
submissions are not automatically retried, avoiding accidental duplicate
real-money orders. Validate with a dedicated low-balance wallet before use.

The live filters never limit paper collection. For example, with the settings
above, paper mode still collects crypto, politics, props, totals, and other
markets; live mode only submits mapped sports team-winner orders. Leave either
filter blank to allow every value in that dimension.

## International to US mapping

The live mapper follows the strict configuration principle used by
`Live-sport-trading-bot`, adapted for an unattended stream:

- sports team-winner markets match the team identity, scheduled game time, and
  US moneyline market type;
- other types require an exact normalized title and matching expiry;
- the mapper records which US LONG/SHORT side corresponds to the International
  YES side;
- successful mappings are cached in the `market_mappings` SQLite table;
- zero matches or multiple matches are rejected and logged—never guessed.

## Accounting notes

- Paper mode's $10 amount is a maximum cash outlay, not a guaranteed fill.
- Live mode submits up to `LIVE_SHARES_PER_TRADE` contracts with IOC execution;
  a thin book can produce a partial fill.
- Paper entries use the order book available when the worker handles the
  signal—not the earlier wallet execution price.
- The 10-cent guard is measured from the normalized buy price. For example,
  `SELL YES at 0.70` becomes `BUY NO at 0.30`, with a 0.40 maximum.
- Payout is shares multiplied by the selected outcome's final price on the
  platform recorded for that trade, including split or void resolutions.
- Portfolio ROI is settled P&L divided by settled cost basis.

Prediction Hunt provides a WebSocket channel, not an incoming webhook. The
worker maintains that persistent connection and reconnects automatically.

Sources: [Prediction Hunt Fade Finder WebSocket](https://www.predictionhunt.com/api/docs/websocket/channels/fade-finder),
[Polymarket US market books](https://docs.polymarket.us/api-reference/markets/get-market-book),
[Polymarket US order placement](https://docs.polymarket.us/api-reference/orders/create-order),
and [Polymarket US authentication](https://docs.polymarket.us/api-reference/authentication).
