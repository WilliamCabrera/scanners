"""
Webull provider via the unofficial webull Python SDK.

Requirements:
    pip install webull
    Webull account credentials required.

Docs: https://github.com/tedchou12/webull
"""
from datetime import datetime
from ..base import DataProvider
from ...core.models import Quote


class WebullProvider(DataProvider):
    def __init__(self, email: str, password: str) -> None:
        self._email = email
        self._password = password
        self._wb = None  # webull.webull

    def connect(self) -> None:
        from webull import webull  # type: ignore[import]
        self._wb = webull()
        self._wb.login(self._email, self._password)

    def disconnect(self) -> None:
        if self._wb:
            self._wb.logout()
            self._wb = None

    def is_connected(self) -> bool:
        return self._wb is not None

    def get_quote(self, symbol: str) -> Quote:
        return self.get_quotes([symbol])[0]

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        quotes: list[Quote] = []
        for sym in symbols:
            data = self._wb.get_quote(sym)
            quotes.append(Quote(
                symbol=sym,
                last=float(data.get("close", 0)),
                bid=float(data.get("bidList", [{}])[0].get("price", 0)),
                ask=float(data.get("askList", [{}])[0].get("price", 0)),
                volume=int(data.get("volume", 0)),
                open=float(data.get("open", 0)),
                high=float(data.get("high", 0)),
                low=float(data.get("low", 0)),
                prev_close=float(data.get("preClose", 0)),
                timestamp=datetime.now(),
            ))
        return quotes
