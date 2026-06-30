from dataclasses import dataclass


@dataclass(frozen=True)
class AssetClassConfig:
    market: str          # massive.websocket.models.Market attribute name
    seconds_prefix: str  # subscription prefix for per-second bars
    seconds_event: str   # event_type value on incoming messages (seconds)
    minutes_prefix: str  # subscription prefix for per-minute bars
    minutes_event: str   # event_type value on incoming messages (minutes)


CONFIGS: dict[str, AssetClassConfig] = {
    "stocks": AssetClassConfig(
        market="Stocks",
        seconds_prefix="A",   seconds_event="A",
        minutes_prefix="AM",  minutes_event="AM",
    ),
    "crypto": AssetClassConfig(
        market="Crypto",
        seconds_prefix="XAS", seconds_event="XAS",
        minutes_prefix="XAM", minutes_event="XAM",
    ),
    "forex": AssetClassConfig(
        market="Forex",
        seconds_prefix="CAS", seconds_event="CAS",
        minutes_prefix="CAM", minutes_event="CAM",
    ),
    "options": AssetClassConfig(
        market="Options",
        seconds_prefix="OAS", seconds_event="OAS",
        minutes_prefix="OAM", minutes_event="OAM",
    ),
    "futures": AssetClassConfig(
        market="Futures",
        seconds_prefix="FAS", seconds_event="FAS",
        minutes_prefix="FAM", minutes_event="FAM",
    ),
    "indices": AssetClassConfig(
        market="Indices",
        seconds_prefix="A",  seconds_event="A",
        minutes_prefix="AM", minutes_event="AM",
    ),
}

SUPPORTED: frozenset[str] = frozenset(CONFIGS)
