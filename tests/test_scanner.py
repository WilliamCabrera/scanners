from scanner.core.filters import PriceFilter, VolumeFilter
from scanner.core.scanner import Scanner
from scanner.providers.mock.provider import MockProvider


def test_scan_once_returns_results():
    provider = MockProvider(seed=42)
    provider.connect()
    scanner = Scanner(
        provider=provider,
        symbols=["AAPL", "TSLA", "NVDA"],
        filters=[PriceFilter(1.0, 10_000.0), VolumeFilter(0)],
        interval=1.0,
    )
    results = scanner.scan_once()
    assert isinstance(results, list)
    assert len(results) > 0
    provider.disconnect()


def test_scan_filters_reduce_results():
    provider = MockProvider(seed=42)
    provider.connect()

    # Force volume ticks so volume > 0
    for _ in range(20):
        provider.get_quotes(["AAPL", "TSLA", "NVDA"])

    scanner_loose = Scanner(
        provider=provider,
        symbols=["AAPL", "TSLA", "NVDA"],
        filters=[PriceFilter(0.0, 10_000.0)],
        interval=1.0,
    )
    scanner_strict = Scanner(
        provider=provider,
        symbols=["AAPL", "TSLA", "NVDA"],
        filters=[PriceFilter(0.0, 10_000.0), VolumeFilter(999_999_999)],
        interval=1.0,
    )
    loose = scanner_loose.scan_once()
    strict = scanner_strict.scan_once()
    assert len(loose) >= len(strict)
    provider.disconnect()
