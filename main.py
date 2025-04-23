#!/usr/bin/env python3
"""
main.py – unified CLI for the AirBuddy / SigCap stack.

Features:
  • Single entry‑point for all commands: mqtt, speedtest, wifi-scan, upload,
    heartbeat, aircrack, auto, setup.
  • Deferred loading of ~/.airbuddy_config (JSON or INI) with safe defaults.
  • 'setup' command can bootstrap the machine (runs sigcap_setup.py under sudo).
  • Commands like 'aircrack' and 'wifi-scan' pick values first from CLI,
    then from the loaded config, then from built‑in defaults.
  • 'auto' launches MQTT, Speedtest, and (optionally) aircrack in background
    threads, then returns immediately to the shell.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from configparser import ConfigParser, ExtendedInterpolation
from pathlib import Path
from types import ModuleType
from typing import Callable, Dict

# ---------------------------------------------------------------------------- #
# PART 1: CONFIGURATION SETUP
# ---------------------------------------------------------------------------- #

# 1.1 – Built‑in defaults; any missing config keys fall back here.
DEFAULT_CONFIG: dict = {
    # Core directories
    "buddy_dir":          str(Path.home() / "airbuddy"),
    "log_dir":            str(Path.home() / "airbuddy" / "logs"),
    "venv_dir":           str(Path.home() / "venv_firebase"),

    # Defaults for aircrack capture
    "aircrack_iface":     "wlan0mon",
    "aircrack_dir":       "logs/aircrack",
    "aircrack_capture":   30,   # seconds per capture
    "aircrack_interval":  300,  # seconds between captures
    "aircrack_channel":   None, # None → hop all channels

    # Which services run in 'auto' mode?
    "auto_mqtt":          True,
    "auto_speedtest":     True,
    "auto_aircrack":      True,
}

# 1.2 – Where to look for overrides
CONFIG_ENV   = os.environ.get("AIRBUDDY_CONFIG")   # env var override
CONFIG_LOCAL = Path.home() / ".airbuddy_config"    # default path (no extension)
SETUP_MARKER = Path.home() / ".airbuddy_setup_complete"

# 1.3 – Helper to load JSON; non‑fatal on parse error
def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception as e:
        logging.warning("JSON config %s unreadable: %s", path, e)
        return None

# 1.4 – Helper to load INI into a flat dict; non‑fatal on parse error
def _load_ini(path: Path) -> dict | None:
    cp = ConfigParser(interpolation=ExtendedInterpolation())
    try:
        read_files = cp.read(path)  # returns [] if unreadable
        if not read_files:
            return None
        out: dict = {}
        # Flatten each section.key → value
        for section in cp.sections():
            for key, raw in cp.items(section):
                # Build a flat key: section_key (lowercased, dashes→underscores)
                flat_key = f"{section.lower().replace('-', '_')}_{key.lower()}"
                # Simple type handling: int, bool, None, otherwise string
                val_l = raw.lower()
                if raw.isdigit():
                    out[flat_key] = int(raw)
                elif val_l in ("none",):
                    out[flat_key] = None
                elif val_l in ("yes", "true", "on"):
                    out[flat_key] = True
                elif val_l in ("no", "false", "off"):
                    out[flat_key] = False
                else:
                    out[flat_key] = raw
        return out
    except Exception as e:
        logging.warning("INI config %s unreadable: %s", path, e)
        return None

# 1.5 – Master config loader: merges DEFAULT_CONFIG ← external overrides
def load_config(path: Path) -> dict:
    """
    Load JSON or INI config from 'path', flatten it, and merge with defaults.
    Any missing keys stay at DEFAULT_CONFIG.  Errors are logged but not fatal.
    """
    cfg = DEFAULT_CONFIG.copy()  # start from built‑ins

    # If no file → skip
    if not path.exists():
        logging.warning("Config %s not found – using defaults.", path)
        return cfg

    # If JSON by extension
    if path.suffix.lower() == ".json":
        overrides = _load_json(path) or {}
        cfg.update(overrides)
        logging.debug("Loaded JSON config: %s", overrides)
        return cfg

    # Otherwise try INI
    overrides = _load_ini(path) or {}
    cfg.update(overrides)
    logging.debug("Loaded INI config: %s", overrides)
    return cfg

# ---------------------------------------------------------------------------- #
# PART 2: GENERIC UTILITIES
# ---------------------------------------------------------------------------- #

# 2.1 – Lazy‑import modules on demand
def _lazy(name: str) -> ModuleType:
    """
    Import `name` only when first used, then cache it in sys.modules.
    Avoids loading unused modules.
    """
    if name in sys.modules:
        return sys.modules[name]
    return importlib.import_module(name)

# 2.2 – Get the Pi’s MAC with a safe fallback
def _mac() -> str:
    try:
        mac = Path("/sys/class/net/eth0/address").read_text().strip().upper()
        return mac.replace(":", "-")
    except Exception:
        return "00-00-00-00-00-00"

# ---------------------------------------------------------------------------- #
# PART 3: COMMAND IMPLEMENTATIONS
# ---------------------------------------------------------------------------- #

# 3.1 – MQTT publisher loop (blocks)
def run_mqtt(_: argparse.Namespace) -> None:
    _lazy("mqtt").main()

# 3.2 – Speedtest logger loop (blocks)
def run_speedtest(_: argparse.Namespace) -> None:
    _lazy("speedtest_logger").main()

# 3.3 – One‑off Wi‑Fi scan (prints JSON)
def run_wifi_scan(args: argparse.Namespace) -> None:
    """
    args.iface: the wireless interface to scan (string).
    """
    result = _lazy("wifi_scan").scan(args.iface)
    print(json.dumps(result, indent=2, sort_keys=True))

# 3.4 – Upload logs directory now
def run_upload(_: argparse.Namespace) -> None:
    """
    Push all pending log files to Firebase and update data_used.
    """
    fb   = _lazy("firebase")
    utils = _lazy("utils")

    # Use the configured log_dir, expanding ~ if present
    logdir = Path(CFG["log_dir"]).expanduser()
    amount = fb.upload_directory(source_dir=logdir, mac=_mac())
    fb.push_data_used(_mac(), amount)
    print(f"Uploaded {amount:.3f} GB of logs.")

# 3.5 – Push a single heartbeat record
def run_heartbeat(_: argparse.Namespace) -> None:
    _lazy("firebase").push_heartbeat(_mac())
    print("Heartbeat sent.")

# 3.6 – Aircrack capture loop (with optional detach)
def run_aircrack(args: argparse.Namespace) -> None:
    """
    args expects: iface, dir, capture, interval, channel, for_span, until, detach
    """
    target = _lazy("aircrack_capture").capture_loop
    if args.detach:
        # Run in a background daemon thread and return immediately
        th = threading.Thread(target=target, args=(args,), daemon=True)
        th.start()
        logging.info("Aircrack capture detached in thread id=%d", th.ident)
    else:
        # Blocking mode
        target(args)

# 3.7 – Auto mode: start selected services in background, then detach
def _auto_supervisor() -> None:
    """
    Supervisor thread that launches jobs marked enabled in config,
    then idles forever (until Ctrl‑C).
    """
    # Build job list based on config toggles
    jobs: Dict[str, Callable[[argparse.Namespace], None]] = {}
    if CFG.get("auto_mqtt", True):
        jobs["mqtt"] = run_mqtt
    if CFG.get("auto_speedtest", True):
        jobs["speedtest"] = run_speedtest

    # Launch each on a daemon thread
    for name, fn in jobs.items():
        t = threading.Thread(target=fn, args=(argparse.Namespace(),), daemon=True)
        t.start()
        logging.info("Started %s thread id=%d", name, t.ident)

    # Optionally launch aircrack in background if enabled
    if CFG.get("auto_aircrack", True) and CFG.get("aircrack_iface"):
        ac_ns = argparse.Namespace(
            iface    = CFG["aircrack_iface"],
            dir      = CFG["aircrack_dir"],
            capture  = CFG["aircrack_capture"],
            interval = CFG["aircrack_interval"],
            channel  = CFG["aircrack_channel"],
            for_span = None,
            until    = None,
            detach   = False
        )
        t = threading.Thread(
            target=_lazy("aircrack_capture").capture_loop,
            args=(ac_ns,),
            daemon=True,
        )
        t.start()
        logging.info("Started aircrack thread id=%d", t.ident)

    # Keep the supervisor alive so daemon threads keep running
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logging.info("Auto supervisor received Ctrl‑C; exiting.")

def run_auto(_: argparse.Namespace) -> None:
    """
    Detach immediately: spawn a non‑daemon supervisor thread and return.
    """
    sup = threading.Thread(target=_auto_supervisor, daemon=False)
    sup.start()
    logging.info("Auto supervisor detached in thread id=%d", sup.ident)

# 3.8 – Setup command: run sigcap_setup.py under sudo if needed
def run_setup(args: argparse.Namespace) -> None:
    """
    Find sigcap_setup.py next to this script and invoke it (with sudo if not root).
    Pass through an optional single stage argument.
    """
    setup_script = Path(__file__).with_name("sigcap_setup.py")
    if not setup_script.exists():
        logging.error("Setup script not found: %s", setup_script)
        sys.exit(1)

    cmd = [sys.executable, str(setup_script)]
    if args.stage and args.stage != "all":
        cmd.append(args.stage)
    if os.geteuid() != 0:
        cmd.insert(0, "sudo")

    logging.info("Invoking setup: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)
    print("\n✅  Setup completed. You may now run other commands.")

# ---------------------------------------------------------------------------- #
# PART 4: CLI DEFINITION
# ---------------------------------------------------------------------------- #

def _cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="airbuddy",
        description="Unified CLI for the AirBuddy / SigCap stack",
    )
    p.add_argument(
        "--config",
        metavar="PATH",
        help="path to config file (INI or JSON; default: " f"{CONFIG_LOCAL})",
    )

    sub = p.add_subparsers(dest="cmd", required=True)

    # mqtt, speedtest, wifi-scan, upload, heartbeat
    sub.add_parser("mqtt").set_defaults(fn=run_mqtt)
    sub.add_parser("speedtest").set_defaults(fn=run_speedtest)

    w = sub.add_parser("wifi-scan", help="one‑off Wi‑Fi beacon scan")
    w.add_argument("--iface", default=None, help="wireless interface")
    w.set_defaults(fn=run_wifi_scan)

    sub.add_parser("upload", help="upload pending logs").set_defaults(fn=run_upload)
    sub.add_parser("heartbeat", help="send single heartbeat").set_defaults(fn=run_heartbeat)

    # aircrack capture with optional override flags
    a = sub.add_parser("aircrack", help="periodic airodump-ng capture")
    a.add_argument("--iface",    help="monitor-mode interface")
    a.add_argument("--dir",      help="output directory")
    a.add_argument("--capture",  type=int, help="seconds per capture")
    a.add_argument("--interval", type=int, help="seconds between captures")
    a.add_argument("--channel",  help="lock to a specific channel")
    a.add_argument("--for",      dest="for_span", help="stop after span (3d/12h/…)")
    a.add_argument("--until",    help="stop at RFC‑3339 timestamp")
    a.add_argument("--detach",   action="store_true", help="background mode")
    a.set_defaults(fn=run_aircrack)

    sub.add_parser("auto", help="run enabled services in background").set_defaults(fn=run_auto)

    # setup
    s = sub.add_parser("setup", help="run the one‑shot installer")
    s.add_argument("stage", nargs="?", default="all", help="specific stage or 'all'")
    s.set_defaults(fn=run_setup)

    return p

# ---------------------------------------------------------------------------- #
# PART 5: ENTRY‑POINT
# ---------------------------------------------------------------------------- #

if __name__ == "__main__":
    # Initialize logging for the CLI itself
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    # 5.1 – Parse CLI
    args = _cli().parse_args()

    # 5.2 – If 'setup', run it immediately (no config needed)
    if args.cmd == "setup":
        run_setup(args)
        sys.exit(0)

    # 5.3 – Locate the config file: CLI override → ENV → default path
    cfg_path = Path(args.config) if args.config else Path(CONFIG_ENV or CONFIG_LOCAL)

    # 5.4 – Ensure setup has run once
    if not (cfg_path.exists() and SETUP_MARKER.exists()):
        logging.error(
            "❌ AirBuddy is not initialised on this machine.\n"
            "   Please run: sudo ./main.py setup"
        )
        sys.exit(1)

    # 5.5 – Load and merge config
    CFG: dict = load_config(cfg_path)

    # 5.6 – Fill in missing CLI flags from the config for specific commands
    if args.cmd == "aircrack":
        # For each possible flag, if not provided on CLI, pull from CFG
        for field in ("iface", "dir", "capture", "interval", "channel", "for_span", "until"):
            if getattr(args, field) is None:
                # INI flatten used 'aircrack_{field}'
                setattr(args, field, CFG.get(f"aircrack_{field}"))
    elif args.cmd == "wifi-scan":
        if args.iface is None:
            args.iface = CFG.get("wifi_scan_iface", DEFAULT_CONFIG["aircrack_iface"])

    # 5.7 – Dispatch to the requested command
    args.fn(args)
