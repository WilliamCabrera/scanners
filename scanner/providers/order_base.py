from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class BracketRef:
    """
    Represents a placed bracket order (entry + TP + SL).
    Provider-specific state is stored in _internal — opaque to the executor.
    """
    symbol:       str
    action:       str          # "BUY" | "SELL"
    quantity:     float
    entry_type:   str          # "MKT" | "LMT"
    entry_price:  float        # requested entry (signal.entry_est)
    sl:           float
    tp:           float
    fill_price:   float = 0.0  # actual entry fill
    exit_price:   float = 0.0  # actual exit fill
    status:       str  = "pending"  # pending | open | tp | sl | cancelled
    opened_at:    datetime = field(default_factory=datetime.now)
    _internal:    object   = field(default=None, repr=False, compare=False)


class OrderProvider(ABC):
    """
    Abstract interface for order execution.
    Decoupled from DataProvider — any combination of data/order brokers is valid.
    """

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def is_connected(self) -> bool: ...

    @abstractmethod
    def place_bracket(
        self,
        symbol:       str,
        action:       str,    # "BUY" | "SELL"
        quantity:     float,
        entry_type:   str,    # "MKT" | "LMT"
        entry_price:  float,
        sl:           float,
        tp:           float,
    ) -> BracketRef:
        """
        Place an entry order with attached TP (limit) and SL (stop).
        Returns a BracketRef whose status starts as "pending".
        """

    @abstractmethod
    def refresh(self, ref: BracketRef, current_price: float = 0.0) -> BracketRef:
        """
        Update ref.status / fill_price / exit_price from current broker state.
        current_price is used by providers that simulate exits (e.g. Mock).
        Always returns the same ref object (mutated in-place).
        """

    @abstractmethod
    def cancel(self, ref: BracketRef) -> None:
        """
        Cancel all open orders in the bracket and close any open position at market.
        No-op if already closed.
        """

    def __enter__(self) -> "OrderProvider":
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.disconnect()
