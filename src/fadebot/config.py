from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Load a small .env file without adding another runtime dependency."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("\"'")


@dataclass(frozen=True)
class Settings:
    prediction_hunt_api_key: str
    trading_mode: str = "paper"
    paper_stake_usd: float = 10.0
    max_event_hours: int = 72
    max_price_drift: float = 0.10
    database_path: Path = Path("data/fade_finder.db")
    settlement_poll_seconds: int = 900
    api_read_timeout_seconds: int = 45
    websocket_open_timeout_seconds: int = 60
    prediction_hunt_ws_url: str = "wss://ws.predictionhunt.com"
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"
    polymarket_clob_url: str = "https://clob.polymarket.com"
    live_trading_enabled: bool = False
    live_trading_ack: str = ""
    polymarket_private_key: str = ""
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""
    polymarket_funder_address: str = ""
    polymarket_signature_type: int = 3
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv()
        api_key = os.getenv("PREDICTION_HUNT_API_KEY", "").strip()
        if not api_key:
            raise ValueError(
                "PREDICTION_HUNT_API_KEY is required. Copy .env.example to .env "
                "and add a Dev, Pro, or Enterprise key."
            )
        stake = float(os.getenv("PAPER_STAKE_USD", "10"))
        if stake <= 0:
            raise ValueError("PAPER_STAKE_USD must be greater than zero")
        trading_mode = os.getenv("TRADING_MODE", "paper").strip().casefold()
        if trading_mode not in {"paper", "live"}:
            raise ValueError("TRADING_MODE must be either paper or live")
        drift = float(os.getenv("MAX_PRICE_DRIFT", "0.10"))
        if not 0 <= drift <= 1:
            raise ValueError("MAX_PRICE_DRIFT must be between zero and one")
        settings = cls(
            prediction_hunt_api_key=api_key,
            trading_mode=trading_mode,
            paper_stake_usd=stake,
            max_event_hours=int(os.getenv("MAX_EVENT_HOURS", "72")),
            max_price_drift=drift,
            database_path=Path(os.getenv("DATABASE_PATH", "data/fade_finder.db")),
            settlement_poll_seconds=int(os.getenv("SETTLEMENT_POLL_SECONDS", "900")),
            api_read_timeout_seconds=int(
                os.getenv("API_READ_TIMEOUT_SECONDS", "45")
            ),
            websocket_open_timeout_seconds=int(
                os.getenv("WEBSOCKET_OPEN_TIMEOUT_SECONDS", "60")
            ),
            prediction_hunt_ws_url=os.getenv(
                "PREDICTION_HUNT_WS_URL", "wss://ws.predictionhunt.com"
            ),
            polymarket_gamma_url=os.getenv(
                "POLYMARKET_GAMMA_URL", "https://gamma-api.polymarket.com"
            ).rstrip("/"),
            polymarket_clob_url=os.getenv(
                "POLYMARKET_CLOB_URL", "https://clob.polymarket.com"
            ).rstrip("/"),
            live_trading_enabled=_bool_env("LIVE_TRADING_ENABLED", False),
            live_trading_ack=os.getenv("LIVE_TRADING_ACK", "").strip(),
            polymarket_private_key=os.getenv("POLYMARKET_PRIVATE_KEY", "").strip(),
            polymarket_api_key=os.getenv("POLYMARKET_API_KEY", "").strip(),
            polymarket_api_secret=os.getenv("POLYMARKET_API_SECRET", "").strip(),
            polymarket_api_passphrase=os.getenv(
                "POLYMARKET_API_PASSPHRASE", ""
            ).strip(),
            polymarket_funder_address=os.getenv(
                "POLYMARKET_FUNDER_ADDRESS", ""
            ).strip(),
            polymarket_signature_type=int(
                os.getenv("POLYMARKET_SIGNATURE_TYPE", "3")
            ),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
        settings.validate_live_mode()
        return settings

    def validate_live_mode(self) -> None:
        if self.trading_mode != "live":
            return
        if not self.live_trading_enabled:
            raise ValueError(
                "Live mode is blocked: set LIVE_TRADING_ENABLED=true explicitly"
            )
        if self.live_trading_ack != "I_UNDERSTAND_REAL_MONEY_IS_AT_RISK":
            raise ValueError(
                "Live mode is blocked: LIVE_TRADING_ACK must equal "
                "I_UNDERSTAND_REAL_MONEY_IS_AT_RISK"
            )
        credentials = {
            "POLYMARKET_PRIVATE_KEY": self.polymarket_private_key,
            "POLYMARKET_API_KEY": self.polymarket_api_key,
            "POLYMARKET_API_SECRET": self.polymarket_api_secret,
            "POLYMARKET_API_PASSPHRASE": self.polymarket_api_passphrase,
            "POLYMARKET_FUNDER_ADDRESS": self.polymarket_funder_address,
        }
        missing = [name for name, value in credentials.items() if not value]
        if missing:
            raise ValueError(
                "Live mode is blocked: missing " + ", ".join(missing)
            )


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().casefold() in {"1", "true", "yes", "on"}
