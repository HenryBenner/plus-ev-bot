from datetime import datetime, timezone

import pytest

from fadebot.models import FadeSignal


def sample_message(**overrides):
    message = {
        "channel": "fade_finder",
        "type": "fade_finder_update",
        "ts": 1712847605.5,
        "data": {
            "type": "fade_finder_update",
            "version": 1,
            "event_id": 12,
            "group_id": 101,
            "market_slug": "lakers-celtics",
            "snapshot": False,
            "title": "Lakers vs Celtics",
            "created_at": "2026-07-23T18:21:11+00:00",
            "data": {
                "marketSlug": "lakers-celtics",
                "profitable_wallet_count": 3,
                "time_delta_min": 6.3,
                "resolution_date": "2026-07-24",
                "profitable_wallet": {
                    "userAddr": "0xwinner",
                    "pnl_to_date": 180000,
                    "outcome": "YES",
                    "side": "BUY",
                    "price": 0.47,
                    "amount": 1800,
                },
                "losing_wallet": {
                    "userAddr": "0xloser",
                    "pnl_to_date": -75000,
                    "outcome": "NO",
                    "side": "BUY",
                    "price": 0.53,
                    "amount": 1000,
                },
            },
        },
    }
    message.update(overrides)
    return message


def test_parse_signal_and_stable_identity():
    received = datetime(2026, 7, 23, 18, 22, tzinfo=timezone.utc)
    first = FadeSignal.from_message(sample_message(), received)
    second = FadeSignal.from_message(sample_message(), received)
    assert first.signal_id == second.signal_id
    assert first.paper_outcome == "YES"
    assert first.group_id == 101
    assert first.signal_price == pytest.approx(0.47)


def test_sell_is_normalized_to_opposite_buy():
    message = sample_message()
    message["data"]["data"]["profitable_wallet"]["side"] = "SELL"
    signal = FadeSignal.from_message(message)
    assert signal.profitable_outcome == "YES"
    assert signal.paper_outcome == "NO"


def test_invalid_message_rejected():
    message = sample_message(channel="prices")
    with pytest.raises(ValueError, match="not a fade_finder"):
        FadeSignal.from_message(message)


def test_missing_created_at_uses_websocket_timestamp():
    message = sample_message()
    message["data"]["created_at"] = None
    signal = FadeSignal.from_message(
        message, datetime(2030, 1, 1, tzinfo=timezone.utc)
    )
    assert signal.created_at == datetime.fromtimestamp(
        message["ts"], tz=timezone.utc
    )


def test_received_time_fallback_does_not_change_signal_identity():
    message = sample_message()
    message["data"]["created_at"] = None
    message["ts"] = None
    first = FadeSignal.from_message(
        message, datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    second = FadeSignal.from_message(
        message, datetime(2026, 1, 2, tzinfo=timezone.utc)
    )
    assert first.created_at != second.created_at
    assert first.signal_id == second.signal_id
