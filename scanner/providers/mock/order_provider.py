"""
Mock order provider for testing without a live broker.

Entry is simulated as instantly filled at entry_price.
TP/SL exits are evaluated on every refresh() call using current_price
passed by the executor (from the live quote source).
"""
import logging
from datetime import datetime

from ..order_base import BracketRef, OrderProvider

log = logging.getLogger(__name__)


class MockOrderProvider(OrderProvider):

    def __init__(self) -> None:
        self._connected = False

    def connect(self) -> None:
        self._connected = True
        log.info("MockOrderProvider connected")

    def disconnect(self) -> None:
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    def place_bracket(
        self,
        symbol:      str,
        action:      str,
        quantity:    float,
        entry_type:  str,
        entry_price: float,
        sl:          float,
        tp:          float,
    ) -> BracketRef:
        ref = BracketRef(
            symbol=symbol, action=action, quantity=quantity,
            entry_type=entry_type, entry_price=entry_price,
            sl=sl, tp=tp,
            fill_price=entry_price,  # instant fill at requested price
            status="open",
            opened_at=datetime.now(),
        )
        log.info("Mock bracket | %s %s | entry=%.4f sl=%.4f tp=%.4f",
                 action, symbol, entry_price, sl, tp)
        return ref

    def refresh(self, ref: BracketRef, current_price: float = 0.0) -> BracketRef:
        if ref.status != "open" or current_price <= 0:
            return ref

        if ref.action == "BUY":
            if current_price >= ref.tp:
                ref.status     = "tp"
                ref.exit_price = ref.tp
            elif current_price <= ref.sl:
                ref.status     = "sl"
                ref.exit_price = ref.sl
        else:  # SELL (short)
            if current_price <= ref.tp:
                ref.status     = "tp"
                ref.exit_price = ref.tp
            elif current_price >= ref.sl:
                ref.status     = "sl"
                ref.exit_price = ref.sl

        return ref

    def cancel(self, ref: BracketRef) -> None:
        if ref.status == "open":
            ref.status = "cancelled"
            log.info("Mock bracket cancelled | %s", ref.symbol)
