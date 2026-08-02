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

rsync -av \
  --exclude '.git' \
  --exclude 'chunks' \
  --exclude 'sessions' \
  --exclude 'logs' \
  --exclude 'var' \
  --exclude 'vendor/wsjtx/build-darwin-*' \
  --exclude 'vendor/ft8_lib/build-darwin-*' \
  "$REPO_DIR/" "$REMOTE:$REMOTE_DIR/"

echo "==> Deployed to $REMOTE:$REMOTE_DIR"
echo "==> Next: ssh $REMOTE 'sh $REMOTE_DIR/provisioning/install.sh'"
