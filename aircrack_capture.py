#!/usr/bin/env python3
"""
aircrack_capture.py – periodic airodump-ng capture with automatic
archiving, cumulative quota, and channel hopping.

Features
--------
* Each cycle:
    1. Spawn airodump-ng for <capture> seconds.
    2. Write files into a timestamped folder.
    3. Tar-gzip the folder → <timestamp>.tar.gz, delete originals.
    4. Append "timestamp size bytes total bytes" to tar_sizes.log
       and persist total bytes in total_size.txt.

* Stops when:
    • --for / --until reached,      OR
    • --total-limit exceeded.

* Channel selection:
    • --channel omitted      → no --channel arg → airodump hops by itself.
    • --channel 44           → fixed channel 44.
    • --channel 36,40,44,48  → round-robins through the list.

Usage example
-------------
./main.py --bypass-setup-warning aircrack --iface mon5 \
          --capture 45 --interval 900 \
          --channel 36,40,44,48 --total-limit 1G
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from typing import List, Optional

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
_TIME_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_span(text: str) -> int:
    """
    Convert '3d', '12h', '600s' to seconds.
    """
    try:
        unit = text[-1].lower()
        val = float(text[:-1])
        return int(val * _TIME_UNITS[unit])
    except Exception:
        raise argparse.ArgumentTypeError(f"bad span '{text}'")


def parse_chan_list(raw: Optional[str]) -> List[Optional[str]]:
    """
    '' or None  → [None]
    '36'        → ['36']
    '1,6,11'    → ['1','6','11']
    """
    if raw is None or raw.lower() in {"none", ""}:
        return [None]
    return [c.strip() for c in raw.split(",") if c.strip()]


def ensure_monitor(iface: str) -> None:
    if "type monitor" in subprocess.run(
        f"iw dev {iface} info", shell=True, text=True, stdout=subprocess.PIPE
    ).stdout.lower():
        return
    logging.info("Switching %s to monitor mode", iface)
    subprocess.run(f"ip link set {iface} down", shell=True, check=True)
    subprocess.run(f"iw dev {iface} set type monitor", shell=True, check=True)
    subprocess.run(f"ip link set {iface} up", shell=True, check=True)


def run_airodump_once(
    iface: str, out_dir: Path, chan: Optional[str], dur: int
) -> None:
    """
    Launch airodump-ng for `dur` seconds.
    """
    ts = dt.datetime.now(dt.timezone.utc).astimezone().isoformat(
        timespec="seconds"
    )
    base = out_dir / f"capture"
    cmd = ["airodump-ng", "-w", str(base), "--output-format", "pcap,csv"]
    if chan:
        cmd += ["--channel", chan]
    cmd.append(iface)

    logging.info("▶ %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    try:
        time.sleep(dur)
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=15)
    except Exception:
        proc.kill()

    if proc.stderr and (err := proc.stderr.read().decode().strip()):
        logging.warning("airodump-ng stderr:\n%s", err)


def archive_capture(cap_dir: Path) -> int:
    """
    Tar-gzip all files inside `cap_dir`, delete folder, return tar size (bytes).
    """
    tar_path = cap_dir.with_suffix(".tar.gz")
    with tarfile.open(tar_path, "w:gz") as tar:
        for f in cap_dir.iterdir():
            tar.add(f, arcname=f.name)
    # remove originals
    for f in cap_dir.iterdir():
        f.unlink()
    cap_dir.rmdir()
    size = tar_path.stat().st_size
    logging.info("Packed %s (%d bytes)", tar_path.name, size)
    return size


# --------------------------------------------------------------------------- #
# capture loop
# --------------------------------------------------------------------------- #
def capture_loop(args: argparse.Namespace) -> None:
    iface = args.iface
    out_root = Path(args.dir).expanduser()
    capture = args.capture
    interval = args.interval
    chan_list = parse_chan_list(args.channel)
    total_limit = args.total_limit
    if interval < capture:
        logging.error("--interval must be >= --capture")
        sys.exit(1)

    ensure_monitor(iface)
    out_root.mkdir(parents=True, exist_ok=True)

    # persistent counters
    total_file = out_root / "total_size.txt"
    total_bytes = int(total_file.read_text()) if total_file.exists() else 0
    log_file = out_root / "tar_sizes.log"

    # stop conditions
    end: Optional[float] = None
    if args.for_span:
        end = time.time() + args.for_span
    if args.until:
        end = args.until.timestamp()

    logging.info("Writing captures to %s", out_root)

    cycle = 0
    while True:
        if end and time.time() > end:
            break
        if total_limit and total_bytes >= total_limit:
            logging.info(
                "Total limit reached (%d bytes ≥ %d) – stopping.",
                total_bytes,
                total_limit,
            )
            break

        # pick channel
        chan = chan_list[cycle % len(chan_list)]

        # timestamped folder
        ts = (
            dt.datetime.now(dt.timezone.utc)
            .astimezone()
            .isoformat(timespec="seconds")
            .replace(":", "-")
        )
        cap_dir = out_root / ts
        cap_dir.mkdir()

        # capture & archive
        run_airodump_once(iface, cap_dir, chan, capture)
        tar_size = archive_capture(cap_dir)

        # update counters
        total_bytes += tar_size
        total_file.write_text(str(total_bytes))
        with log_file.open("a") as fp:
            fp.write(f"{ts} {tar_size} {total_bytes}\n")

        cycle += 1

        # sleep until next cycle
        sleep_for = interval - capture
        if sleep_for > 0:
            if end:
                sleep_for = min(sleep_for, max(0, end - time.time()))
            time.sleep(sleep_for)

    logging.info("Capture loop finished.")


# --------------------------------------------------------------------------- #
# CLI (stand-alone)
# --------------------------------------------------------------------------- #
def _parse_size(text: str) -> int:
    if text.isdigit():
        return int(text)
    units = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}
    try:
        unit = text[-1].lower()
        val = float(text[:-1])
        return int(val * units[unit])
    except Exception:
        raise argparse.ArgumentTypeError(f"bad size '{text}'")


def _cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aircrack_capture")
    p.add_argument("--iface", required=True, help="monitor-mode interface")
    p.add_argument("--dir", default="logs/aircrack", help="output directory")
    p.add_argument(
        "--capture",
        type=int,
        default=30,
        help="seconds per capture (default 30)",
    )
    p.add_argument(
        "--interval",
        type=int,
        default=300,
        help="capture+sleep cycle length (default 300)",
    )
    p.add_argument(
        "--channel",
        help="fixed channel or comma list (omit for airodump hopping)",
    )
    p.add_argument(
        "--for", dest="for_span", type=parse_span, help="stop after span 3d/12h/…"
    )
    p.add_argument(
        "--until",
        type=lambda s: dt.datetime.fromisoformat(s),
        help="stop at RFC-3339 timestamp",
    )
    p.add_argument(
        "--total-limit",
        type=_parse_size,
        help="stop after cumulative TAR size reaches this limit",
    )
    return p


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s"
    )
    args = _cli().parse_args()
    capture_loop(args)


if __name__ == "__main__":
    main()
