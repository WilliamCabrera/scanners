"""
Interactive Brokers provider via ib_insync.

Requirements:
    pip install ib_insync
    TWS or IB Gateway must be running with API connections enabled.

Docs: https://ib-insync.readthedocs.io/
"""
from datetime import datetime
from ..base import DataProvider
from ...core.models import Quote


class IBKRProvider(DataProvider):
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 1,
    ) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._ib = None  # ib_insync.IB

    def connect(self) -> None:
        from ib_insync import IB  # type: ignore[import]
        self._ib = IB()
        self._ib.connect(self._host, self._port, clientId=self._client_id)

    def disconnect(self) -> None:
        if self._ib and self._ib.isConnected():
            self._ib.disconnect()
            self._ib = None

    def is_connected(self) -> bool:
        return self._ib is not None and self._ib.isConnected()

    def get_quote(self, symbol: str) -> Quote:
        return self.get_quotes([symbol])[0]

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        from ib_insync import Stock  # type: ignore[import]

        contracts = [Stock(s, "SMART", "USD") for s in symbols]
        tickers = self._ib.reqTickers(*contracts)
        self._ib.sleep(0.5)

        quotes: list[Quote] = []
        for ticker in tickers:
            quotes.append(Quote(
                symbol=ticker.contract.symbol,
                last=float(ticker.last or ticker.close or 0),
                bid=float(ticker.bid or 0),
                ask=float(ticker.ask or 0),
                volume=int(ticker.volume or 0),
                open=float(ticker.open or 0),
                high=float(ticker.high or 0),
                low=float(ticker.low or 0),
                prev_close=float(ticker.close or 0),
                timestamp=datetime.now(),
            ))
        return quotes
