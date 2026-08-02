#!/bin/sh
# provisioning/install.sh
#
# Run ON the Pi (after deploy.sh has rsynced this repo over) to install
# everything hsd needs, entirely offline -- no internet access assumed or
# used at any point. Pre-built linux-aarch64 decoder binaries and the apk
# package bundle must already be present (from build-bundle.sh + deploy.sh
# on the dev Mac).

set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUNDLE_DIR="$REPO_DIR/provisioning/bundle"

for f in "$REPO_DIR/vendor/wsjtx/build-linux-aarch64/jt9" \
         "$REPO_DIR/vendor/wsjtx/build-linux-aarch64/wsprd" \
         "$REPO_DIR/vendor/ft8_lib/build-linux-aarch64/decode_ft8" \
         "$BUNDLE_DIR/aarch64/APKINDEX.tar.gz"; do
  if [ ! -e "$f" ]; then
    echo "ERROR: missing $f -- run provisioning/build-bundle.sh on the dev machine first" >&2
    exit 1
  fi
done

echo "==> Installing python3, gpsd, rtl-sdr from the offline bundle"
apk add --repository "$BUNDLE_DIR" --allow-untrusted --no-network python3 gpsd gpsd-openrc rtl-sdr

echo "==> Installing hsd OpenRC service"
cp "$REPO_DIR/provisioning/hsd.openrc" /etc/init.d/hsd
chmod +x /etc/init.d/hsd
rc-update add hsd default
rc-update add gpsd default

echo "==> Done."
echo "    Start now with: rc-service gpsd start && rc-service hsd start"
echo "    Check with:     $REPO_DIR/src/hsctl.py status"
