from .base import DataProvider
from ..config.settings import Settings


def get_provider(name: str, settings: Settings, asset_class: str = "stocks") -> DataProvider:
    """Factory — resolves provider name to a concrete DataProvider instance."""
    match name:
        case "mock":
            from .mock.provider import MockProvider
            return MockProvider()
        case "moomoo":
            from .moomoo.provider import MoomooProvider
            return MoomooProvider(host=settings.moomoo_host, port=settings.moomoo_port)
        case "ibkr":
            from .ibkr.provider import IBKRProvider
            syms = [s for s in settings.symbols_for(asset_class) if s != "*"]
            return IBKRProvider(
                host=settings.ibkr_host,
                port=settings.ibkr_port,
                client_id=settings.ibkr_client_id,
                symbols=syms,
            )
        case "webull":
            from .webull.provider import WebullProvider
            return WebullProvider(
                email=settings.webull_email,
                password=settings.webull_password,
            )
        case "massive":
            from .massive.provider import MassiveProvider
            return MassiveProvider(
                api_key=settings.massive_api_key,
                asset_class=asset_class,
                symbols=settings.symbols_for(asset_class),
                feed=settings.massive_feed,
                timeframe=settings.massive_timeframe,
            )
        case _:
            raise ValueError(
                f"Unknown provider: {name!r}. Choose: mock | moomoo | ibkr | webull | massive"
            )


__all__ = ["DataProvider", "get_provider"]
