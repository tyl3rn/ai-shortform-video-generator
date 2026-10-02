#!/usr/bin/env bash
# pi/setup.sh -- set up showrunner on a Raspberry Pi (Raspberry Pi OS 64-bit).
#
# Safe to re-run: it updates the repo and dependencies, and keeps .env,
# backgrounds/, and your rclone config.
#
#   curl -fsSL https://raw.githubusercontent.com/tyl3rn/ai-shortform-video-generator/main/pi/setup.sh | bash
#
# Afterwards (once): copy .env and backgrounds/parkour.mp4 over, run
# `rclone config` to connect OneDrive (see README "Running on a Raspberry Pi").
set -euo pipefail

REPO_URL="https://github.com/tyl3rn/ai-shortform-video-generator.git"
DIR="$HOME/showrunner"

echo "== system packages"
sudo apt-get update -qq
sudo apt-get install -y -qq git python3-venv python3-pip ffmpeg fonts-dejavu-core curl unzip

if ! command -v rclone >/dev/null; then
  echo "== rclone"
  # Debian's packaged rclone lags far behind; the official installer is current.
  curl -fsSL https://rclone.org/install.sh | sudo bash
fi

echo "== code"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone "$REPO_URL" "$DIR"
fi
mkdir -p "$DIR/backgrounds" "$DIR/run_output" "$DIR/demo"

echo "== python deps"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

echo "== daily cron job (6:00 local time)"
CRON_LINE="0 6 * * * cd $DIR && .venv/bin/python daily.py >/dev/null 2>&1"
# `crontab -l` and `grep -v` both exit 1 on a fresh Pi (no crontab yet,
# nothing to keep), which would trip `set -e`.
{ crontab -l 2>/dev/null | grep -v 'showrunner.*daily.py' || true; echo "$CRON_LINE"; } | crontab -

echo
echo "Done. Timezone is $(timedatectl show -p Timezone --value) -- the job runs at 6:00 in it."
[ -f "$DIR/.env" ] || echo "Still needed: $DIR/.env (ANTHROPIC_API_KEY, SHOWRUNNER_RCLONE_DEST)"
[ -f "$DIR/backgrounds/parkour.mp4" ] || echo "Still needed: $DIR/backgrounds/parkour.mp4"
rclone listremotes 2>/dev/null | grep -q . || echo "Still needed: rclone config (connect OneDrive)"
