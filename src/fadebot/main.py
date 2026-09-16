from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

from .config import Settings
from .db import Database
from .service import TradingService


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prediction Hunt Fade Finder trader"
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("run", "stats"),
        default="run",
        help="run the worker (default) or print stored performance statistics",
    )
    parser.add_argument(
        "--mode", choices=("paper", "live"),
        help="filter stats to paper or live trades",
    )
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    database = Database(settings.database_path)
    if args.command == "stats":
        asyncio.run(print_stats(database, args.mode))
    else:
        if args.mode:
            parser.error("--mode is only valid with stats")
        try:
            asyncio.run(run_worker(settings, database))
        except KeyboardInterrupt:
            pass


async def run_worker(settings: Settings, database: Database) -> None:
    await database.initialize()
    service = TradingService(settings, database)
    await service.start()
    logging.getLogger(__name__).info(
        "Fade Finder trader is running in %s mode. Press Ctrl+C to stop.",
        settings.trading_mode.upper(),
    )
    try:
        await asyncio.Event().wait()
    finally:
        await service.stop()


async def print_stats(database: Database, mode: str | None = None) -> None:
    await database.initialize()
    summary = await database.summary(mode)
    recent = await database.recent_trades(10, mode)
    rows: list[tuple[str, Any, str]] = [
        ("Signals received (all)", summary.get("total_signals") or 0, "number"),
        ("Trades", summary.get("total_trades") or 0, "number"),
        ("Open trades", summary.get("open_trades") or 0, "number"),
        ("Settled trades", summary.get("settled_trades") or 0, "number"),
        ("Wins", summary.get("wins") or 0, "number"),
        ("Losses", summary.get("losses") or 0, "number"),
        ("Win rate", summary.get("win_rate"), "percent"),
        ("Total fees", summary.get("total_fees") or 0, "money"),
        ("Settled P&L", summary.get("pnl") or 0, "money"),
        ("Portfolio ROI", summary.get("portfolio_roi"), "percent"),
        ("Average trade ROI", summary.get("avg_trade_roi"), "percent"),
        ("Best trade ROI", summary.get("best_roi"), "percent"),
        ("Worst trade ROI", summary.get("worst_roi"), "percent"),
        ("Profit factor", summary.get("profit_factor"), "decimal"),
    ]
    print(f"\nFADE FINDER TRADING STATS{f' ({mode.upper()})' if mode else ''}")
    print("=" * 43)
    for label, value, kind in rows:
        print(f"{label:<24} {_format_value(value, kind):>18}")

    rejections = summary.get("rejections") or []
    if rejections:
        print("\nREJECTED SIGNALS")
        print("-" * 43)
        for rejection in rejections:
            print(f"{rejection['reason']:<32} {rejection['count']:>10}")

    if recent:
        print("\nRECENT TRADES")
        print("-" * 95)
        print(
            f"{'Market':<38} {'Mode':<6} {'Side':<5} {'Entry':>7} "
            f"{'Status':<8} {'P&L':>10} {'ROI':>9}"
        )
        for trade in recent:
            title = str(trade["title"])[:37]
            pnl = _format_value(trade.get("pnl"), "money")
            roi = _format_value(trade.get("roi"), "percent")
            print(
                f"{title:<38} {trade['execution_mode']:<6} {trade['outcome']:<5} "
                f"{trade['fill_avg_price']:>7.3f} {trade['status']:<8} "
                f"{pnl:>10} {roi:>9}"
            )
    print()


def _format_value(value: Any, kind: str) -> str:
    if value is None:
        return "—"
    if kind == "money":
        return f"${float(value):,.2f}"
    if kind == "percent":
        return f"{float(value) * 100:,.2f}%"
    if kind == "decimal":
        return f"{float(value):,.2f}"
    return f"{int(value):,}"


if __name__ == "__main__":
    main()
