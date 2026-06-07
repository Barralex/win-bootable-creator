# CLAUDE.md

Terminal app (Python + rich) that creates a bootable Windows USB from macOS — Microsoft's Media Creation Tool only runs on Windows, so this fills the gap with a native macOS workflow.

**Status: works end-to-end, validated on real hardware** (full Windows 11 ISO → bootable UEFI stick).

## Architecture

Single-file app (`winusb.py`), five-step interactive flow:

1. **ISO input** — drag & drop supported; `clean_path()` normalizes quotes/escapes.
2. **Mount & analyze** — read-only `hdiutil attach`, sanity-check the layout (`sources/` + `efi/`|`bootmgr`), scan all files.
3. **Target selection** — external physical disks only (`diskutil -plist`), capacity validated.
4. **Destructive confirmation** — user must type the disk identifier, then `eraseDisk MS-DOS WINUSB MBR` (force-unmount + retry to dodge Spotlight/fskit races, error `-69888`).
5. **Copy + split** — direct-I/O copy with truthful progress; oversized `install.wim` split into FAT32-sized `.swm` parts; sync, eject, boot instructions.

`run.sh` bootstraps a venv and installs deps; works through symlinks (`${0:A:h}`), so it can be linked into `PATH` as a global `winusb` command.

## Engineering decisions — each one measured, not guessed

### Direct I/O with real backpressure (`F_NOCACHE` + `mmap` buffer)

The kernel page cache makes USB progress bars lie: writes land in RAM at ~35 MB/s, then pin at 0 B/s for half a minute while writeback drains to a 5-10 MB/s stick. Instead of guessing a throttle rate (impossible to know up front, and it drifts mid-write), files ≥1 MiB are written with `F_NOCACHE`: every 4 MiB `write()` blocks until the device accepts it — the stick itself paces the copy, self-regulating chunk by chunk.

**The gotcha that benchmarks caught**: `F_NOCACHE` requires a page-aligned user buffer, or the kernel *silently* falls back to the cache. Python `bytes` objects are misaligned (object header up front). Measured against the same stick, 16×4 MiB chunks:

| Setup | Per chunk | Final sync | Direct I/O? |
|---|---|---|---|
| C, `malloc` buffer | 0.15 s steady | 0.0 s | ✓ |
| Python, `bytes` buffer | 0.00 s (RAM speed) | 6.5 s | ✗ silently cached |
| Python, `mmap` buffer | 0.15 s steady | 0.0 s | ✓ identical to C |

Hence: anonymous `mmap` buffer + `readinto()`/`write(view[:n])` + `buffering=0` (raw syscalls, no intermediate copies). Python matches C exactly once the syscalls are the same — the bottleneck is the device, never the language.

The 1 MiB threshold is data-driven: in a Windows 11 ISO, ~855 files under 128 KB add up to only 12 MB, and the page cache genuinely helps there (write batching).

### WIM split with SSD staging

`wimlib` writes with its own file descriptors — no way to inject `F_NOCACHE`. Splitting straight onto the USB would reintroduce the lying cache. Solution: split to the internal SSD first (seconds), then carry the parts to the USB through the same direct-I/O copy path. Falls back to direct split when free space < `wim_size + 2 GB`.

### Truthful progress monitoring (`watch_progress`)

- Custom `rich.Live` render, three short lines — current file, `[####----]` bar + stats, cancel hint.
- **Instantaneous speed** over an 8 s sliding window: drops to 0 B/s when the disk dies, unlike a cumulative average that stays stuck at the last good number.
- **I/O watchdog**: on-screen ⚠ after 15 s without progress, recovery instructions panel after 120 s.
- Copy runs in a worker thread: if a `write()` blocks inside the kernel, the UI stays alive and reports it.
- All live lines stay **≤78 columns**: longer lines + a terminal resize make the emulator reflow text, `Live` loses track of line counts, and ghost bars pile up on screen. Repaints immediately on resize as a second guard.

### Sleep protection

`caffeinate -dims -w <pid>` from startup: if the Mac sleeps mid-write, USB bus power cuts out and processes hang in uninterruptible `U` state — unkillable until the drive is physically unplugged. The `-w` flag ties the assertion to the app's lifetime, no cleanup needed.

### CLI-grade cancellation

- Persistent `Ctrl+C to cancel` hint during long phases.
- First `SIGINT`: graceful — kills any running `wimlib`, unmounts the ISO, reports the exact state the USB was left in. Exit code 130.
- Second `SIGINT`: immediate exit, after restoring terminal echo + cursor (`os._exit` skips context managers, so termios state is restored explicitly).
- `SIGTERM` follows the same graceful path.
- Keyboard echo is disabled (`termios`) during output-only phases so stray keypresses can't corrupt the live display; typed-ahead input is flushed on exit.

### Robustness details

- Orphaned ISO attaches (a previous run cut short, or Finder double-click) make `hdiutil attach` fail with "Resource busy": `find_stale_attach()` locates the stale attach by image path, detaches it, retries.
- `wimlib` stderr is drained in a dedicated thread — a full pipe would deadlock the child.
- FAT32/MBR chosen because it's the only combination UEFI firmware boots natively; 4 GiB file limit handled via 3800 MB `.swm` parts that Windows Setup rejoins automatically.

## Performance expectations

Cheap flash drives sustain 5-10 MB/s (initial ~17 MB/s bursts are the controller's SLC cache). Occasional 1-2 s stalls at 0 B/s are the controller's internal garbage collection — they show up identically under C, no software can remove them. Full Windows 11 stick: 20-30 minutes; the progress bar shows the device's true speed the whole way.

## Run & test

```bash
./run.sh            # full interactive flow
./run.sh --check    # environment + disk list only, touches nothing
.venv/bin/python -m py_compile winusb.py   # syntax sanity after edits
```

Manual test suite used during development (inline, no framework):
- Byte-for-byte integrity check of a real copy through `copy_files`.
- Split staging exercised with a synthetic wim (`wimlib-imagex capture`, small `SPLIT_SIZE_MB`).
- Signal handling against a live subprocess: single SIGINT (graceful, exit 130), double SIGINT (immediate), SIGTERM (graceful).
- Worst-case display width assertions (≤78 columns).

## Conventions

- Code, comments, UI and docs in English.
- `Optional[X]` over `X | None` (Python 3.9 compatibility).
- Live-display lines ≤78 columns — see the ghost-bars note above.
- Classic `[####----]` hash bar as the visual signature of the progress UI.

## Roadmap ideas

- Post-copy verification (checksums).
- Explicit warning for `install.esd` > 4 GiB (can't be split, unlike `.wim`).
- `--label` flag for a custom volume name.
