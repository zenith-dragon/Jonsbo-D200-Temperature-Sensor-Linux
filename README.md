# jonsbo-d200-temp

Drive the Jonsbo D200 case's front-panel CPU temperature display from
Linux, without Jonsbo's Windows-only "System Temperature Monitoring" app.

Reverse-engineered from a USB capture of the official Windows software
(Wireshark + USBPcap) and confirmed against a real device. No official
Linux support exists for this display as far as I could find, so this
fills that gap.

## Requirements

- Linux with the `hidraw` kernel driver (standard on any modern distro)
- Python 3, no external packages required
- A Jonsbo D200 (USB HID device `0145:1001`)

## Quick start

```bash
git clone https://github.com/zenith-dragon/Jonsbo-D200-Temperature-Sensor-Linux
cd jonsbo-d200-temp
sudo python3 jonsbo_d200_temp.py
```

You should see your CPU temperature on the case display within a couple
of seconds. Stop with Ctrl+C.

Run `python3 jonsbo_d200_temp.py --help` for options — most notably
`--hwmon-name` if your CPU temp sensor isn't auto-detected (see
[Troubleshooting](#troubleshooting)).

## Running without sudo

By default only root can open the display's `hidraw` device. To grant
your user access:

```bash
sudo cp 99-jonsbo-d200.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Unplug and reconnect the display's USB cable (or replug the whole case's
USB header) for the new permissions to take effect, then run without
`sudo`.

## Running automatically at login

1. Copy the script somewhere on your `PATH` setup, e.g.:
   ```bash
   mkdir -p ~/.local/bin
   cp jonsbo_d200_temp.py ~/.local/bin/
   ```
2. Install the udev rule above (needed so the service can run as your
   user, not root).
3. Install and enable the systemd user service:
   ```bash
   mkdir -p ~/.config/systemd/user
   cp jonsbo-d200-temp.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now jonsbo-d200-temp.service
   ```

Check it's running with `systemctl --user status jonsbo-d200-temp.service`.

## Troubleshooting

**"No hidraw device found"** — confirm the display shows up with
`lsusb | grep 0145:1001`. If it enumerates under a different VID:PID on
your unit, pass `--vid`/`--pid` explicitly.

**"No usable CPU temperature source found"** — this script reads your
CPU temp via the Linux `hwmon` subsystem, trying `k10temp` (AMD),
`coretemp` (Intel), and `zenpower` (AMD, alternate driver) in that order.
List what's available on your system with:
```bash
for d in /sys/class/hwmon/hwmon*; do echo "$d: $(cat $d/name)"; done
```
then pass the right one with `--hwmon-name <name>`.

**Display shows one static value and never updates** — make sure you're
running an up-to-date copy of the script; earlier development versions
had this bug (frozen timestamp fields caused the firmware to ignore
repeat updates). If it persists, try unplugging and reconnecting the
display's USB cable to clear any stuck state, then rerun.

## Protocol notes

The display is a generic-looking USB HID device (`0145:1001`,
manufacturer string "HWCX") with a deliberately generic report descriptor
— a raw 64-byte pipe in each direction, no semantic hints. All protocol
details below came from capturing real traffic from Jonsbo's Windows app.

Every ~1 second, the app sends a 64-byte HID Output report (no Report ID;
interrupt endpoint `0x03`) twice in a row. Within that packet:

| Byte(s) | Meaning |
|---|---|
| `[1]` | Displayed temperature, plain Celsius integer, no offset |
| `[5]` | Running "seconds" counter, 0–59 |
| `[6]` | Faster sub-second counter, 0–99 |
| `[12]` | Rolling sequence counter, 0–255 |

The seconds/sub-second/sequence fields matter: sending an unchanging
packet (even with the correct temperature) makes the display latch onto
the first value and ignore everything after. Advancing them each send
keeps it live — this script just uses wall-clock time and an incrementing
counter, which is sufficient.

All other bytes are replayed verbatim from one real captured packet and
their exact meaning isn't confirmed. Some are very likely other telemetry
this same protocol carries but this display doesn't use — Jonsbo's own
`TempComm.dll` (from the Windows installer) exports functions like
`SetFanRPM`, `SetGpuDynamicInfo`, `SetDiskDynamicInfo`, and
`SetMemDynamicInfo`, suggesting one shared protocol drives multiple
product SKUs with different display capabilities. Contributions decoding
more of this space (fan RPM display, multi-line displays on other Jonsbo
products, etc.) are welcome.

## License
MIT
