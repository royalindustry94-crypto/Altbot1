"""
BuyTheDip — starter strategy (method 1: buy the dip, sell the bounce).

Buy when ALL are true on a closed 15m candle:
  * RSI(14) is below 30            -> price has been falling hard ("oversold")
  * close is below the lower Bollinger Band -> price is unusually low vs. its recent average
  * volume is above zero           -> real trading happened

Sell when ANY is true:
  * RSI(14) climbs above 60        -> the bounce has happened
  * profit target hit (minimal_roi table below)
  * loss reaches -8% (stoploss)    -> cut the loser, protect the account

This is a starting point for paper trading, not a proven money-maker.
Backtest it first:  freqtrade backtesting --strategy BuyTheDip --timerange 20260101-
"""

from pandas import DataFrame

import talib.abstract as ta
from freqtrade.strategy import IStrategy, IntParameter
from technical import qtpylib


class BuyTheDip(IStrategy):
    INTERFACE_VERSION = 3

    timeframe = "15m"
    can_short = False

    # Take profit: +6% any time, +3% after 2h, +1% after 6h, break-even after 12h.
    minimal_roi = {
        "0": 0.06,
        "120": 0.03,
        "360": 0.01,
        "720": 0.0,
    }
    stoploss = -0.08

    process_only_new_candles = True
    use_exit_signal = True
    exit_profit_only = False
    startup_candle_count = 30

    # Tunable later with hyperopt.
    buy_rsi = IntParameter(20, 40, default=30, space="buy")
    sell_rsi = IntParameter(50, 75, default=60, space="sell")

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        bb = qtpylib.bollinger_bands(qtpylib.typical_price(dataframe), window=20, stds=2)
        dataframe["bb_lower"] = bb["lower"]
        dataframe["bb_mid"] = bb["mid"]
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["rsi"] < self.buy_rsi.value)
            & (dataframe["close"] < dataframe["bb_lower"])
            & (dataframe["volume"] > 0),
            ["enter_long", "enter_tag"],
        ] = (1, "rsi_bb_dip")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (dataframe["rsi"] > self.sell_rsi.value) & (dataframe["volume"] > 0),
            ["exit_long", "exit_tag"],
        ] = (1, "rsi_recovered")
        return dataframe
