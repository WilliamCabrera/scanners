import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()

_DEFAULT_SYMBOLS: dict[str, list[str]] = {
    "stocks":  ['*'],
    "crypto":  ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD"],
    "forex":   ["EUR-USD", "GBP-USD", "USD-JPY", "AUD-USD"],
    "options": [],
    "futures": [],
    "indices": ["I:SPX", "I:DJI", "I:NDX", "I:VIX"],
}


def _parse_list(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


@dataclass
class Settings:
    provider: str = "mock"
    scan_interval: float = 5.0
    log_level: str = "INFO"

    moomoo_host: str = "127.0.0.1"
    moomoo_port: int = 11111

    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7497
    ibkr_client_id: int = 1

    webull_email: str = ""
    webull_password: str = ""

    massive_api_key: str = ""
    massive_feed: str = "delayed"
    massive_timeframe: str = "seconds"
    massive_asset_classes: list[str] = field(default_factory=lambda: ["stocks"])

    # Per-asset-class symbol lists
    symbols_stocks:  list[str] = field(default_factory=lambda: list(_DEFAULT_SYMBOLS["stocks"]))
    symbols_crypto:  list[str] = field(default_factory=lambda: list(_DEFAULT_SYMBOLS["crypto"]))
    symbols_forex:   list[str] = field(default_factory=lambda: list(_DEFAULT_SYMBOLS["forex"]))
    symbols_options: list[str] = field(default_factory=list)
    symbols_futures: list[str] = field(default_factory=list)
    symbols_indices: list[str] = field(default_factory=lambda: list(_DEFAULT_SYMBOLS["indices"]))

    def symbols_for(self, asset_class: str) -> list[str]:
        """Return the symbol list for the given asset class."""
        syms = getattr(self, f"symbols_{asset_class}", None)
        return syms if syms else list(_DEFAULT_SYMBOLS.get(asset_class, []))

    @classmethod
    def from_env(cls) -> "Settings":
        def _syms(key: str, asset_class: str) -> list[str]:
            raw = os.getenv(key, "")
            return _parse_list(raw) if raw else list(_DEFAULT_SYMBOLS.get(asset_class, []))

        raw_ac = os.getenv("MASSIVE_ASSET_CLASSES", "stocks")
        asset_classes = _parse_list(raw_ac) or ["stocks"]

        return cls(
            provider=os.getenv("SCANNER_PROVIDER", "mock"),
            scan_interval=float(os.getenv("SCANNER_INTERVAL", "5")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            moomoo_host=os.getenv("MOOMOO_HOST", "127.0.0.1"),
            moomoo_port=int(os.getenv("MOOMOO_PORT", "11111")),
            ibkr_host=os.getenv("IBKR_HOST", "127.0.0.1"),
            ibkr_port=int(os.getenv("IBKR_PORT", "7497")),
            ibkr_client_id=int(os.getenv("IBKR_CLIENT_ID", "1")),
            webull_email=os.getenv("WEBULL_EMAIL", ""),
            webull_password=os.getenv("WEBULL_PASSWORD", ""),
            massive_api_key=os.getenv("MASSIVE_API_KEY", ""),
            massive_feed=os.getenv("MASSIVE_FEED", "delayed"),
            massive_timeframe=os.getenv("MASSIVE_TIMEFRAME", "seconds"),
            massive_asset_classes=asset_classes,
            symbols_stocks=_syms("SCANNER_SYMBOLS_STOCKS", "stocks"),
            symbols_crypto=_syms("SCANNER_SYMBOLS_CRYPTO", "crypto"),
            symbols_forex=_syms("SCANNER_SYMBOLS_FOREX", "forex"),
            symbols_options=_syms("SCANNER_SYMBOLS_OPTIONS", "options"),
            symbols_futures=_syms("SCANNER_SYMBOLS_FUTURES", "futures"),
            symbols_indices=_syms("SCANNER_SYMBOLS_INDICES", "indices"),
        )
