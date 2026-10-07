"""
Builds server/cloud-init.template.sh: a first-boot script for an Oracle Cloud
(Ubuntu) server that installs Docker + Tailscale and starts Freqtrade in paper mode.

Placeholders filled in by the setup page (in the user's browser, never stored):
  __TS_AUTHKEY__  Tailscale auth key        __UI_PASSWORD__  web screen password
  __JWT_SECRET__  random secret             __WS_TOKEN__     random secret

Run after changing config.json / the strategy:  python3 server/build_cloud_init.py
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FT = ROOT / "freqtrade"

COMPOSE = """services:
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
"""


def heredoc(path: str, body: str) -> str:
    assert "ALTBOT_EOF" not in body
    return f"cat > {path} <<'ALTBOT_EOF'\n{body.rstrip()}\nALTBOT_EOF\n"


def main() -> None:
    config = (FT / "user_data/config.json").read_text()
    strategy = (FT / "user_data/strategies/BuyTheDip.py").read_text()
    script = f"""#!/bin/bash
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
{heredoc("docker-compose.yml", COMPOSE)}
{heredoc("user_data/config.json", config)}
{heredoc("user_data/strategies/BuyTheDip.py", strategy)}
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
"""
    out = ROOT / "server/cloud-init.template.sh"
    out.write_text(script)
    print(f"wrote {out} ({len(script)} bytes)")


if __name__ == "__main__":
    main()
