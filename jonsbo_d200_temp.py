#!/usr/bin/env python3
"""
jonsbo_d200_temp.py — Drive the Jonsbo D200 case's front-panel CPU
temperature display from Linux, without Jonsbo's Windows-only "System
Temperature Monitoring" app.

Talks directly to the display over USB HID (VID:PID 0145:1001) via a raw
/dev/hidrawX node. No external dependencies — standard library only.

PROTOCOL
--------
Reverse-engineered from a Wireshark/USBPcap capture of the official
Windows app, then confirmed against a real device. Every ~1s, the app
sends a 64-byte HID Output report (no Report ID; interrupt endpoint 0x03)
twice in a row. Within that packet:

  - byte[1]  = the displayed temperature, as a plain Celsius integer
               (0-99). No offset, no inversion — just the number.
  - byte[5]  = a running "seconds" counter, 0-59.
  - byte[6]  = a faster sub-second counter, 0-99.
  - byte[12] = a rolling sequence counter, 0-255.

The seconds/sub-second/sequence fields appear to matter: sending the same
temperature repeatedly with those fields frozen caused the real display
to latch onto the first value and stop updating. Advancing them each
send keeps the display live.

All other bytes are replayed verbatim from one real captured packet.
Their exact purpose isn't confirmed (the protocol clearly carries more
telemetry than this one display uses — fan RPM, GPU/disk/memory stats,
etc., per the exported functions in Jonsbo's own TempComm.dll), but they
aren't needed to drive this display and are left untouched.

USAGE
-----
  ./jonsbo_d200_temp.py                  # auto-detect CPU sensor, run
  ./jonsbo_d200_temp.py --interval 0.5   # update twice a second
  ./jonsbo_d200_temp.py --hwmon-name coretemp   # force a specific sensor

See README.md for udev/systemd setup to run this without sudo and start
it automatically at boot.
"""

import argparse
import glob
import os
import sys
import time

VID = 0x0145
PID = 0x1001
PACKET_SIZE = 64

# One real 64-byte packet captured from Jonsbo's official Windows app,
# used as a template. Only byte[1] (temperature) and the three "liveness"
# fields below are overwritten per send; everything else is replayed
# exactly as captured.
TEMPLATE = bytes.fromhex(
    "022a000d0f1b23141a0816060512df0000000012000000000927010b00f50000"
    "0000000000000000000000000000000000000000000000000000000000000000"
)

DEFAULT_HWMON_NAMES = ("k10temp", "coretemp", "zenpower")
DEFAULT_ZONE_TYPE = "x86_pkg_temp"


def find_hidraw_node(vid, pid):
    """Return the /dev/hidrawN path for the given VID:PID, or None."""
    for node in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        uevent_path = os.path.join(node, "device", "uevent")
        try:
            with open(uevent_path) as f:
                content = f.read().upper()
        except OSError:
            continue
        if f"{vid:08X}" in content and f"{pid:08X}" in content:
            return "/dev/" + os.path.basename(node)
    return None


def find_hwmon_temp_path(names):
    """Find a CPU temp sensor via the hwmon subsystem (k10temp on AMD,
    coretemp on Intel) — the fast, accurate source. Prefers a sensor
    labeled Tctl/Tdie/Package, the overall-CPU reading, over per-core
    sensors if multiple are exposed."""
    for hwmon_dir in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        try:
            with open(os.path.join(hwmon_dir, "name")) as f:
                name = f.read().strip()
        except OSError:
            continue
        if name not in names:
            continue

        candidates = []
        for label_path in sorted(glob.glob(os.path.join(hwmon_dir, "temp*_label"))):
            idx = label_path.split("temp")[-1].split("_label")[0]
            try:
                with open(label_path) as f:
                    label = f.read().strip()
            except OSError:
                label = ""
            input_path = os.path.join(hwmon_dir, f"temp{idx}_input")
            if os.path.exists(input_path):
                candidates.append((label, input_path))

        for label, path in candidates:
            if label in ("Tctl", "Tdie", "Package id 0", "Package"):
                return path
        if candidates:
            return candidates[0][1]

        fallback = os.path.join(hwmon_dir, "temp1_input")
        if os.path.exists(fallback):
            return fallback
    return None


def find_thermal_zone_path(zone_type):
    """Fallback CPU temp source: an ACPI thermal zone matching zone_type."""
    for zone_dir in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        try:
            with open(os.path.join(zone_dir, "type")) as f:
                if f.read().strip() == zone_type:
                    return os.path.join(zone_dir, "temp")
        except OSError:
            continue
    return None


def find_temp_source(hwmon_names, zone_type):
    """Resolve a temperature source, preferring hwmon over ACPI zones."""
    path = find_hwmon_temp_path(hwmon_names)
    if path:
        return "hwmon", path
    path = find_thermal_zone_path(zone_type)
    if path:
        return "thermal_zone", path
    return None, None


def read_temp_c(temp_path):
    with open(temp_path) as f:
        return round(int(f.read().strip()) / 1000)


def build_packet(temp_c, seconds, subsec, counter):
    """Build one display-update packet from TEMPLATE, with the current
    temperature and liveness fields substituted in."""
    pkt = bytearray(TEMPLATE)
    pkt[1] = max(0, min(99, temp_c))
    pkt[5] = seconds % 60
    pkt[6] = subsec % 100
    pkt[12] = counter % 256
    return bytes(pkt)


def run(vid, pid, hwmon_names, zone_type, interval, verbose):
    node = find_hidraw_node(vid, pid)
    if node is None:
        sys.exit(f"No hidraw device found for {vid:04x}:{pid:04x}. "
                  "Is the display connected? You may need to run as root "
                  "or check your udev permissions (see README.md).")

    source_kind, temp_path = find_temp_source(hwmon_names, zone_type)
    if temp_path is None:
        sys.exit("No usable CPU temperature source found. Pass "
                  "--hwmon-name or --zone-type to point at one explicitly "
                  "(see README.md for how to list what's available).")

    try:
        fd = os.open(node, os.O_RDWR)
    except PermissionError:
        sys.exit(f"Permission denied opening {node}. Run as root, or set "
                  "up a udev rule for passwordless access (see README.md).")

    print(f"Driving {node} ({vid:04x}:{pid:04x}) from {temp_path} "
          f"({source_kind}), every {interval}s. Ctrl+C to stop.")

    counter = 0
    try:
        while True:
            temp_c = read_temp_c(temp_path)
            now = time.time()
            packet = build_packet(temp_c, int(now), int(now * 100), counter)
            try:
                os.write(fd, packet)
                os.write(fd, packet)  # the real app sends each update twice
            except OSError as e:
                print(f"Write failed: {e}", file=sys.stderr)
            if verbose:
                print(f"  {temp_c}C")
            counter += 1
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(
        description="Drive the Jonsbo D200 front-panel temperature "
                     "display from Linux.")
    parser.add_argument("--vid", type=lambda x: int(x, 0), default=VID,
                         help="USB vendor ID of the display (default 0x0145)")
    parser.add_argument("--pid", type=lambda x: int(x, 0), default=PID,
                         help="USB product ID of the display (default 0x1001)")
    parser.add_argument("--hwmon-name", action="append", default=None,
                         help="hwmon driver name to read CPU temp from "
                              "(e.g. k10temp, coretemp). Repeatable. "
                              "Default tries common AMD/Intel drivers.")
    parser.add_argument("--zone-type", default=DEFAULT_ZONE_TYPE,
                         help="ACPI thermal zone type to fall back to if "
                              f"no hwmon driver matches (default "
                              f"'{DEFAULT_ZONE_TYPE}')")
    parser.add_argument("--interval", type=float, default=1.0,
                         help="Seconds between display updates (default "
                              "1.0, matching the real app's cadence)")
    parser.add_argument("-v", "--verbose", action="store_true",
                         help="Print each temperature as it's sent")
    args = parser.parse_args()

    hwmon_names = tuple(args.hwmon_name) if args.hwmon_name else DEFAULT_HWMON_NAMES
    run(args.vid, args.pid, hwmon_names, args.zone_type, args.interval,
        args.verbose)


if __name__ == "__main__":
    main()
