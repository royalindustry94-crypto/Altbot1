#!/bin/bash
# altbot first-boot setup (paper trading only). Log: /var/log/altbot-setup.log
exec > /var/log/altbot-setup.log 2>&1
set -euxo pipefail

# 1. Swap (helps on small 1 GB servers)
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# 2. Docker
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker

# 3. Tailscale (private link between this server and your phone)
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up --authkey='__TS_AUTHKEY__' --hostname=altbot
iptables -I INPUT 1 -i tailscale0 -j ACCEPT
netfilter-persistent save || true

# 4. Bot files
APP=/opt/altbot
mkdir -p $APP/user_data/strategies $APP/user_data/logs $APP/user_data/data
cd $APP
cat > docker-compose.yml <<'ALTBOT_EOF'
services:
  freqtrade:
    image: freqtradeorg/freqtrade:stable
    container_name: altbot
    restart: unless-stopped
    volumes:
      - "./user_data:/freqtrade/user_data"
    ports:
      - "8080:8080"
    env_file: .env
    command: >
      trade
      --logfile /freqtrade/user_data/logs/freqtrade.log
      --db-url sqlite:////freqtrade/user_data/tradesv3.dryrun.sqlite
      --config /freqtrade/user_data/config.json
      --strategy BuyTheDip
ALTBOT_EOF

cat > user_data/config.json <<'ALTBOT_EOF'
{
  "$schema": "https://schema.freqtrade.io/schema.json",
  "bot_name": "altbot",
  "dry_run": true,
  "dry_run_wallet": 1000,
  "trading_mode": "spot",
  "margin_mode": "",
  "stake_currency": "USDT",
  "stake_amount": 150,
  "max_open_trades": 5,
  "tradable_balance_ratio": 0.8,
  "fiat_display_currency": "AUD",
  "cancel_open_orders_on_exit": false,
  "initial_state": "running",
  "force_entry_enable": false,
  "unfilledtimeout": {
    "entry": 10,
    "exit": 10,
    "exit_timeout_count": 0,
    "unit": "minutes"
  },
  "entry_pricing": {
    "price_side": "same",
    "use_order_book": true,
    "order_book_top": 1,
    "price_last_balance": 0.0,
    "check_depth_of_market": {
      "enabled": false,
      "bids_to_ask_delta": 1
    }
  },
  "exit_pricing": {
    "price_side": "same",
    "use_order_book": true,
    "order_book_top": 1
  },
  "exchange": {
    "name": "binance",
    "key": "",
    "secret": "",
    "ccxt_config": {},
    "ccxt_async_config": {},
    "pair_whitelist": [
      "ONDO/USDT",
      "LINK/USDT",
      "SUI/USDT",
      "QNT/USDT",
      "SAND/USDT"
    ],
    "pair_blacklist": []
  },
  "pairlists": [
    { "method": "StaticPairList" }
  ],
  "telegram": {
    "enabled": false,
    "token": "",
    "chat_id": ""
  },
  "api_server": {
    "enabled": true,
    "listen_ip_address": "0.0.0.0",
    "listen_port": 8080,
    "verbosity": "error",
    "enable_openapi": false,
    "CORS_origins": []
  },
  "internals": {
    "process_throttle_secs": 5
  }
}
ALTBOT_EOF

cat > user_data/strategies/BuyTheDip.py <<'ALTBOT_EOF'
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
ALTBOT_EOF

cat > .env <<'ALTBOT_EOF'
FREQTRADE__API_SERVER__USERNAME=trader
FREQTRADE__API_SERVER__PASSWORD=__UI_PASSWORD__
FREQTRADE__API_SERVER__JWT_SECRET_KEY=__JWT_SECRET__
FREQTRADE__API_SERVER__WS_TOKEN=__WS_TOKEN__
ALTBOT_EOF
chmod 600 .env
chown -R 1000:1000 user_data

# 5. Start (restarts automatically after reboots and crashes)
docker compose up -d
echo "ALTBOT SETUP COMPLETE"
