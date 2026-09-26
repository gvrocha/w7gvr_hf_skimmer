#!/bin/sh
# provisioning/build-bundle.sh
#
# Run on the dev Mac to produce everything a Pi needs to run hsd without
# internet access:
#   1. Cross-built jt9/wsprd/decode_ft8/csdr binaries for linux-aarch64,
#      written into vendor/*/build-linux-aarch64/ -- picked up automatically
#      by bin/'s arch-dispatch wrappers, same convention as the darwin-arm64
#      builds already checked in there. csdr replaces rtl_fm's own (buggy)
#      USB demod -- see capture_worker.py's module docstring and
#      hardware/20260920_rtl_fm_demod_bug.md.
#   2. A self-contained offline apk package bundle (python3, gpsd, rtl-sdr,
#      fftw-single-libs, libsamplerate + full dependency closure) with a
#      real APKINDEX.tar.gz, installable on the Pi with `apk add --no-network`.
#      fftw-single-libs/libsamplerate are csdr's runtime deps.
#
# Prerequisite: Colima running with the containerd runtime --
#   colima start --runtime containerd --arch aarch64 --vm-type=vz
#
# Alpine version MUST match the Pi's actual installed version
# (alpine-rpi-3.21.6-aarch64.tar.gz per minimal_pi) -- don't bump this to
# "latest" without also updating what's flashed onto the Pi, or packages
# built/fetched here may not match the base system's ABI/library versions.

set -e

ALPINE_IMAGE="alpine:3.21"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUNDLE_DIR="$REPO_DIR/provisioning/bundle"

WSJTX_APK_DEPS="build-base cmake gfortran boost-dev boost1.84-log boost1.84-log_setup fftw-dev hamlib-dev libusb-dev qt5-qtbase-dev qt5-qtmultimedia-dev qt5-qtserialport-dev qt5-qtsvg-dev qt5-qttools-dev qt5-qtwebsockets-dev linux-headers pkgconf"

echo "==> Cross-building decode_ft8 (linux-aarch64)"
mkdir -p "$REPO_DIR/vendor/ft8_lib/build-linux-aarch64"
colima nerdctl -- run --rm --platform linux/arm64 \
  -v "$REPO_DIR:/repo" -w /repo/vendor/ft8_lib \
  "$ALPINE_IMAGE" sh -c "apk add --no-cache build-base >/dev/null 2>&1 && make clean >/dev/null 2>&1; make decode_ft8"
mv "$REPO_DIR/vendor/ft8_lib/decode_ft8" "$REPO_DIR/vendor/ft8_lib/build-linux-aarch64/decode_ft8"

echo "==> Cross-building jt9/wsprd (linux-aarch64) -- this takes a few minutes"
colima nerdctl -- run --rm --platform linux/arm64 \
  -v "$REPO_DIR:/repo" -w /repo \
  "$ALPINE_IMAGE" sh -c "
    apk add --no-cache $WSJTX_APK_DEPS >/dev/null 2>&1
    cmake -S vendor/wsjtx -B vendor/wsjtx/build-linux-aarch64 \
      -DCMAKE_BUILD_TYPE=Release \
      -DWSJT_SKIP_MAP65=ON -DWSJT_BUILD_UTILS=OFF -DWSJT_BUILD_TESTS=OFF \
      -DWSJT_SKIP_MANPAGES=ON -DWSJT_GENERATE_DOCS=OFF
    cmake --build vendor/wsjtx/build-linux-aarch64 --target jt9 --target wsprd -j4
  "

echo "==> Cross-building csdr (linux-aarch64) -- replaces rtl_fm's buggy USB demod"
colima nerdctl -- run --rm --platform linux/arm64 \
  -v "$REPO_DIR:/repo" -w /repo \
  "$ALPINE_IMAGE" sh -c "
    apk add --no-cache build-base cmake pkgconf fftw-dev libsamplerate-dev >/dev/null 2>&1
    cmake -S vendor/csdr -B vendor/csdr/build-linux-aarch64 -DCMAKE_BUILD_TYPE=Release
    cmake --build vendor/csdr/build-linux-aarch64 --target csdr-bin -j4
    cp vendor/csdr/build-linux-aarch64/src/apps/csdr/csdr vendor/csdr/build-linux-aarch64/csdr
    cp -P vendor/csdr/build-linux-aarch64/src/lib/libcsdr.so* vendor/csdr/build-linux-aarch64/
  "
# The csdr executable dynamically links against libcsdr.so, a shared
# library built alongside it in the same tree -- not a system package, so
# nothing on the default linker search path would find it. Copied flat
# next to the executable (-P preserves the .so -> .so.N -> .so.N.M symlink
# chain); bin/_platform.sh's decoder_bin() adds this directory to
# LD_LIBRARY_PATH/DYLD_LIBRARY_PATH before exec'ing, so it resolves.

echo "==> Building offline apk bundle (python3, gpsd, rtl-sdr, fftw-single-libs, libsamplerate + dependency closure)"
mkdir -p "$BUNDLE_DIR/aarch64"
colima nerdctl -- run --rm --platform linux/arm64 \
  -v "$BUNDLE_DIR:/bundle" \
  "$ALPINE_IMAGE" sh -c "
    apk update >/dev/null 2>&1
    apk fetch --recursive -o /bundle/aarch64 python3 gpsd gpsd-openrc rtl-sdr fftw-single-libs libsamplerate
    apk index --rewrite-arch aarch64 -o /bundle/aarch64/APKINDEX.tar.gz /bundle/aarch64/*.apk
  "
# --rewrite-arch is required: apk fetch --recursive pulls in some packages
# tagged "noarch" (e.g. ncurses-terminfo-base) alongside the aarch64 ones;
# without normalizing them to a single arch, `apk add` on the Pi fails with
# "package mentioned in index not found" for the noarch entries even though
# the .apk files are present and the index technically references them.

echo "==> Verifying the bundle installs offline (fresh container, --network none)"
colima nerdctl -- run --rm --platform linux/arm64 --network none \
  -v "$BUNDLE_DIR:/bundle" \
  "$ALPINE_IMAGE" sh -c "apk add --repository /bundle --allow-untrusted --no-network python3 gpsd gpsd-openrc rtl-sdr fftw-single-libs libsamplerate && which python3 gpsd rtl_fm rtl_sdr"

echo "==> Done."
echo "    Cross-built binaries: vendor/wsjtx/build-linux-aarch64/, vendor/ft8_lib/build-linux-aarch64/, vendor/csdr/build-linux-aarch64/"
echo "    Offline apk bundle:   $BUNDLE_DIR"
echo "    Next: provisioning/deploy.sh to push this to a Pi"
