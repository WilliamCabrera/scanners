from abc import ABC, abstractmethod
from ..core.models import Quote, Bar


class DataProvider(ABC):
    """
    Abstract interface for all market data sources.
    Implement this class to add a new provider (Moomoo, IBKR, Webull, …).
    """

    @abstractmethod
    def connect(self) -> None:
        """Establish connection to the data source."""

    @abstractmethod
    def disconnect(self) -> None:
        """Close the connection and release resources."""

    @abstractmethod
    def is_connected(self) -> bool:
        """Return True if the connection is currently active."""

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        """Return the latest Quote for a single symbol."""

    @abstractmethod
    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        """Return latest Quotes for multiple symbols (batch-friendly)."""

    # Optional — providers may override for efficiency
    def get_bars(self, symbol: str, limit: int = 100) -> list[Bar]:
        raise NotImplementedError(f"{type(self).__name__} does not support get_bars()")

    # Context-manager support — connect/disconnect automatically
    def __enter__(self) -> "DataProvider":
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.disconnect()
