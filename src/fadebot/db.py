from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from .models import FadeSignal, MarketInfo, PaperFill


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL,
    source_ts REAL,
    created_at TEXT NOT NULL,
    event_id INTEGER,
    group_id INTEGER,
    market_slug TEXT NOT NULL,
    title TEXT NOT NULL,
    snapshot INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'received',
    rejection_reason TEXT,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL UNIQUE REFERENCES signals(signal_id),
    event_id INTEGER,
    group_id INTEGER,
    event_slug TEXT NOT NULL,
    market_slug TEXT NOT NULL,
    market_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    title TEXT NOT NULL,
    event_time TEXT,
    outcome TEXT NOT NULL,
    signal_price REAL NOT NULL,
    fill_avg_price REAL NOT NULL,
    shares REAL NOT NULL,
    notional REAL NOT NULL,
    entry_fee REAL NOT NULL,
    cost_basis REAL NOT NULL,
    fully_filled INTEGER NOT NULL,
    filled_at TEXT NOT NULL,
    execution_mode TEXT NOT NULL DEFAULT 'paper',
    max_price REAL,
    external_order_id TEXT,
    external_status TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    resolved_outcome TEXT,
    final_price REAL,
    payout REAL,
    pnl REAL,
    roi REAL,
    settled_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_filled ON trades(filled_at DESC);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            await self._ensure_trade_columns(db)
            await db.commit()

    async def _ensure_trade_columns(self, db: aiosqlite.Connection) -> None:
        rows = await (await db.execute("PRAGMA table_info(trades)")).fetchall()
        existing = {str(row[1]) for row in rows}
        additions = {
            "execution_mode": "TEXT NOT NULL DEFAULT 'paper'",
            "max_price": "REAL",
            "external_order_id": "TEXT",
            "external_status": "TEXT",
        }
        for name, definition in additions.items():
            if name not in existing:
                await db.execute(
                    f"ALTER TABLE trades ADD COLUMN {name} {definition}"
                )

    async def add_signal(self, signal: FadeSignal) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO signals (
                    signal_id, received_at, source_ts, created_at, event_id,
                    group_id, market_slug, title, snapshot, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.signal_id,
                    signal.received_at.isoformat(),
                    signal.source_ts,
                    signal.created_at.isoformat(),
                    signal.event_id,
                    signal.group_id,
                    signal.market_slug,
                    signal.title,
                    int(signal.snapshot),
                    json.dumps(signal.raw, separators=(",", ":")),
                ),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def reject_signal(self, signal_id: str, reason: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                UPDATE signals
                SET status = 'rejected', rejection_reason = ?
                WHERE signal_id = ?
                """,
                (reason, signal_id),
            )
            await db.commit()

    async def create_trade(
        self,
        signal: FadeSignal,
        market: MarketInfo,
        token_id: str,
        fill: PaperFill,
        filled_at: datetime,
        *,
        execution_mode: str = "paper",
        max_price: float | None = None,
        external_order_id: str | None = None,
        external_status: str | None = None,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN")
            await db.execute(
                """
                INSERT OR IGNORE INTO trades (
                    signal_id, event_id, group_id, event_slug, market_slug,
                    market_id, condition_id, token_id, title, event_time,
                    outcome, signal_price, fill_avg_price, shares, notional,
                    entry_fee, cost_basis, fully_filled, filled_at,
                    execution_mode, max_price, external_order_id, external_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.signal_id,
                    signal.event_id,
                    signal.group_id,
                    market.event_slug,
                    market.market_slug,
                    market.market_id,
                    market.condition_id,
                    token_id,
                    market.title or signal.title,
                    market.event_time.isoformat() if market.event_time else None,
                    signal.paper_outcome,
                    signal.signal_price,
                    fill.average_price,
                    fill.shares,
                    fill.notional,
                    fill.fee,
                    fill.total_cost,
                    int(fill.fully_filled),
                    filled_at.isoformat(),
                    execution_mode,
                    max_price,
                    external_order_id,
                    external_status,
                ),
            )
            await db.execute(
                """
                UPDATE signals
                SET status = 'traded', rejection_reason = NULL
                WHERE signal_id = ?
                """,
                (signal.signal_id,),
            )
            await db.commit()

    async def open_trades(self) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT * FROM trades WHERE status = 'open' ORDER BY filled_at"
        )

    async def settle_trade(
        self,
        trade_id: int,
        *,
        final_price: float,
        resolved_outcome: str,
        settled_at: datetime,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT shares, cost_basis FROM trades WHERE id = ? AND status = 'open'",
                (trade_id,),
            )
            row = await cursor.fetchone()
            if not row:
                return
            payout = float(row["shares"]) * final_price
            pnl = payout - float(row["cost_basis"])
            roi = pnl / float(row["cost_basis"])
            await db.execute(
                """
                UPDATE trades SET
                    status = 'settled', resolved_outcome = ?, final_price = ?,
                    payout = ?, pnl = ?, roi = ?, settled_at = ?
                WHERE id = ?
                """,
                (
                    resolved_outcome,
                    final_price,
                    payout,
                    pnl,
                    roi,
                    settled_at.isoformat(),
                    trade_id,
                ),
            )
            await db.commit()

    async def summary(self) -> dict[str, Any]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            signal_row = await (
                await db.execute(
                    """
                    SELECT
                        COUNT(*) AS total_signals,
                        SUM(status = 'traded') AS traded_signals,
                        SUM(status = 'rejected') AS rejected_signals
                    FROM signals
                    """
                )
            ).fetchone()
            trade_row = await (
                await db.execute(
                    """
                    SELECT
                        COUNT(*) AS total_trades,
                        SUM(status = 'open') AS open_trades,
                        SUM(status = 'settled') AS settled_trades,
                        COALESCE(SUM(cost_basis), 0) AS total_cost,
                        COALESCE(SUM(entry_fee), 0) AS total_fees,
                        COALESCE(SUM(CASE WHEN status='settled' THEN payout ELSE 0 END), 0) AS payout,
                        COALESCE(SUM(CASE WHEN status='settled' THEN pnl ELSE 0 END), 0) AS pnl,
                        SUM(CASE WHEN status='settled' AND pnl > 0 THEN 1 ELSE 0 END) AS wins,
                        SUM(CASE WHEN status='settled' AND pnl < 0 THEN 1 ELSE 0 END) AS losses,
                        AVG(CASE WHEN status='settled' THEN roi END) AS avg_trade_roi,
                        MAX(CASE WHEN status='settled' THEN roi END) AS best_roi,
                        MIN(CASE WHEN status='settled' THEN roi END) AS worst_roi,
                        COALESCE(SUM(CASE WHEN status='settled' AND pnl > 0 THEN pnl ELSE 0 END), 0) AS gross_profit,
                        ABS(COALESCE(SUM(CASE WHEN status='settled' AND pnl < 0 THEN pnl ELSE 0 END), 0)) AS gross_loss
                    FROM trades
                    """
                )
            ).fetchone()
            rejection_rows = await (
                await db.execute(
                    """
                    SELECT rejection_reason AS reason, COUNT(*) AS count
                    FROM signals WHERE status = 'rejected'
                    GROUP BY rejection_reason ORDER BY count DESC
                    """
                )
            ).fetchall()

        result = dict(signal_row or {})
        result.update(dict(trade_row or {}))
        settled = int(result.get("settled_trades") or 0)
        wins = int(result.get("wins") or 0)
        settled_cost = await self._scalar(
            "SELECT COALESCE(SUM(cost_basis), 0) FROM trades WHERE status='settled'"
        )
        pnl = float(result.get("pnl") or 0)
        gross_loss = float(result.get("gross_loss") or 0)
        result["win_rate"] = wins / settled if settled else None
        result["portfolio_roi"] = pnl / float(settled_cost) if settled_cost else None
        result["profit_factor"] = (
            float(result.get("gross_profit") or 0) / gross_loss
            if gross_loss
            else None
        )
        result["rejections"] = [dict(row) for row in rejection_rows]
        return result

    async def recent_trades(self, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = min(max(int(limit), 1), 500)
        return await self._fetchall(
            "SELECT * FROM trades ORDER BY filled_at DESC LIMIT ?", (safe_limit,)
        )

    async def equity_curve(self) -> list[dict[str, Any]]:
        rows = await self._fetchall(
            """
            SELECT id, settled_at, pnl
            FROM trades WHERE status='settled'
            ORDER BY settled_at, id
            """
        )
        cumulative = 0.0
        curve = []
        for row in rows:
            cumulative += float(row["pnl"])
            curve.append(
                {"settled_at": row["settled_at"], "cumulative_pnl": cumulative}
            )
        return curve

    async def recent_signals(self, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = min(max(int(limit), 1), 500)
        return await self._fetchall(
            """
            SELECT signal_id, received_at, created_at, market_slug, title,
                   snapshot, status, rejection_reason
            FROM signals ORDER BY received_at DESC LIMIT ?
            """,
            (safe_limit,),
        )

    async def _fetchall(
        self, sql: str, params: tuple[Any, ...] = ()
    ) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(sql, params)).fetchall()
            return [dict(row) for row in rows]

    async def _scalar(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute(sql, params)).fetchone()
            return row[0] if row else None
