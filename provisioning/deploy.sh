#!/bin/sh
# provisioning/deploy.sh
#
# Rsync this repo (including the cross-built linux-aarch64 binaries and
# offline apk bundle from build-bundle.sh) to a Pi, over a link that does
# NOT need to reach the internet -- only the dev Mac.
#
# Usage: provisioning/deploy.sh [user@host] [remote_dir]
# Defaults match minimal_pi's confirmed-working direct-cable setup
# (Pi 169.254.100.1, root login, no separate user configured yet).

set -e

REMOTE="${1:-root@169.254.100.1}"
REMOTE_DIR="${2:-/root/w7gvr_hf_skimmer}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ ! -x "$REPO_DIR/vendor/wsjtx/build-linux-aarch64/jt9" ]; then
  echo "ERROR: no linux-aarch64 build found -- run provisioning/build-bundle.sh first" >&2
  exit 1
fi

# Per-host runtime state is excluded, not just runtime output dirs: the
# Pi's own session counter, live socket, and config.json must never be
# overwritten by the dev Mac's copies (a stale Mac .hsd_log_counter would
# silently reset the Pi's session numbering; a Mac config.json would
# change the Pi's band/gain). Edit the Pi's config.json on the Pi.
rsync -av \
  --exclude '.git' \
  --exclude 'chunks' \
  --exclude 'sessions' \
  --exclude 'logs' \
  --exclude 'var' \
  --exclude '/hsd.sock' \
  --exclude '/.hsd_log_counter' \
  --exclude '/config/config.json' \
  --exclude '.DS_Store' \
  --exclude '__pycache__' \
  --exclude '/project_management/reference_data' \
  --exclude 'vendor/wsjtx/build-darwin-*' \
  --exclude 'vendor/ft8_lib/build-darwin-*' \
  "$REPO_DIR/" "$REMOTE:$REMOTE_DIR/"

echo "==> Deployed to $REMOTE:$REMOTE_DIR"
echo "==> Next: ssh $REMOTE 'sh $REMOTE_DIR/provisioning/install.sh'"
