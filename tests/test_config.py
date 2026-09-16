import pytest

from fadebot.config import Settings


def test_safe_live_filter_and_price_defaults(monkeypatch):
    monkeypatch.setenv("PREDICTION_HUNT_API_KEY", "test")
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.delenv("LIVE_CATEGORY_FILTERS", raising=False)
    monkeypatch.delenv("LIVE_MARKET_TYPE_FILTERS", raising=False)
    monkeypatch.delenv("LIVE_MIN_ENTRY_PRICE", raising=False)
    monkeypatch.delenv("LIVE_MAX_ENTRY_PRICE", raising=False)
    settings = Settings.from_env()
    assert settings.live_category_filters == ("sports",)
    assert settings.live_market_type_filters == ("team_winner",)
    assert settings.live_min_entry_price == pytest.approx(0.30)
    assert settings.live_max_entry_price == pytest.approx(0.90)


@pytest.mark.parametrize(
    ("minimum", "maximum"),
    [("0", "0.90"), ("0.90", "0.30"), ("0.30", "1"), ("0.50", "0.50")],
)
def test_invalid_live_price_band_is_rejected(monkeypatch, minimum, maximum):
    monkeypatch.setenv("PREDICTION_HUNT_API_KEY", "test")
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("LIVE_MIN_ENTRY_PRICE", minimum)
    monkeypatch.setenv("LIVE_MAX_ENTRY_PRICE", maximum)
    with pytest.raises(ValueError, match="LIVE_MIN_ENTRY_PRICE"):
        Settings.from_env()
