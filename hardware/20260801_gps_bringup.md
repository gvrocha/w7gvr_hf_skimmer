# GPS bring-up session — 2026-08-01

Dev-machine (macOS) test of a real GPS unit against `src/gps_clock.py`.
This is **not** the Pi deployment setup — `mobile_aprs_gateway`'s GPS lives on `/dev/ttyACM0` under Linux/Alpine, a different device path and possibly a different Python-bindings situation than what's documented here.
Revisit this doc (or write a Pi-specific one) once GPS is wired up on-device — don't assume this macOS procedure carries over unchanged.

## Hardware

u-blox 7 GPS/GNSS receiver — same model `mobile_aprs_gateway` uses.
Confirmed via `ioreg -p IOUSB -l -w0`, matched on `"u-blox 7 - GPS/GNSS Receiver"`.
On macOS it enumerates as a USB-CDC serial device: `/dev/cu.usbmodem1301`.

## Step 1 — confirm the device is alive (no gpsd yet)

`pyserial` isn't installed and wasn't needed — the device is a plain readable character device:

```python
import os, time

fd = os.open("/dev/cu.usbmodem1301", os.O_RDONLY | os.O_NOCTTY)
end = time.time() + 3
buf = b""
while time.time() < end:
    chunk = os.read(fd, 4096)
    if chunk:
        buf += chunk
os.close(fd)
print(buf.decode(errors="replace"))
```

Produced raw NMEA immediately (`$GPTXT`, `$GPRMC`, `$GPGGA`, etc.) — confirms the unit is alive and talking before gpsd is even involved.
With no fix: `$GPGGA` fix quality `0`, `$GPRMC` status `V` (void).

## Step 2 — bring up gpsd

Installed via Homebrew (`brew install gpsd`), but the binary is **not on `PATH`** — it lives at `/opt/homebrew/opt/gpsd/sbin/gpsd`.

**Gotcha:** Homebrew's suggested default control-socket path fails with a permission error in this environment:

```
gpsd:ERROR: filesock() can't bind to local socket /opt/homebrew/var/gpsd.sock. Permission denied(13)
```

Fix: point `-F` at a writable path instead (e.g. `/tmp/gpsd.sock`):

```sh
/opt/homebrew/opt/gpsd/sbin/gpsd -N -F /tmp/gpsd.sock /dev/cu.usbmodem1301
```

Also saw (harmless, ignore):

```
gpsd:ERROR: NTP:SHM: shmat failed,  unit 10: Too many open files(24)
gpsd:ERROR: SHM: shmat failed: Too many open files(24)
```

This is gpsd trying to set up shared memory for NTP time discipline, which isn't needed here — `gps_clock.py` reads GPS time over gpsd's own socket protocol, not via NTP/SHM.
gpsd still comes up and listens correctly on `127.0.0.1:2947` (`lsof -i :2947` confirms) despite these errors.

## Step 3 — no Python `gps` bindings on macOS

`mobile_aprs_gateway`'s `magd.py` does `import gps as _gpslib` (the official gpsd Python bindings).
That module **isn't available on this Mac**: Homebrew's `gpsd` formula doesn't bundle it, and there's no reliably-maintained `gps` package on PyPI either.

Fix (now in `src/gps_clock.py`'s `GpsdSource`): talk to gpsd directly over its own JSON socket protocol instead of any bindings package — stdlib `socket` + `json` only. This is arguably better long-term anyway, since gpsd's wire protocol is identical on the Pi too, so it sidesteps needing to check whether Alpine's gpsd package ships working Python bindings when that day comes.

Protocol, minimally:

```python
import socket, json

sock = socket.create_connection(("127.0.0.1", 2947), timeout=5)
sock.settimeout(5)
# first line off the wire is a VERSION banner -- read and discard it
# then enable streaming:
sock.sendall(b'?WATCH={"enable":true,"json":true}\n')
# from here, gpsd pushes newline-delimited JSON objects continuously:
#   {"class": "VERSION", ...}      -- once, on connect
#   {"class": "DEVICES", ...}      -- device list
#   {"class": "WATCH", ...}        -- ack of the WATCH command
#   {"class": "DEVICE", ...}       -- one per device, as it's identified
#   {"class": "TPV", "mode": ..., "time": ...}   -- the one we care about
#   {"class": "SKY", "satellites": [...]}         -- satellite diagnostics
```

`TPV.mode`: `0`/`1` = no fix, `2` = 2D fix, `3` = 3D fix.
`TPV.time` is only present once a fix exists — a no-fix `TPV` looks like `{"class": "TPV", "device": "...", "mode": 1}`, with no `time` key at all.

## Step 4 — satellite diagnostics (`SKY` reports)

To check *why* no fix, watch `SKY` reports: `satellites` is a list of dicts with `PRN`, `el`, `az`, `ss` (signal strength), `used` (bool — actually contributing to the current fix).

```python
sats = obj.get("satellites", [])
used = [s for s in sats if s.get("used")]
print(f"{len(sats)} visible, {len(used)} used for fix")
```

## Result this session

- Indoors: 0 satellites visible.
- Outdoors (~3 minutes, unclear how open the sky view actually was): 0-2 satellites visible, **0 ever used** for a fix. u-blox 7 generally needs 4+ satellites tracked for a 3D fix (3+ for 2D).
- **No real fix acquired.** `GpsClock`'s no-fix path (`mode=1`, `is_ready()` staying `False`) was validated end-to-end against the real device, both indoors and outdoors — but the actual offset-computation math (the point of `gps_clock.py`) is still only proven against the unit-test stub, not a real fix.

## Next attempt should

- Use a spot with a much more open sky view (unobstructed horizon, away from buildings/trees) — 0-2 visible satellites suggests significant obstruction, not just "needs more time."
- Wait longer if the sky view is confirmed clear — a cold start (no recent almanac) can take several minutes on a u-blox 7, but that's moot until satellite visibility itself improves.
- Once a fix is acquired, re-run the same `GpsClock`/`GpsdSource` combination (no code changes anticipated) and confirm `is_ready()` flips `True` and the computed offset is small (sub-second, since both GPS and system clock should already roughly agree).
