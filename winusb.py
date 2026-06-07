#!/usr/bin/env python3
"""
WinUSB Creator — Create a bootable Windows USB from macOS.

Microsoft's Media Creation Tool only runs on Windows. This app does the same
job from a Mac:

  1. Pick the Windows ISO.
  2. Pick the target USB drive.
  3. Format the USB as FAT32 (MBR) — the only filesystem UEFI firmware
     boots natively.
  4. Copy the ISO contents with detailed progress (current file, n/total,
     true instantaneous speed, elapsed, ETA).
  5. If `sources/install.wim` exceeds 4 GiB (FAT32 limit), split it into
     `install.swm` parts with wimlib. Windows Setup joins them automatically.

Built-in protections (lessons learned the hard way, one hung flash drive at
a time):
  · Automatic `caffeinate`: the Mac will NOT sleep while this runs. Sleep
    cuts power to the USB bus mid-write and leaves everything hung.
  · I/O watchdog: if the USB stops accepting writes you see it on screen
    within 15 seconds (not 2 hours later), with instructions after 2 minutes.
  · True instantaneous speed (sliding window), not the misleading average
    that still shows 24 MB/s with a dead disk.
  · The copy runs in a worker thread: if a write blocks inside the kernel,
    the UI stays alive and tells you.
  · F_NOCACHE writes with a page-aligned buffer: progress reflects what the
    device actually accepted, not what landed in the kernel's page cache.

Requirements: macOS, Python 3.9+, `rich` (pip), `wimlib` (brew) only if the
ISO ships an install.wim larger than 4 GiB.

Usage:
    python3 winusb.py            # full interactive flow
    python3 winusb.py --check    # only check environment and list disks
"""

import argparse
import fcntl
import math
import mmap
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, List, Optional, Tuple

try:
    from rich import box
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.table import Table
    from rich.text import Text
except ImportError:
    sys.exit(
        "The 'rich' library is missing. Install it with:\n"
        "  python3 -m pip install rich\n"
        "or use ./run.sh, which creates a venv and installs it for you."
    )

# ── Constants ────────────────────────────────────────────────────────────────

FAT32_MAX_FILE = 4 * 1024**3 - 1  # 4 GiB - 1 byte: max file size on FAT32
SPLIT_SIZE_MB = 3800              # size of each .swm part (comfortably under the limit)
VOLUME_LABEL = "WINUSB"           # FAT32: max 11 chars, uppercase
COPY_CHUNK = 4 * 1024 * 1024      # 4 MiB per read/write
NOCACHE_MIN = 1024 * 1024         # F_NOCACHE for files of 1 MiB and up
POLL_S = 0.25                     # monitor refresh interval
STALL_WARN_S = 15                 # on-screen ⚠ after this long without progress
STALL_HELP_S = 120                # help panel if the freeze persists
CANCEL_HINT = "Ctrl+C to cancel"

STALL_HELP = (
    "The USB drive has not accepted writes for over 2 minutes. Typical causes:\n"
    "  · The stick overheated or failed (common with cheap flash drives).\n"
    "  · The USB bus dropped (hub, adapter, or the port itself).\n\n"
    "What to do:\n"
    "  1. Wait one more minute in case it recovers on its own.\n"
    "  2. If not: unplug the USB drive physically. The process will abort with\n"
    "     an I/O error and you can retry from scratch (reconnect it first).\n"
    "  3. If it keeps happening with this stick, try another port or another drive."
)

console = Console()


# ── Helpers ──────────────────────────────────────────────────────────────────

def run(cmd: List[str], timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    """Run a command capturing output; never raises on non-zero returncode."""
    return subprocess.run(cmd, capture_output=True, text=False, timeout=timeout)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def human_pair(done: float, total: float) -> str:
    """'391.2/863.0 MB': progress and total in the total's unit."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if total < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(done)}/{int(total)} B"
            return f"{done:.1f}/{total:.1f} {unit}"
        done /= 1024
        total /= 1024
    return f"{done:.1f}/{total:.1f} TB"


def fmt_secs(s: float) -> str:
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {sec:02d}s" if m else f"{sec}s"


def fmt_eta(s: float) -> str:
    """Compact ETA: seconds under a minute, rounded to the minute after that."""
    if s < 60:
        return f"{int(s)}s"
    m = round(s / 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def clean_path(raw: str) -> Path:
    """Normalize a path pasted or dragged into the terminal (strip quotes/escapes)."""
    s = raw.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    if "\\" in s:
        s = re.sub(r"\\(.)", r"\1", s)  # "Win\ 11.iso" → "Win 11.iso"
    return Path(s).expanduser()


def fail(msg: str) -> None:
    console.print(Panel(msg, border_style="red", title="Error"))
    sys.exit(1)


def shorten(name: str, width: int = 34) -> str:
    return name if len(name) <= width else "…" + name[-(width - 1):]


# Terminal state to restore on abrupt exits (double Ctrl+C): if the process
# dies with echo disabled, the terminal is left "mute".
_TERMIOS_SAVED: Optional[Tuple[int, list]] = None


@contextmanager
def silence_keyboard() -> Iterator[None]:
    """Disable keyboard echo during the long input-free phases.

    Without this, any keypress (Enter included) gets printed to the terminal
    and pushes rich's live display around, leaving ghost copies of the
    progress bar. On exit it discards anything typed so it doesn't leak into
    the next prompt. Ctrl+C keeps working (ISIG stays enabled).
    """
    global _TERMIOS_SAVED
    if not sys.stdin.isatty():
        yield
        return
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    quiet = termios.tcgetattr(fd)
    quiet[3] &= ~(termios.ECHO | termios.ECHONL)  # lflags: no echo
    try:
        _TERMIOS_SAVED = (fd, old)
        termios.tcsetattr(fd, termios.TCSADRAIN, quiet)
        yield
    finally:
        _TERMIOS_SAVED = None
        termios.tcflush(fd, termios.TCIFLUSH)  # discard accumulated keypresses
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def restore_terminal() -> None:
    """Leave the terminal usable (echo + cursor) on exits that skip cleanup."""
    if _TERMIOS_SAVED:
        fd, old = _TERMIOS_SAVED
        try:
            termios.tcsetattr(fd, termios.TCSANOW, old)
        except termios.error:
            pass
    console.show_cursor(True)


# ── Model ────────────────────────────────────────────────────────────────────

@dataclass
class Disk:
    identifier: str       # e.g. disk4
    name: str             # MediaName
    bus: str              # USB, Thunderbolt...
    size: int             # bytes
    removable: bool

    @property
    def device(self) -> str:
        return f"/dev/{self.identifier}"


class SpeedWindow:
    """True instantaneous speed over a sliding window of a few seconds.

    Unlike a cumulative average, this drops to 0 B/s when the disk stops
    writing, instead of staying stuck at the last good speed.
    """

    def __init__(self, window_s: float = 8.0) -> None:
        self.window_s = window_s
        self.samples: deque = deque()  # (timestamp, total_bytes)

    def add(self, total_bytes: int, now: float) -> None:
        self.samples.append((now, total_bytes))
        while self.samples and now - self.samples[0][0] > self.window_s:
            self.samples.popleft()

    def bytes_per_s(self) -> float:
        if len(self.samples) < 2:
            return 0.0
        t0, b0 = self.samples[0]
        t1, b1 = self.samples[-1]
        if t1 <= t0:
            return 0.0
        return max(0.0, (b1 - b0) / (t1 - t0))


# ── Step 0: environment ──────────────────────────────────────────────────────

def macos_version() -> str:
    out = run(["sw_vers", "-productVersion"]).stdout.decode().strip()
    return out or "?"


def wimlib_path() -> Optional[str]:
    return shutil.which("wimlib-imagex")


def prevent_sleep() -> Optional[subprocess.Popen]:
    """Block Mac sleep while the app runs.

    If the Mac sleeps mid-write it cuts power to the USB bus and leaves the
    stick (and wimlib) hung on I/O forever. `-w` ties caffeinate to our PID:
    it dies exactly when we exit, no matter how.
    """
    caf = shutil.which("caffeinate")
    if not caf:
        return None
    return subprocess.Popen(
        [caf, "-dims", "-w", str(os.getpid())],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def banner() -> None:
    title = Text("WinUSB Creator", style="bold cyan")
    sub = Text(
        f"Bootable Windows USB from macOS {macos_version()} · FAT32 + UEFI",
        style="dim",
    )
    console.print(Panel.fit(Text.assemble(title, "\n", sub), border_style="cyan", padding=(1, 4)))


def show_environment() -> None:
    table = Table(box=box.SIMPLE, show_header=False, pad_edge=False)
    table.add_column(style="bold")
    table.add_column()
    table.add_row("macOS", macos_version())
    table.add_row("Python", sys.version.split()[0])
    wl = wimlib_path()
    table.add_row(
        "wimlib",
        f"[green]{wl}[/green]" if wl else "[yellow]not installed[/yellow] (brew install wimlib)",
    )
    console.print(table)
    console.print()


# ── Progress monitor with watchdog ───────────────────────────────────────────

def hashbar(frac: float, width: int = 18) -> Text:
    """Classic terminal-style progress bar: [######----------]."""
    filled = int(width * max(0.0, min(1.0, frac)))
    return Text.assemble(
        ("[", "dim"),
        ("#" * filled, "bold green"),
        ("-" * (width - filled), "dim"),
        ("]", "dim"),
    )


def watch_progress(
    title: str,
    total: int,
    poll: Callable[[], Tuple[int, str]],
    finished: Callable[[], bool],
) -> float:
    """Monitor a write job: bar + instantaneous speed + watchdog.

    poll() -> (bytes_written, current_label); finished() -> True when done.
    The watchdog warns after STALL_WARN_S seconds without progress and shows
    help after STALL_HELP_S. Returns the elapsed seconds.

    The display is 3 short lines (file / bar+stats / cancel hint), each under
    ~78 columns. This matters: if a live-display line exceeds the terminal
    width and the window is resized (e.g. moved to another screen), the
    emulator reflows the text, rich loses track of line counts, and ghost
    copies of the bar are left on screen.
    """
    start = time.monotonic()
    win = SpeedWindow()
    prev_bytes = -1
    last_change = start
    help_shown = False

    def render(label: str, written: int, now: float, done: bool) -> Group:
        speed = win.bytes_per_s()
        stall = now - last_change
        line1 = Text(label or title)
        if not done and stall >= STALL_WARN_S:
            line1.append(f"  ⚠ stalled for {int(stall)}s", style="yellow")
        frac = (written / total) if total else 0.0
        if speed > 0:
            speed_part = (f"{human(speed)}/s", "cyan")
        elif now - start < 5:
            speed_part = ("measuring…", "dim")
        else:
            speed_part = ("0 B/s", "red")
        line2 = Text.assemble(
            hashbar(1.0 if done else frac),
            (f" {min(frac, 1.0):4.0%}", "bold"),
            (" · ", "dim"),
            human_pair(min(written, total), total),
            (" · ", "dim"),
            speed_part,
            (" · ", "dim"),
            fmt_secs(now - start),
        )
        if not done:
            line2.append(" · ", style="dim")
            eta = (total - written) / speed if speed > 0 else None
            line2.append(f"ETA {fmt_eta(eta)}" if eta is not None else "ETA —")
            return Group(line1, line2, Text(CANCEL_HINT, style="dim"))
        return Group(line1, line2)

    with Live(console=console, refresh_per_second=8) as live:
        last_size = console.size
        while True:
            done = finished()
            written, label = poll()
            now = time.monotonic()
            if written != prev_bytes:
                prev_bytes = written
                last_change = now
            win.add(written, now)
            if not done and now - last_change >= STALL_HELP_S and not help_shown:
                help_shown = True
                live.console.print(
                    Panel(STALL_HELP, title="USB not responding", border_style="red")
                )
            if console.size != last_size:
                # The terminal was resized: repaint right away, before the
                # emulator's reflow misaligns the live display area.
                last_size = console.size
                live.refresh()
            live.update(render(label, written, now, done), refresh=done)
            if done:
                break
            time.sleep(POLL_S)
    return time.monotonic() - start


# ── Step 1: ISO ──────────────────────────────────────────────────────────────

def ask_iso() -> Path:
    console.print("[bold]Step 1/5 · ISO image[/bold]")
    console.print("[dim]Tip: you can drag the .iso file into this window and press Enter.[/dim]")
    while True:
        raw = Prompt.ask("Path to the Windows ISO")
        path = clean_path(raw)
        if not path.exists():
            console.print(f"[red]Not found:[/red] {path}")
            continue
        if path.is_dir():
            console.print("[red]That's a folder, not an ISO.[/red]")
            continue
        if path.suffix.lower() != ".iso":
            if not Confirm.ask(f"'{path.name}' doesn't end in .iso, use it anyway?", default=False):
                continue
        size = path.stat().st_size
        console.print(f"[green]✓[/green] {path.name} · {human(size)}\n")
        return path


# ── Step 2: mount and analyze the ISO ────────────────────────────────────────

def find_stale_attach(iso: Path) -> Optional[str]:
    """Find a previous attach of this same ISO and return its /dev/diskN (or None).

    Happens when an earlier run was cut short (or Finder mounted the image):
    the orphaned attach makes the new `hdiutil attach` fail with
    "Resource busy".
    """
    proc = run(["hdiutil", "info", "-plist"])
    if proc.returncode != 0 or not proc.stdout:
        return None
    info = plistlib.loads(proc.stdout)
    target = str(iso.resolve())
    for img in info.get("images", []):
        if img.get("image-path") != target:
            continue
        for ent in img.get("system-entities", []):
            dev = ent.get("dev-entry")
            if dev:
                return dev
    return None


def mount_iso(iso: Path) -> Tuple[str, str]:
    """Mount the ISO read-only. Returns (dev_entry, mount_point)."""
    attach = ["hdiutil", "attach", "-plist", "-nobrowse", "-readonly", str(iso)]
    with console.status(f"Mounting {iso.name}…"):
        proc = run(attach)
        if proc.returncode != 0:
            stale = find_stale_attach(iso)
            if stale:
                run(["hdiutil", "detach", stale, "-force"])
                time.sleep(1)
                proc = run(attach)
    if proc.returncode != 0:
        fail(
            f"Could not mount the ISO:\n{proc.stderr.decode(errors='replace').strip()}"
        )
    info = plistlib.loads(proc.stdout)
    dev, mount = "", ""
    for ent in info.get("system-entities", []):
        if ent.get("mount-point"):
            mount = ent["mount-point"]
            dev = ent.get("dev-entry", dev)
        elif not dev and ent.get("dev-entry"):
            dev = ent["dev-entry"]
    if not mount:
        fail("The ISO mounted but I couldn't find the mount point.")
    console.print(f"[green]✓[/green] ISO mounted at [bold]{mount}[/bold]")
    return dev, mount


def unmount_iso(dev_or_mount: str) -> None:
    run(["hdiutil", "detach", dev_or_mount, "-force"])


def scan_iso(mount: str) -> Tuple[List[Tuple[Path, Path, int]], int]:
    """Walk the mounted ISO. Returns ([(src, rel, size)], total_bytes)."""
    files: List[Tuple[Path, Path, int]] = []
    total = 0
    root = Path(mount)
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            src = Path(dirpath) / fn
            try:
                size = src.stat().st_size
            except OSError:
                continue
            files.append((src, src.relative_to(root), size))
            total += size
    return files, total


def validate_windows_iso(mount: str) -> None:
    root = Path(mount)
    has_sources = (root / "sources").is_dir()
    has_boot = (root / "efi").is_dir() or (root / "bootmgr").exists()
    if not (has_sources and has_boot):
        console.print(
            "[yellow]⚠ This ISO doesn't look like a Windows installation image "
            "(can't find 'sources/' + 'efi/' or 'bootmgr').[/yellow]"
        )
        if not Confirm.ask("Continue anyway?", default=False):
            sys.exit(0)


def find_oversize(files: List[Tuple[Path, Path, int]]) -> Tuple[Optional[Tuple[Path, Path, int]], List[Tuple[Path, Path, int]]]:
    """Single out the install.wim to split (if any) and detect other >4 GiB files."""
    wim_to_split = None
    blockers = []
    for f in files:
        src, rel, size = f
        if size <= FAT32_MAX_FILE:
            continue
        if src.suffix.lower() == ".wim":
            wim_to_split = f
        else:
            blockers.append(f)
    return wim_to_split, blockers


# ── Step 3: USB ──────────────────────────────────────────────────────────────

def list_external_disks() -> List[Disk]:
    proc = run(["diskutil", "list", "-plist", "external", "physical"])
    if proc.returncode != 0 or not proc.stdout:
        return []
    data = plistlib.loads(proc.stdout)
    disks: List[Disk] = []
    for entry in data.get("AllDisksAndPartitions", []):
        ident = entry.get("DeviceIdentifier", "")
        if not ident:
            continue
        info_proc = run(["diskutil", "info", "-plist", ident])
        if info_proc.returncode != 0:
            continue
        info = plistlib.loads(info_proc.stdout)
        disks.append(
            Disk(
                identifier=ident,
                name=(info.get("MediaName") or "External disk").strip(),
                bus=info.get("BusProtocol", "?"),
                size=info.get("TotalSize", entry.get("Size", 0)),
                removable=bool(info.get("RemovableMediaOrExternalDevice", True)),
            )
        )
    return disks


def render_disks(disks: List[Disk]) -> None:
    table = Table(title="External disks detected", box=box.ROUNDED)
    table.add_column("#", justify="right", style="bold")
    table.add_column("Identifier")
    table.add_column("Name")
    table.add_column("Bus")
    table.add_column("Size", justify="right")
    for i, d in enumerate(disks, 1):
        table.add_row(str(i), d.identifier, d.name, d.bus, human(d.size))
    console.print(table)


def pick_disk(needed_bytes: int) -> Disk:
    console.print("[bold]Step 3/5 · Target USB drive[/bold]")
    while True:
        disks = list_external_disks()
        if not disks:
            console.print("[yellow]No external disks detected. Plug in the USB drive.[/yellow]")
            Prompt.ask("Enter to retry (Ctrl+C to quit)", default="", show_default=False)
            continue
        render_disks(disks)
        choice = Prompt.ask(
            "Disk number ([bold]r[/bold] to refresh)",
            default="r",
            show_default=False,
        ).strip().lower()
        if choice == "r":
            continue
        if not choice.isdigit() or not (1 <= int(choice) <= len(disks)):
            console.print("[red]Invalid option.[/red]")
            continue
        disk = disks[int(choice) - 1]
        margin = 64 * 1024 * 1024
        if disk.size < needed_bytes + margin:
            console.print(
                f"[red]Disk {disk.identifier} ({human(disk.size)}) is too small for "
                f"{human(needed_bytes)} of content.[/red]"
            )
            continue
        console.print(f"[green]✓[/green] Target: {disk.identifier} · {disk.name} · {human(disk.size)}\n")
        return disk


# ── Step 4: confirmation + formatting ────────────────────────────────────────

def confirm_destruction(disk: Disk, iso: Path, total: int, will_split: bool) -> None:
    body = (
        f"ISO     [bold]{iso.name}[/bold] ({human(total)} of content)\n"
        f"USB     [bold]{disk.device}[/bold] · {disk.name} · {human(disk.size)}\n"
        f"Format  FAT32 (MBR) · label {VOLUME_LABEL}\n"
        f"WIM     {'will be split into .swm parts (>4 GiB)' if will_split else 'fits as is, no splitting'}\n\n"
        f"[bold red]ALL contents of {disk.device} will be ERASED.[/bold red]"
    )
    console.print(Panel(body, title="Step 4/5 · Confirmation", border_style="red"))
    typed = Prompt.ask(
        f"Type [bold]{disk.identifier}[/bold] to confirm (anything else cancels)"
    ).strip()
    if typed != disk.identifier:
        console.print("Canceled. Nothing was touched.")
        sys.exit(0)


def format_disk(disk: Disk) -> Path:
    """Erase the disk as FAT32/MBR and return the partition's mount point."""
    # Spotlight/fskit tend to grab the volume as soon as it mounts and
    # eraseDisk fails with -69888 "Couldn't unmount disk". Force-unmounting
    # first (and retrying once) avoids it.
    with console.status(f"Unmounting {disk.device}…"):
        run(["diskutil", "unmountDisk", "force", disk.identifier], timeout=120)
    proc = None
    with console.status(f"Formatting {disk.device} as FAT32 (MBR)… this can take a minute"):
        for attempt in (1, 2):
            proc = run(
                ["diskutil", "eraseDisk", "MS-DOS", VOLUME_LABEL, "MBR", disk.identifier],
                timeout=600,
            )
            if proc.returncode == 0:
                break
            if attempt == 1:
                run(["diskutil", "unmountDisk", "force", disk.identifier], timeout=120)
                time.sleep(2)
    if proc.returncode != 0:
        fail(
            f"diskutil eraseDisk failed:\n{proc.stderr.decode(errors='replace').strip()}\n"
            "If the disk is in use, close Finder/apps using it and retry."
        )
    # Find the freshly created FAT32 partition and its mount point
    lst = plistlib.loads(run(["diskutil", "list", "-plist", disk.identifier]).stdout)
    for entry in lst.get("AllDisksAndPartitions", []):
        for part in entry.get("Partitions", []):
            ident = part.get("DeviceIdentifier", "")
            if not ident:
                continue
            info = plistlib.loads(run(["diskutil", "info", "-plist", ident]).stdout)
            mp = info.get("MountPoint")
            if mp:
                console.print(f"[green]✓[/green] USB formatted and mounted at [bold]{mp}[/bold]\n")
                return Path(mp)
    fail("Formatted the disk but can't find the mounted partition.")


# ── Step 5: copy + split ─────────────────────────────────────────────────────

def copy_files(
    files: List[Tuple[Path, Path, int]],
    dest_root: Path,
    skip: Optional[Path],
    total: int,
    header: Optional[str] = None,
) -> None:
    """Copy in a worker thread; the main thread monitors with a watchdog.

    If a write hangs inside the kernel (dead USB), the UI stays alive and
    warns, instead of freezing along with the disk.

    Large files (≥ NOCACHE_MIN) are written with F_NOCACHE: straight to the
    device, bypassing the kernel page cache. Without it, the first few
    hundred MB "fly" at RAM speed, then writeback fills up and everything
    pins at 0 B/s while the kernel drains in the background: a lying progress
    bar, false watchdog alarms, and a minutes-long final sync. With F_NOCACHE
    the stick itself sets the pace (true backpressure). Small files still go
    through the cache, where it actually helps (it batches writes) and adds
    little to the final sync.

    CRITICAL: F_NOCACHE requires the user buffer to be page-aligned; if it
    isn't, the kernel SILENTLY falls back to the cache. A Python `bytes`
    object carries its header up front (misaligned pointer), so the flag did
    nothing. The mmap buffer (anonymous memory, page-aligned) fixes it:
    measured against the same stick, Python+mmap performs identically to C
    (true direct writes, final sync in 0 s). Files are opened with
    buffering=0 so reads/writes are direct syscalls on that buffer, with no
    intermediate Python copies.
    """
    todo = [f for f in files if skip is None or f[1] != skip]
    state = {"bytes": 0, "current": "", "done": 0, "error": None, "finished": False}
    lock = threading.Lock()

    def worker() -> None:
        buf = mmap.mmap(-1, COPY_CHUNK)  # page-aligned buffer (F_NOCACHE requirement)
        view = memoryview(buf)
        try:
            for src, rel, size in todo:
                with lock:
                    state["current"] = str(rel)
                dest = dest_root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(src, "rb", buffering=0) as fin, open(dest, "wb", buffering=0) as fout:
                    if size >= NOCACHE_MIN:
                        try:
                            fcntl.fcntl(fout.fileno(), fcntl.F_NOCACHE, 1)
                        except OSError:
                            pass  # FS without support: falls back to cache, as always
                    while True:
                        n = fin.readinto(buf)
                        if not n:
                            break
                        fout.write(view[:n])
                        with lock:
                            state["bytes"] += n
                with lock:
                    state["done"] += 1
        except OSError as exc:
            with lock:
                state["error"] = f"{state['current']}: {exc}"
        finally:
            with lock:
                state["finished"] = True
            view.release()
            buf.close()

    def poll() -> Tuple[int, str]:
        with lock:
            return state["bytes"], f"({state['done']}/{len(todo)}) {shorten(state['current'])}"

    def finished() -> bool:
        with lock:
            return state["finished"]

    plural = "file" if len(todo) == 1 else "files"
    if header is None:
        header = f"Step 5/5 · Copying {len(todo)} {plural} ({human(total)})"
    console.print(f"[bold]{header}[/bold]")
    threading.Thread(target=worker, daemon=True).start()
    elapsed = watch_progress("preparing…", total, poll, finished)
    if state["error"]:
        fail(
            f"Write error on the USB drive:\n{state['error']}\n\n"
            "Unplug and reconnect the USB drive, then run the app again."
        )
    avg = total / elapsed if elapsed > 0 else 0
    console.print(
        f"[green]✓[/green] {len(todo)} {plural} copied: {human(total)} in "
        f"{fmt_secs(elapsed)} ({human(avg)}/s average)\n"
    )


def run_wimlib_split(wl: str, wim_src: Path, swm: Path, rel: Path, wim_size: int) -> List[Path]:
    """Run `wimlib-imagex split` with progress and return the generated parts."""
    dest_dir = swm.parent
    expected = max(1, math.ceil(wim_size / (SPLIT_SIZE_MB * 1024 * 1024)))
    proc = subprocess.Popen(
        [wl, "split", str(wim_src), str(swm), str(SPLIT_SIZE_MB)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    # Drain stderr in a separate thread: if the pipe fills up, wimlib blocks.
    stderr_buf: List[bytes] = []
    threading.Thread(
        target=lambda: stderr_buf.append(proc.stderr.read() if proc.stderr else b""),
        daemon=True,
    ).start()
    pattern = rel.stem + "*.swm"

    def poll() -> Tuple[int, str]:
        parts = sorted(dest_dir.glob(pattern))
        written = 0
        for p in parts:
            try:
                written += p.stat().st_size
            except OSError:
                pass
        current = parts[-1].name if parts else rel.name
        return written, f"part {len(parts)}/{expected}: {current}"

    def finished() -> bool:
        return proc.poll() is not None

    try:
        elapsed = watch_progress(f"{rel.stem}.swm", wim_size, poll, finished)
    except BaseException:
        proc.kill()  # don't leave an orphaned wimlib writing
        raise
    if proc.returncode != 0:
        err = stderr_buf[0].decode(errors="replace").strip() if stderr_buf else ""
        fail(
            f"wimlib-imagex split failed (code {proc.returncode}):\n{err}\n\n"
            "If the USB drive stopped responding, unplug it, reconnect and retry."
        )
    parts = sorted(dest_dir.glob(pattern))
    written = sum(p.stat().st_size for p in parts)
    avg = written / elapsed if elapsed > 0 else 0
    plural = "part" if len(parts) == 1 else "parts"
    console.print(
        f"[green]✓[/green] WIM split into {len(parts)} {plural} "
        f"({', '.join(p.name for p in parts)}) in {fmt_secs(elapsed)} ({human(avg)}/s)\n"
    )
    return parts


def split_wim(wim_src: Path, rel: Path, wim_size: int, dest_root: Path) -> None:
    """Split install.wim into .swm parts and write them to the USB with backpressure.

    wimlib writes with its own file descriptors and we can't slip F_NOCACHE
    into them, so splitting straight onto the USB inflates the page cache
    just like the old copy did: lying progress and an endless final sync.
    Instead, the split runs against the internal SSD (seconds) and the parts
    are carried to the USB with copy_files (mmap + F_NOCACHE → true progress
    and speed). If the internal disk lacks space, it falls back to the old
    direct mode with a notice.
    """
    wl = wimlib_path()
    if not wl:
        fail(
            "This ISO ships an install.wim larger than 4 GiB and FAT32 can't hold "
            "files that big.\nInstall wimlib and run the app again:\n\n"
            "  brew install wimlib"
        )
    expected = max(1, math.ceil(wim_size / (SPLIT_SIZE_MB * 1024 * 1024)))
    plural = "part" if expected == 1 else "parts"
    console.print(
        f"[bold]Splitting {rel.name}[/bold] ({human(wim_size)}, doesn't fit in FAT32) "
        f"into ~{expected} {plural} of {SPLIT_SIZE_MB} MB…"
    )
    margin = 2 * 1024**3
    if shutil.disk_usage(tempfile.gettempdir()).free > wim_size + margin:
        tmp_dir = Path(tempfile.mkdtemp(prefix="winusb_swm_"))
        try:
            parts = run_wimlib_split(wl, wim_src, tmp_dir / (rel.stem + ".swm"), rel, wim_size)
            files = [(p, rel.parent / p.name, p.stat().st_size) for p in parts]
            total = sum(size for _, _, size in files)
            plural = "part" if len(parts) == 1 else "parts"
            copy_files(
                files,
                dest_root,
                skip=None,
                total=total,
                header=f"Writing {len(parts)} .swm {plural} to the USB ({human(total)})",
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    else:
        console.print(
            "[yellow]⚠ Low space on the internal disk: splitting directly onto the USB "
            "(works the same, but progress is approximate).[/yellow]"
        )
        dest_dir = dest_root / rel.parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        run_wimlib_split(wl, wim_src, dest_dir / (rel.stem + ".swm"), rel, wim_size)


# ── Wrap-up ──────────────────────────────────────────────────────────────────

def finish(disk: Disk, total_elapsed: float) -> None:
    start = time.monotonic()
    with console.status(
        "Flushing write buffers… with slow sticks this can take several "
        "minutes. Do NOT remove the USB drive."
    ):
        os.sync()
    sync_s = time.monotonic() - start
    if sync_s > 1:
        console.print(f"[green]✓[/green] Buffers flushed in {fmt_secs(sync_s)}")
    with console.status(f"Ejecting {disk.device}…"):
        proc = run(["diskutil", "eject", disk.identifier], timeout=300)
    ejected = proc.returncode == 0
    msg = (
        f"[bold green]Bootable USB ready![/bold green] (total time: {fmt_secs(total_elapsed + time.monotonic() - start)})\n\n"
        f"{'You can remove the USB drive now.' if ejected else f'Could not eject it automatically: run  diskutil eject {disk.identifier}'}\n\n"
        "[bold]To boot Windows:[/bold]\n"
        "  1. Plug the USB into the powered-off PC.\n"
        "  2. Power on and open the boot menu (F12/F11/F8/Esc depending on the board).\n"
        "  3. Pick the [bold]UEFI[/bold] entry for the USB drive.\n"
        "  4. If it doesn't show up, disable Secure Boot or enable USB boot in the BIOS."
    )
    console.print(Panel(msg, border_style="green", title="Success"))


# ── Cancellation ─────────────────────────────────────────────────────────────

# True once formatting starts: from that point on, canceling leaves the USB half-done.
_USB_DIRTY = False
_SIGINT_COUNT = 0


def _on_interrupt(signum: int, frame) -> None:
    """SIGINT/SIGTERM → orderly cancellation; second Ctrl+C → immediate exit.

    The first signal cuts the flow with KeyboardInterrupt, which kills any
    running wimlib, unmounts the ISO and reports the USB's state. If cleanup
    gets stuck (or you're in a hurry), the second Ctrl+C exits on the spot
    leaving the terminal healthy.
    """
    global _SIGINT_COUNT
    _SIGINT_COUNT += 1
    if _SIGINT_COUNT >= 2:
        restore_terminal()
        os._exit(130)
    console.print("\n[yellow]Canceling… (Ctrl+C again exits immediately, skipping cleanup)[/yellow]")
    raise KeyboardInterrupt


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Bootable Windows USB from macOS")
    parser.add_argument(
        "--check",
        action="store_true",
        help="only check the environment and list external disks, touch nothing",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _on_interrupt)
    signal.signal(signal.SIGTERM, _on_interrupt)

    banner()
    show_environment()

    if args.check:
        disks = list_external_disks()
        if disks:
            render_disks(disks)
        else:
            console.print("[yellow]No external disks connected.[/yellow]")
        return

    sleep_guard = prevent_sleep()
    if sleep_guard:
        console.print(
            "[dim]☕ caffeinate active: the Mac won't sleep while this runs.[/dim]\n"
        )
    else:
        console.print(
            "[yellow]⚠ 'caffeinate' not found. Keep the Mac from sleeping during "
            "the process: sleep cuts USB power mid-write.[/yellow]\n"
        )

    iso = ask_iso()
    iso_dev, iso_mount = mount_iso(iso)
    try:
        validate_windows_iso(iso_mount)
        files, total = scan_iso(iso_mount)
        if not files:
            fail("The ISO is empty or unreadable.")
        wim_to_split, blockers = find_oversize(files)
        if blockers:
            names = "\n".join(f"  {rel} ({human(size)})" for _, rel, size in blockers)
            fail(
                "These files exceed FAT32's 4 GiB limit and are not .wim "
                f"(they can't be split):\n{names}"
            )
        if wim_to_split and not wimlib_path():
            fail(
                f"'{wim_to_split[1]}' weighs {human(wim_to_split[2])} (>4 GiB) and "
                "wimlib is needed to split it:\n\n  brew install wimlib\n\n"
                "Install it and run the app again."
            )
        console.print(
            f"[green]✓[/green] {len(files)} files · {human(total)} of content\n"
        )

        disk = pick_disk(needed_bytes=total)
        confirm_destruction(disk, iso, total, will_split=wim_to_split is not None)

        # No more input from here to the end: silence the keyboard so stray
        # keypresses don't smear the progress bars.
        global _USB_DIRTY
        _USB_DIRTY = True
        start = time.monotonic()
        with silence_keyboard():
            usb_mount = format_disk(disk)
            copy_files(
                files,
                usb_mount,
                skip=wim_to_split[1] if wim_to_split else None,
                total=total - (wim_to_split[2] if wim_to_split else 0),
            )
            if wim_to_split:
                split_wim(wim_to_split[0], wim_to_split[1], wim_to_split[2], usb_mount)
            finish(disk, time.monotonic() - start)
    finally:
        unmount_iso(iso_dev or iso_mount)
        if sleep_guard:
            sleep_guard.terminate()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.show_cursor(True)
        console.print()
        if _USB_DIRTY:
            console.print(
                Panel(
                    "Process canceled. The USB drive is [bold]incomplete[/bold]: it won't boot as is.\n\n"
                    "· You can unplug it whenever you want (ignore macOS's warning\n"
                    "  about ejecting, it will be reformatted anyway).\n"
                    "· To finish the job, run the app again: it starts from scratch.",
                    title="Canceled",
                    border_style="yellow",
                )
            )
        else:
            console.print("[yellow]Canceled. Nothing was touched.[/yellow]")
        sys.exit(130)
