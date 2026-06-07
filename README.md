<img src="https://capsule-render.vercel.app/api?type=waving&color=0:0f172a,100:0ea5e9&height=180&section=header&text=winusb-creator&fontSize=36&fontColor=ffffff&fontAlignY=35&desc=Bootable%20Windows%20USB%20from%20your%20Mac%20%E2%80%94%20no%20Windows%20required&descSize=16&descColor=7dd3fc&descAlignY=55" width="100%" />

[![Python](https://img.shields.io/badge/python-%3E%3D3.9-3776ab?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![Platform](https://img.shields.io/badge/platform-macOS-000000?style=flat-square&logo=apple&logoColor=white)](https://www.apple.com/macos)
[![Boot](https://img.shields.io/badge/boot-UEFI-6366f1?style=flat-square)](https://en.wikipedia.org/wiki/UEFI)
[![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)

**Drag an ISO. Type a disk name. Boot Windows.**

Microsoft's Media Creation Tool only runs on Windows. This terminal app fills that gap on macOS: it turns any Windows ISO into a bootable UEFI USB drive, with progress reporting based on what the device has actually written.

* * *

### Install

One command. That's it.

```bash
git clone https://github.com/Barralex/win-bootable-creator.git
cd win-bootable-creator && ./run.sh
```

`run.sh` creates its own venv and installs the single dependency ([rich](https://github.com/Textualize/rich)). Optional global command:

```bash
ln -sf "$(pwd)/run.sh" /opt/homebrew/bin/winusb    # then just: winusb
```

* * *

### How it works

```
You: drag win11.iso into the terminal
                  |
        mount + scan (read-only)
                  |
     format USB → FAT32 (MBR)          ← the only FS UEFI firmware boots natively
                  |
     direct-I/O copy (F_NOCACHE)       ← progress = what the stick actually wrote
                  |
     install.wim > 4 GiB? split on the
     SSD, stream .swm parts to the USB ← Windows Setup rejoins them on install
                  |
         sync · eject · boot
```

No Windows VM. No Boot Camp. No paid tools.

* * *

### What you see

Real device speed, real ETA, a watchdog that catches dying sticks in 15 seconds:

```
Step 5/5 · Copying 1063 files (863.0 MB)
(431/1063) sources/boot.wim
[########----------]  45% · 391.2/863.0 MB · 8.4 MB/s · 1m 12s · ETA 56s
Ctrl+C to cancel
```

* * *

### Safety

Nothing is erased until you type the disk identifier yourself. While the app runs, `caffeinate` keeps the Mac awake, since sleeping mid-write cuts power to the USB bus and leaves the drive hung. An I/O watchdog warns on screen within 15 seconds if the drive stops accepting writes, and shows recovery instructions after two minutes.

Cancellation is handled cleanly: the first `Ctrl+C` stops child processes, unmounts the ISO and reports the exact state the drive was left in; a second one exits immediately with the terminal restored. Common macOS quirks, such as orphaned ISO attaches (`Resource busy`) or Spotlight interfering with formatting (`-69888`), are detected and resolved automatically.

* * *

### Speed expectations

| Drive | Sustained write | Full Windows 11 USB |
|---|---|---|
| Cheap flash stick | 5-10 MB/s | 20-30 min |
| Decent USB 3 stick | 30-80 MB/s | 3-6 min |
| External SSD | 200+ MB/s | ~2 min |

* * *

### Troubleshooting

| Problem | Solution |
|---|---|
| USB not in the PC's boot menu | Disable Secure Boot, or enable USB boot in the BIOS. Pick the **UEFI** entry. |
| `⚠ stalled` for 2+ minutes | The stick died or overheated. Unplug it physically — the app aborts cleanly and you can retry from scratch. |
| A process stuck in `U` state after pulling the drive | That's kernel I/O wait: it clears on its own once the device is gone. Relaunch the app. |
| ISO rejected as "not a Windows image" | The app checks for `sources/` + `efi/`/`bootmgr`. You can continue anyway at your own risk. |

* * *

### Requirements

- macOS (tested on macOS 26.5, Apple Silicon)
- Python 3.9+
- [`wimlib`](https://wimlib.net) — only if the ISO ships an `install.wim` > 4 GiB (typical for Windows 10/11): `brew install wimlib`

* * *

### Development

```bash
./run.sh --check                           # environment + disk list, touches nothing
.venv/bin/python -m py_compile winusb.py   # syntax sanity after edits
```

Single file (`winusb.py`), single dependency, no framework. See [CLAUDE.md](CLAUDE.md) for the full engineering decisions and the measurements behind them.

<img src="https://capsule-render.vercel.app/api?type=waving&color=0:0f172a,100:0ea5e9&height=80&section=footer" width="100%" />
