#!/usr/bin/env bash
#
# start-indi.sh — detect the connected, supported instruments and launch
# `indiserver -v` with exactly the drivers they need (nothing more).
#
# Why not just list every driver? indiserver runs each named driver as a child
# process. Loading the full ~330-driver set spawns hundreds of processes that
# can't find their hardware (churn + phantom simulator devices), and a few of
# the names (indi_getprop/indi_setprop/indi_eval) are CLIENT tools, not drivers.
#
# Two classes of device, handled differently:
#   * USB-native gear (cameras / focusers / wheels with a vendor SDK) — detected
#     automatically by USB ID below. One vendor driver enumerates every unit of
#     that brand, so multiple identical cameras need only one entry.
#   * Serial & network gear (mounts, focusers/wheels on FTDI/CH340/Prolific
#     cables, GPS, weather) — CANNOT be identified from the USB bridge chip
#     (a mount and a focuser on FTDI cables look identical: 0403:6001). Declare
#     these explicitly in indi-drivers.local or $EXTRA_INDI_DRIVERS.
#
# Usage:
#   ./start-indi.sh                 # detect + launch
#   ./start-indi.sh -n              # dry run: print the command, don't launch
#   ./start-indi.sh --sim           # also load the INDI simulator suite
#   EXTRA_INDI_DRIVERS="indi_eqmod_telescope" ./start-indi.sh
#
set -euo pipefail

PORT="${INDI_PORT:-7624}"
MAXQ="${INDI_MAXQ:-256}"          # per-client send-queue cap (MB); BLOBs need headroom
DRY=0
WITH_SIM=0
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF="${INDI_DRIVERS_CONF:-$HERE/indi-drivers.local}"

for arg in "$@"; do
  case "$arg" in
    -n|--dry-run) DRY=1 ;;
    --sim)        WITH_SIM=1 ;;
    -h|--help)    sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

note() { printf '%s\n' "$*" >&2; }

# ---------------------------------------------------------------------------
# USB-native device map. Exact "vid:pid" is matched FIRST and is REQUIRED for the
# OEM-shared vendor IDs (0547 and 04b4 are reused by ToupTek/Altair/Nncam/Meade/
# QHY/Atik); a bare-vendor match there would start several drivers fighting over
# the same camera. Vendors that own their whole USB vendor ID get a "vid" entry.
#
# Grounded in the udev rules shipped with the vendor packages
# (/lib/udev/rules.d/*.rules) — add new lines as you add hardware.
# ---------------------------------------------------------------------------
declare -A BY_VIDPID=(
  # ToupTek family — G3M662M imaging camera + AAF autofocuser (vendor 0547 shared)
  [0547:15b2]="indi_toupcam_ccd"
  [0547:15b3]="indi_toupcam_ccd"
  [0547:14ad]="indi_toupcam_focuser"
)

declare -A BY_VID=(
  [03c3]="indi_asi_ccd indi_asi_focuser indi_asi_wheel indi_asi_st4"        # ZWO ASI
  [1618]="indi_qhy_ccd"                                                     # QHYCCD (camera + integrated CFW)
  [f266]="indi_svbonycam_ccd indi_svbonycam_wheel indi_svbonycam_focuser"   # SVBony
  [20e7]="indi_atik_ccd indi_atik_wheel"                                    # Atik
  [1e10]="indi_atik_ccd indi_atik_wheel"                                    # Atik (older)
)

# Astro-vendor IDs worth flagging when an *unrecognised* product shows up, so you
# know to add it above. 04b4/0547 are the shared-OEM bootloader/op vendors.
ASTRO_VENDORS=" 0547 04b4 03c3 1618 f266 20e7 1e10 16d0 "

declare -A WANT=()   # the deduped set of drivers to launch

# ---------------------------------------------------------------------- detect
while read -r vid pid desc; do
  vid="${vid,,}"; pid="${pid,,}"
  drivers="${BY_VIDPID[$vid:$pid]:-}"
  [[ -z "$drivers" ]] && drivers="${BY_VID[$vid]:-}"
  if [[ -n "$drivers" ]]; then
    note "  detected $vid:$pid  $desc  ->  $drivers"
    for d in $drivers; do WANT[$d]=1; done
  elif [[ "$ASTRO_VENDORS" == *" $vid "* ]]; then
    note "  UNKNOWN  $vid:$pid  $desc  — astro vendor but no mapping; add it to start-indi.sh"
  fi
done < <(lsusb | sed -nE 's/.*ID ([0-9a-fA-F]{4}):([0-9a-fA-F]{4}) ?(.*)/\1 \2 \3/p')

# -------------------------------------------------------- explicit (serial/net)
EXTRA=()
# shellcheck disable=SC2206
[[ -n "${EXTRA_INDI_DRIVERS:-}" ]] && EXTRA+=( ${EXTRA_INDI_DRIVERS} )
if [[ -f "$CONF" ]]; then
  while IFS= read -r line; do
    line="${line%%#*}"                       # strip comments
    for d in $line; do EXTRA+=("$d"); done    # whitespace-split the rest
  done < "$CONF"
fi
for d in "${EXTRA[@]:-}"; do
  [[ -n "$d" ]] && { WANT[$d]=1; note "  declared  $d"; }
done

# ----------------------------------------------------------------- simulators
if [[ "$WITH_SIM" == 1 ]]; then
  for d in indi_simulator_telescope indi_simulator_ccd indi_simulator_focus \
           indi_simulator_wheel indi_simulator_guide; do
    WANT[$d]=1
  done
fi

# ------------------------------------------------------- validate + assemble
FINAL=()
for d in $(printf '%s\n' "${!WANT[@]}" | sort); do
  if command -v "$d" >/dev/null 2>&1; then
    FINAL+=("$d")
  else
    note "  SKIP $d — driver binary not installed"
  fi
done

if [[ ${#FINAL[@]} -eq 0 ]]; then
  note "No supported devices detected and nothing declared."
  note "Plug hardware in, or add serial/network drivers to: $CONF"
  exit 1
fi

note ""
note "indiserver -v -p $PORT -m $MAXQ ${FINAL[*]}"
[[ "$DRY" == 1 ]] && exit 0
exec indiserver -v -p "$PORT" -m "$MAXQ" "${FINAL[@]}"
