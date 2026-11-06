#!/usr/bin/env bash
# Sets up Ben's Butt Spreader on a fresh Debian or Ubuntu server and keeps it running 24/7.
#
# Upload this file and your .env (and spamguard.db, to keep the punishment history) to the server's home
# folder, then run:  bash setup.sh
# Running it again later updates the bot to the newest code and restarts it.
set -euo pipefail

REPO="shxbainu/spamguard"
APP_DIR="$HOME/spamguard"
SERVICE="spreader"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "Installing git, the GitHub CLI and Python..."
sudo apt-get update -qq
sudo apt-get install -y -qq git gh python3-venv >/dev/null

if [ ! -d "$APP_DIR/.git" ]; then
  if ! gh auth status >/dev/null 2>&1; then
    say "The repo is private, so log in to GitHub: open the link it shows and enter the code."
    gh auth login --hostname github.com --git-protocol https --web
  fi
  gh auth setup-git  # lets plain "git pull" use the same login for updates
  say "Downloading the bot..."
  gh repo clone "$REPO" "$APP_DIR"
fi

cd "$APP_DIR"
git pull --ff-only -q

# Move uploaded files into place (an existing .env on the server is never overwritten).
for file in .env spamguard.db; do
  if [ -f "$HOME/$file" ] && [ ! -f "$file" ]; then
    mv "$HOME/$file" "$file"
    echo "Moved $file into $APP_DIR"
  fi
done
if [ ! -f .env ]; then
  say "No .env found. Upload your .env to the home folder (next to setup.sh), then run: bash setup.sh"
  exit 1
fi
chmod 600 .env

say "Installing Python packages..."
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

say "Starting the bot (it restarts on crashes and on reboot)..."
sudo tee "/etc/systemd/system/$SERVICE.service" >/dev/null <<EOF
[Unit]
Description=Ben's Butt Spreader Discord bot
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable -q "$SERVICE"
sudo systemctl restart "$SERVICE"

sleep 10
if systemctl is-active -q "$SERVICE"; then
  say "The bot is running. Startup log:"
  journalctl -u "$SERVICE" -n 40 --no-pager -o cat | grep -E "Logged in|AI:|Personality|default personality|could not parse|Error" || true
  say "Done. You can close this window; the bot keeps running."
  echo "Logs:    journalctl -u $SERVICE -f"
  echo "Update:  bash ~/spamguard/deploy/setup.sh   (pulls the newest code and restarts)"
else
  say "The bot didn't start. Last log lines:"
  journalctl -u "$SERVICE" -n 30 --no-pager -o cat
  exit 1
fi
