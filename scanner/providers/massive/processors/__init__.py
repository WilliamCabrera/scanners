from .base import AssetProcessor
from .stocks import StocksProcessor
from .crypto import CryptoProcessor
from .forex import ForexProcessor
from .indices import IndicesProcessor

_REGISTRY: dict[str, type[AssetProcessor]] = {
    "stocks":  StocksProcessor,
    "crypto":  CryptoProcessor,
    "forex":   ForexProcessor,
    "indices": IndicesProcessor,
}


def get_processor(asset_class: str, **kwargs) -> AssetProcessor:
    cls = _REGISTRY.get(asset_class, StocksProcessor)
    return cls(**kwargs)


__all__ = ["AssetProcessor", "get_processor"]
