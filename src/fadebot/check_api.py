from __future__ import annotations

import argparse
import asyncio
import base64
import time

import httpx
from nacl.signing import SigningKey

from .config import Settings


async def check(settings: Settings, *, verify_tls: bool = True) -> int:
    async with httpx.AsyncClient(
        timeout=settings.api_read_timeout_seconds,
        verify=verify_tls,
        headers={"User-Agent": "prediction-hunt-fade-bot/0.1"},
    ) as http:
        markets = await http.get(
            f"{settings.polymarket_us_gateway_url}/v1/markets",
            params={"active": "true", "closed": "false", "limit": 1},
        )
        markets.raise_for_status()
        entries = markets.json().get("markets") or []
        if not entries:
            print("Public API: reachable, but no active market was returned")
            return 1
        slug = str(entries[0]["slug"])
        book = await http.get(
            f"{settings.polymarket_us_gateway_url}/v1/markets/{slug}/book"
        )
        book.raise_for_status()
        market_data = book.json().get("marketData") or {}
        levels = len(market_data.get("bids") or []) + len(
            market_data.get("offers") or []
        )
        print(f"Public API: OK ({slug}, {levels} book levels)")

        if not settings.polymarket_us_key_id or not settings.polymarket_us_secret_key:
            print("Authenticated API: not checked (credentials are missing)")
            return 1
        path = "/v1/portfolio/positions"
        timestamp = str(int(time.time() * 1000))
        secret = base64.b64decode(settings.polymarket_us_secret_key)
        signing_key = SigningKey(secret[:32])
        signature = base64.b64encode(
            signing_key.sign(f"{timestamp}GET{path}".encode()).signature
        ).decode()
        response = await http.get(
            f"{settings.polymarket_us_api_url}{path}",
            headers={
                "X-PM-Access-Key": settings.polymarket_us_key_id,
                "X-PM-Timestamp": timestamp,
                "X-PM-Signature": signature,
            },
        )
        if response.is_success:
            print("Authenticated API: OK")
            return 0
        print(f"Authenticated API: rejected credentials (HTTP {response.status_code})")
        return 2


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only connectivity check for Polymarket US"
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="disable TLS verification only for diagnosing a local CA-store problem",
    )
    args = parser.parse_args()
    settings = Settings.from_env()
    raise SystemExit(asyncio.run(check(settings, verify_tls=not args.insecure)))


if __name__ == "__main__":
    main()
