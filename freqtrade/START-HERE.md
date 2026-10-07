# altbot (Freqtrade) - start here

Fake money only. Starts with $1,000 (USDT). Trades ONDO, LINK, SUI, QNT, SAND.
Method: **BuyTheDip** - buys after a sharp drop, sells on the bounce, cuts losses at -8%.

## One-time setup
1. Install **Docker Desktop**: https://www.docker.com/products/docker-desktop/
2. Download this folder.
3. In the `freqtrade` folder, copy `.env.example` to a new file named `.env`, then change the password in it.

## Start the bot
Open a terminal in the `freqtrade` folder and type:
```
docker compose up -d
```
Then open **http://localhost:8080** in your browser and log in with the username and password from `.env`.

## Stop the bot
```
docker compose down
```

## Test the method on past prices (do this first!)
```
docker compose run --rm freqtrade download-data --config user_data/config.json --timeframe 15m --days 180
docker compose run --rm freqtrade backtesting --config user_data/config.json --strategy BuyTheDip
```
It prints how much the method would have made or lost over the last 6 months.

## Change coins
Edit the list under `pair_whitelist` in `user_data/config.json`, then restart:
```
docker compose restart
```
