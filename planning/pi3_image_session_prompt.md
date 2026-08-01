# Starter prompt — Pi 3 B+ image session

Paste this as the opening message in a new Claude Code session (suggested session name: `hf_skimmer_pi3_image`).

---

I need a Raspberry Pi 3 B+ image for `w7gvr_hf_skimmer` (`/Users/gvrocha/dev/hamradio/w7gvr_hf_skimmer`) — a headless FT8/FT4/WSPR receive-only logger. Read that project's `CLAUDE.md` and `planning/implementation_plan.md` first for full context on the software side; this session is scoped to the **image/provisioning** side only.

## Requirements

1. Minimal — Alpine Linux, matching the sibling `mobile_aprs_gateway` station's approach (not a full Raspberry Pi OS desktop image).
2. Headless out of the box — no monitor/keyboard expected.
3. SSH accessible over eth0 when a cable is available.
4. Also brings up an AP (WiFi) so the Pi is reachable via SSH by connecting to that AP directly, for when eth0 isn't an option.
5. Must be able to compile the source of the supporting decoder utilities on-device (`jt9`/`wsprd` from `vendor/wsjtx`, `decode_ft8` from `vendor/ft8_lib` — both already vendored as git submodules in the `w7gvr_hf_skimmer` repo).

## Reusable prior art — read these fully before designing anything new

`/Users/gvrocha/dev/hamradio/mobile_aprs_gateway/provisioning/`:
- `flashing-base-alpine.md` — full flash procedure, image download through `setup-alpine`
- `build-apkovl.sh` — first-boot overlay builder (SSH key + eth0 DHCP/static). Already generic, no APRS-specific content — likely reusable close to as-is.
- `provision.sh` — two-phase bootstrap/configure script. Currently APRS-specific (installs `direwolf`/`aprx`, sets up a `wlan0` AP + `wlan1` internet-uplink dual-radio topology with NAT). Needs adaptation: swap the package list for the WSJT-X/ft8_lib build toolchain, and **drop the wlan1-uplink+NAT complexity** unless this device should also reach the internet (not a stated requirement — confirm before adding it back).
- `20260704-flash-log.md` — a real flash session with hard-won lessons: use a small (8–16GB) SD card for iteration (a 128GB card's erase took ~8 minutes per attempt), the boot partition's file layout must be flattened (not nested under `boot/`) to match what `setup-alpine`/`setup-disk` produces, and a direct Mac↔Pi Ethernet link (no router) needs a static IP baked into the apkovl rather than DHCP.

## Facts already established — don't re-derive these

- **Same image, no new download needed.** The Alpine aarch64 tarball already used for the Pi 5 (`alpine-rpi-3.21.6-aarch64.tar.gz`) covers Pi 3 B+ too — confirmed via the flash log, `bcm2710-rpi-3-b-plus.dtb` ships in that same tarball. Only `armhf` boards (original Pi Zero/1/2v1.1) would need a different image; Pi 3 B+ isn't one of them.
- **WSJT-X build dependencies** (checked directly in `vendor/wsjtx/CMakeLists.txt` and `INSTALL`): `cmake`, C/C++/Fortran compilers, Qt5 (Widgets, SerialPort, Multimedia, PrintSupport, Sql, WebSockets, LinguistTools), Boost (`log`, `log_setup`), FFTW3 (single-precision), Hamlib, libusb. `jt9` and `wsprd` are separate `add_executable` CMake targets from the full `wsjtx` GUI app, but the `find_package(...)` calls for Qt5/Boost/Hamlib appeared to run unconditionally at configure time in what was checked — **open question to verify empirically**: can `cmake --build . --target jt9 --target wsprd` be configured without the full Qt5 GUI dependency chain, or is that chain unavoidable given this CMakeLists.txt structure?
- **`ft8_lib`'s build is much lighter** — plain `Makefile`, no Qt/Boost/Hamlib dependency at all.
- **Hardware constraint**: Pi 3 B+ has 1GB RAM, quad-core Cortex-A53 @ 1.4GHz. Compiling WSJT-X's Qt5+Fortran chain natively risks OOM during linking without a swap file. Open decision needed: is on-device compiling meant to be the normal workflow, or a rare/occasional capability? That determines how much swap to provision by default and whether it's worth documenting a cross-build-and-copy-binaries fallback (Pi 5 or Mac) alongside it.
- **Runtime performance is not a concern** — separately verified (web search) that single-mode headless FT8/WSPR decoding runs fine on Pi 3-class hardware; the only real risk here is the *build* step, not running the resulting daemon.

## Non-negotiable scope (inherited from the parent project)

RX only, no GUI, one band/mode at a time. The image shouldn't install anything beyond what a headless daemon + build toolchain needs — no desktop environment, no unnecessary GUI-oriented Qt modules if they can be avoided.

## Deliverable

An adapted `provisioning/` directory inside `w7gvr_hf_skimmer` (or wherever makes sense once you've looked at the existing layout) — an apkovl builder, a bootstrap/configure script for this project's package set and simplified (AP-only, no uplink) networking, and a flashing doc. Match the operational rigor of the `mobile_aprs_gateway` docs — session logs for real flash attempts are valuable, not just the idealized procedure.
