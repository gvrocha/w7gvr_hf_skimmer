# WiFi field ops: connecting to the Pi without Ethernet

For working with the Pi (`minpi`) over its own WiFi AP instead of the
direct-cable Ethernet link -- e.g. when you need to walk away from the
Mac, or the antenna setup makes the Ethernet cable impractical.

**Note:** the Pi's AP has no internet uplink (see `minimal_pi`'s own
docs) -- joining it disconnects your Mac from the internet for as long
as you're connected to it.

## 1. Join the Pi's WiFi network

- **SSID:** `minpi-ap`
- **Password:** `CHANGE_ME_8_TO_63_CHARS`

(Yes, that's a literal placeholder value baked into the flashed image --
it was never customized before flashing. Fine for now; worth changing
later if this AP is ever used somewhere less controlled.)

## 2. SSH into the Pi over WiFi

Once joined, the Pi is at a different address than the Ethernet link:

```sh
ssh root@192.168.4.1
```

(The direct-cable address `root@169.254.100.1` will NOT work over WiFi --
that's a separate interface (`eth0`), only reachable when the Ethernet
cable is connected.)

## 3. Check / control the decode session

```sh
# on the Pi, after SSHing in:
python3 /root/w7gvr_hf_skimmer/src/hsctl.py status   # is it listening? what band/mode?
python3 /root/w7gvr_hf_skimmer/src/hsctl.py start    # begin a session
python3 /root/w7gvr_hf_skimmer/src/hsctl.py stop     # end it
```

To change band/mode, edit `/root/w7gvr_hf_skimmer/config/config.json`
(e.g. `mode`, `dial_frequency`, `decoder`), then restart the daemon so
it picks up the change (band/mode/decoder are static config -- only
read at startup):

```sh
rc-service hsd stop; rm -f /run/hsd.pid; rc-service hsd start
```

Known-good FT8 calling frequencies (dial, USB):

| Band | `dial_frequency` |
|---|---|
| 20m | `14.074M` |
| 10m | `28.074M` |
| 6m  | `50.313M` |
| 2m  | `144.174M` |

Use `"decoder": "ft8_lib"` -- `wsjtx`'s `jt9`/`wsprd` are currently
broken on the Pi (missing runtime shared libraries never vendored into
the offline bundle; see `STATUS.md`).

## 4. Watch decodes live

```sh
python3 /root/w7gvr_hf_skimmer/tools/watch_decodes.py
```

Prints each decoded FT8/FT4/WSPR message as it comes in, grouped into
blocks per decode window -- each new block gets a header with that
window's real UTC time and the station's current grid square (both
pulled live: the time from the spot's own GPS-disciplined timestamp,
the location freshly polled from `gpsd`). Requires `hsd` to actually be
listening (step 3) -- otherwise there's nothing to watch. Ctrl-C to stop
watching (does not stop the underlying decode session).

## 5. Back to Ethernet

Reconnect the direct cable and rejoin your normal WiFi/network on the
Mac -- `root@169.254.100.1` works again once the cable's back.
