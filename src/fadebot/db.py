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
    signal_id TEXT NOT NULL REFERENCES signals(signal_id),
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
    platform TEXT NOT NULL DEFAULT 'international',
    status TEXT NOT NULL DEFAULT 'open',
    resolved_outcome TEXT,
    final_price REAL,
    payout REAL,
    pnl REAL,
    roi REAL,
    settled_at TEXT,
    UNIQUE(signal_id, execution_mode)
);

CREATE TABLE IF NOT EXISTS market_mappings (
    source_market_slug TEXT PRIMARY KEY,
    target_market_slug TEXT NOT NULL,
    source_yes_target_outcome TEXT NOT NULL,
    mapping_method TEXT NOT NULL,
    source_title TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_market_sides (
    market_slug TEXT PRIMARY KEY,
    outcome TEXT NOT NULL CHECK (outcome IN ('YES', 'NO')),
    first_signal_id TEXT NOT NULL,
    claimed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_attempts (
    signal_id TEXT PRIMARY KEY REFERENCES signals(signal_id),
    status TEXT NOT NULL,
    reason TEXT,
    recorded_at TEXT NOT NULL
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
            await self._ensure_dual_trade_constraint(db)
            await db.commit()

    async def _ensure_dual_trade_constraint(self, db: aiosqlite.Connection) -> None:
        row = await (await db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='trades'"
        )).fetchone()
        if row is None or "UNIQUE(signal_id, execution_mode)" in str(row[0]):
            return
        # Rebuild only the trades table; preserve IDs, settlement data and all
        # legacy columns while changing the old signal_id-only uniqueness rule.
        await db.execute("BEGIN IMMEDIATE")
        try:
            ddl = SCHEMA.split("CREATE TABLE IF NOT EXISTS trades (", 1)[1].split(");", 1)[0]
            await db.execute("CREATE TABLE trades_dual_migration (" + ddl + ")")
            columns = [str(r[1]) for r in await (await db.execute("PRAGMA table_info(trades)")).fetchall()]
            names = ", ".join(columns)
            await db.execute(
                f"INSERT INTO trades_dual_migration ({names}) SELECT {names} FROM trades"
            )
            await db.execute("DROP TABLE trades")
            await db.execute("ALTER TABLE trades_dual_migration RENAME TO trades")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_trades_filled ON trades(filled_at DESC)")
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    async def _ensure_trade_columns(self, db: aiosqlite.Connection) -> None:
        rows = await (await db.execute("PRAGMA table_info(trades)")).fetchall()
        existing = {str(row[1]) for row in rows}
        additions = {
            "execution_mode": "TEXT NOT NULL DEFAULT 'paper'",
            "max_price": "REAL",
            "external_order_id": "TEXT",
            "external_status": "TEXT",
            "platform": "TEXT NOT NULL DEFAULT 'international'",
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

    async def record_live_attempt(self, signal_id: str, status: str, reason: str | None = None) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT INTO live_attempts (signal_id, status, reason, recorded_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(signal_id) DO UPDATE SET
                     status=excluded.status, reason=excluded.reason,
                     recorded_at=excluded.recorded_at""",
                (signal_id, status, reason, datetime.now(timezone.utc).isoformat()),
            )
            await db.commit()

    async def rejected_signal_messages(self, reason: str) -> list[dict[str, Any]]:
        rows = await self._fetchall(
            """
            SELECT raw_json
            FROM signals
            WHERE status = 'rejected' AND rejection_reason = ?
            ORDER BY received_at
            """,
            (reason,),
        )
        messages: list[dict[str, Any]] = []
        for row in rows:
            try:
                message = json.loads(str(row["raw_json"]))
            except (TypeError, ValueError):
                continue
            if isinstance(message, dict):
                messages.append(message)
        return messages

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
        platform: str = "international",
        outcome: str | None = None,
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
                    execution_mode, max_price, external_order_id, external_status,
                    platform
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    outcome or signal.paper_outcome,
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
                    platform,
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

    async def get_market_mapping(self, source_slug: str) -> dict[str, Any] | None:
        rows = await self._fetchall(
            "SELECT * FROM market_mappings WHERE source_market_slug = ?",
            (source_slug,),
        )
        return rows[0] if rows else None

    async def save_market_mapping(
        self,
        *,
        source_slug: str,
        target_slug: str,
        source_yes_target_outcome: str,
        method: str,
        source_title: str,
    ) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                INSERT INTO market_mappings (
                    source_market_slug, target_market_slug,
                    source_yes_target_outcome, mapping_method,
                    source_title, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_market_slug) DO UPDATE SET
                    target_market_slug=excluded.target_market_slug,
                    source_yes_target_outcome=excluded.source_yes_target_outcome,
                    mapping_method=excluded.mapping_method,
                    source_title=excluded.source_title,
                    created_at=excluded.created_at
                """,
                (
                    source_slug,
                    target_slug,
                    source_yes_target_outcome,
                    method,
                    source_title,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            await db.commit()

    async def claim_live_market_side(
        self, market_slug: str, outcome: str, signal_id: str
    ) -> bool:
        """Atomically prevent live orders opposing an earlier live attempt."""
        selected = outcome.upper()
        if selected not in {"YES", "NO"}:
            raise ValueError("Live market side must be YES or NO")
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            prior = await (
                await db.execute(
                    "SELECT outcome FROM live_market_sides WHERE market_slug = ?",
                    (market_slug,),
                )
            ).fetchone()
            if prior is not None:
                await db.commit()
                return str(prior[0]) == selected

            # Existing VPS databases may already contain live trades from older
            # versions, before the side-lock table was introduced.
            historic_opposite = await (
                await db.execute(
                    """
                    SELECT 1 FROM trades
                    WHERE market_slug = ? AND execution_mode = 'live'
                      AND UPPER(outcome) <> ?
                    LIMIT 1
                    """,
                    (market_slug, selected),
                )
            ).fetchone()
            if historic_opposite is not None:
                await db.commit()
                return False
            await db.execute(
                """
                INSERT INTO live_market_sides (
                    market_slug, outcome, first_signal_id, claimed_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    market_slug,
                    selected,
                    signal_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            await db.commit()
            return True

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

    async def summary(self, mode: str | None = None) -> dict[str, Any]:
        if mode not in (None, "paper", "live"):
            raise ValueError("mode must be paper, live or None")
        trade_where = "WHERE execution_mode = ?" if mode else ""
        trade_params = (mode,) if mode else ()
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
                    """ + trade_where,
                    trade_params,
                )
            ).fetchone()
            rejection_rows = await (
                await db.execute(
                    ("""
                    SELECT rejection_reason AS reason, COUNT(*) AS count
                    FROM signals WHERE status = 'rejected'
                    GROUP BY rejection_reason ORDER BY count DESC
                    """ if mode != "live" else """
                    SELECT reason, COUNT(*) AS count
                    FROM live_attempts WHERE status = 'rejected'
                    GROUP BY reason ORDER BY count DESC
                    """),
                )
            ).fetchall()

        result = dict(signal_row or {})
        result.update(dict(trade_row or {}))
        settled = int(result.get("settled_trades") or 0)
        wins = int(result.get("wins") or 0)
        settled_cost = await self._scalar(
            "SELECT COALESCE(SUM(cost_basis), 0) FROM trades WHERE status='settled'"
            + (" AND execution_mode = ?" if mode else ""),
            trade_params,
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

    async def recent_trades(self, limit: int = 100, mode: str | None = None) -> list[dict[str, Any]]:
        if mode not in (None, "paper", "live"):
            raise ValueError("mode must be paper, live or None")
        safe_limit = min(max(int(limit), 1), 500)
        return await self._fetchall(
            "SELECT * FROM trades "
            + ("WHERE execution_mode = ? " if mode else "")
            + "ORDER BY filled_at DESC LIMIT ?",
            (mode, safe_limit) if mode else (safe_limit,),
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
