from datetime import datetime
from scanner.core.models import Quote
from scanner.core.filters import (
    PriceFilter, VolumeFilter, ChangePctFilter,
    GapUpFilter, SpreadFilter, AndFilter, OrFilter, NotFilter,
)


def _quote(**kwargs) -> Quote:
    defaults = dict(
        symbol="TEST", last=10.0, bid=9.99, ask=10.01,
        volume=1_000_000, open=9.5, high=10.5, low=9.0,
        prev_close=9.0, timestamp=datetime.now(),
    )
    defaults.update(kwargs)
    return Quote(**defaults)


def test_price_filter_passes():
    assert PriceFilter(5.0, 20.0).matches(_quote(last=10.0))

def test_price_filter_fails_below():
    assert not PriceFilter(15.0, 20.0).matches(_quote(last=10.0))

def test_price_filter_fails_above():
    assert not PriceFilter(1.0, 9.0).matches(_quote(last=10.0))

def test_volume_filter():
    f = VolumeFilter(500_000)
    assert f.matches(_quote(volume=1_000_000))
    assert not f.matches(_quote(volume=100_000))

def test_change_pct_filter():
    q = _quote(last=10.0, prev_close=9.0)  # +11.1%
    assert ChangePctFilter(min_pct=10.0).matches(q)
    assert not ChangePctFilter(max_pct=5.0).matches(q)

def test_gap_up_filter():
    q = _quote(open=10.0, prev_close=9.0)  # gap = +11.1%
    assert GapUpFilter(10.0).matches(q)
    assert not GapUpFilter(15.0).matches(q)

def test_and_composition():
    f = PriceFilter(1.0, 20.0) & VolumeFilter(500_000)
    assert f.matches(_quote(last=10.0, volume=1_000_000))
    assert not f.matches(_quote(last=10.0, volume=100_000))

def test_or_composition():
    f = PriceFilter(1.0, 5.0) | PriceFilter(15.0, 20.0)
    assert not f.matches(_quote(last=10.0))

def test_not_composition():
    f = ~PriceFilter(1.0, 5.0)
    assert f.matches(_quote(last=10.0))
    assert not f.matches(_quote(last=3.0))
