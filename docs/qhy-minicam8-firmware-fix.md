# QHY MiniCam8: not detected by INDI + USB speed drop — root cause and fix

**Date:** 2026-06-10
**Machine:** ASUS Vivobook M7400QC (Ubuntu, kernel 6.17.0-1025-oem)
**Status:** Diagnosed; fix prepared but **not yet applied** (the install step needs `sudo` in a real terminal — see [Applying the fix](#applying-the-fix)).

## Symptoms

- Plugging in the QHY MiniCam8 makes the USB link "slow": it enumerates at **480 Mbps (USB 2.0)** instead of 5 Gbps.
- The camera never appears to the INDI QHY driver (`indi_qhy_ccd`) — no QHY device is detected.
- In `lsusb` the camera shows up as a generic device, not as a QHY camera:

  ```
  Bus 001 Device 013: ID 1618:0587 Cypress WestBridge
  ```

- Meanwhile the ToupTek G3M662M works fine and shows its proper operational ID:

  ```
  Bus 002 Device 006: ID 0547:15b2 Anchor Chips, Inc. USB3.0 Camera   (5000M)
  ```

## Root cause

QHY cameras ship with a **blank Cypress FX3** USB controller — there is no firmware stored on the camera. The intended boot sequence on every plug-in is:

1. The camera enumerates as the FX3 ROM bootloader: `1618:0587` ("Cypress WestBridge"). The bootloader only speaks **USB 2.0**, which is the speed drop.
2. A udev rule (`/lib/udev/rules.d/85-qhyccd.rules`, line 118) fires:

   ```
   ATTRS{idVendor}=="1618", ATTRS{idProduct}=="0587", \
       RUN+="/sbin/fxload -t fx3 -I /lib/firmware/qhy/miniCam8.img -D $env{DEVNAME}"
   ```

3. `fxload` pushes `miniCam8.img` into the FX3's RAM; the camera reboots, re-enumerates as a real QHY device at USB 3.0, and `indi_qhy_ccd` can then open it.

**Step 2 fails silently on this machine.** Ubuntu's stock `fxload` package (`0.0.20081013-2ubuntu1`, from 2008) predates the FX3 chip entirely — it only supports `an21, fx, fx2, fx2lp`. The `-t fx3` invocation exits with an error on every plug, the firmware never loads, and the camera stays stuck as the USB 2.0 bootloader forever. udev `RUN+=` failures are not surfaced anywhere visible, so nothing ever complained.

Everything else was already in place and is *not* the problem:

| Component | State |
|---|---|
| `85-qhyccd.rules` udev rule | ✅ present, matches `1618:0587` |
| `miniCam8.img` firmware | ✅ present at `/lib/firmware/qhy/` |
| QHY SDK (`libqhyccd.so.20`) | ✅ installed via `indi-3rdparty-libs` |
| `indi_qhy_ccd` driver | ✅ installed at `/usr/bin/` |
| `fxload` FX3 support | ❌ **missing — this is the bug** |

(`/usr/local/sbin/indi-fxload` exists but is only a wrapper script that calls `/sbin/fxload`, so it inherits the same limitation.)

## The fix

QHY's official Linux SDK bundles a patched `fxload` that supports `-t fx3`. It has been downloaded and verified (its usage text lists `fx3` as a device type):

- SDK: `https://www.qhyccd.com/file/repository/publish/SDK/240109/sdk_linux64_24.01.09.tgz`
- Extracted binary, ready to install: `/tmp/sdk_linux64_24.01.09/sbin/fxload`

### Applying the fix

Run in a **regular terminal** (sudo needs a TTY to prompt for the password):

```bash
# 1. Back up Ubuntu's fxload and install QHY's FX3-capable one over it
sudo bash -c 'cp /usr/sbin/fxload /usr/sbin/fxload.ubuntu-orig && \
              install -m 755 /tmp/sdk_linux64_24.01.09/sbin/fxload /usr/sbin/fxload'

# 2. Confirm fx3 is now supported (look for "fx3" in the device types line)
fxload 2>&1 | grep "device types"

# 3. Load the firmware into the already-plugged camera
#    (check the bus/device numbers with `lsusb | grep 1618` first)
sudo /usr/sbin/fxload -t fx3 -I /lib/firmware/qhy/miniCam8.img -D /dev/bus/usb/001/013
```

Alternatively, after step 1 just **unplug and replug the camera** — the udev rule will now work and do step 3 automatically, as it will on every future plug-in.

### Verification

```bash
lsusb          # the 1618:0587 "Cypress WestBridge" entry should be replaced by a QHY device ID
lsusb -t       # the camera should now be on a 5000M link
```

Then start INDI — `indi_qhy_ccd` should detect the MiniCam8.

## Safety notes

- The firmware upload is **RAM-only and volatile**. `fxload -I ... -D ...` writes to the FX3's internal RAM via the USB boot protocol; nothing persistent is written (EEPROM writes would require the explicit `-s`/`-c` flags, which are not used). This is why the camera boots as a blank bootloader on every plug — load-on-every-plug is the designed behavior.
- The FX3 boot ROM is mask ROM (factory-burned, read-only) and cannot be corrupted by this process. Worst case is a non-responsive camera until the next unplug/replug, which always returns it to the clean bootloader state.
- Ubuntu's original binary is preserved at `/usr/sbin/fxload.ubuntu-orig`. Note an `apt` upgrade of the `fxload` package would overwrite the QHY binary — unlikely (the package hasn't changed since 2008), but if QHY detection ever breaks after a dist-upgrade, re-check `fxload 2>&1 | grep fx3`.

## Caveat: bootloader ID collision with the ToupTek G3M662M

The ToupTek G3M662M also uses a Cypress FX3 and enumerates as the **same** `1618:0587` bootloader before its firmware loads (its firmware is uploaded in-process by `libtoupcam`, not by udev). Consequences:

- The two cameras are **indistinguishable** while cold (both are `1618:0587`).
- Once the fixed `fxload` is installed, QHY's udev rule fires on *any* `1618:0587` — so a cold-plugged ToupTek can get MiniCam8 firmware pushed into it. This is harmless (RAM-only; replug clears it) but the ToupTek will misbehave or be misidentified until replugged.

**Recommended plug order:** bring the ToupTek up first and let it reach its operational ID `0547:15b2`, *then* plug in the QHY MiniCam8.

## Quick reference

| | ToupTek G3M662M | QHY MiniCam8 |
|---|---|---|
| Bootloader (cold) USB ID | `1618:0587` (shared!) | `1618:0587` (shared!) |
| Operational USB ID | `0547:15b2` | QHY ID after firmware load |
| Firmware loaded by | `libtoupcam` (in-process) | udev rule → `fxload -t fx3` |
| INDI driver | `indi_toupcam_ccd` (+ `indi_toupcam_wheel`) | `indi_qhy_ccd` |
