"""
Moomoo / Futu OpenD provider.

Requirements:
    pip install moomoo-openD
    Start the OpenD gateway desktop app before connecting.

Docs: https://openapi.moomoo.com/moomoo-api-doc/
"""
from datetime import datetime
from ..base import DataProvider
from ...core.models import Quote, Bar


class MoomooProvider(DataProvider):
    def __init__(self, host: str = "127.0.0.1", port: int = 11111) -> None:
        self._host = host
        self._port = port
        self._ctx = None  # moomoo.OpenQuoteContext

    def connect(self) -> None:
        import moomoo as ft  # type: ignore[import]
        self._ctx = ft.OpenQuoteContext(host=self._host, port=self._port)

    def disconnect(self) -> None:
        if self._ctx:
            self._ctx.close()
            self._ctx = None

    def is_connected(self) -> bool:
        return self._ctx is not None

    def get_quote(self, symbol: str) -> Quote:
        return self.get_quotes([symbol])[0]

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        import moomoo as ft  # type: ignore[import]

        # Moomoo uses "US.AAPL" format
        ft_symbols = [f"US.{s}" for s in symbols]
        ret, data = self._ctx.get_market_snapshot(ft_symbols)

        if ret != ft.RET_OK:
            raise RuntimeError(f"Moomoo snapshot error: {data}")

        quotes: list[Quote] = []
        for _, row in data.iterrows():
            sym = row["code"].replace("US.", "")
            quotes.append(Quote(
                symbol=sym,
                last=float(row["last_price"]),
                bid=float(row["bid_price"]),
                ask=float(row["ask_price"]),
                volume=int(row["volume"]),
                open=float(row["open_price"]),
                high=float(row["high_price"]),
                low=float(row["low_price"]),
                prev_close=float(row["prev_close_price"]),
                timestamp=datetime.now(),
            ))
        return quotes
