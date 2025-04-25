#!/usr/bin/env python3
"""
main.py – unified CLI for the AirBuddy / SigCap stack.

NEW IN THIS REVISION
--------------------
* Global flag ``--bypass-setup-warning`` lets advanced users skip the
  “please run setup” guard.
* Documentation in the help header updated accordingly.
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

# --------------------------------------------------------------------------- #
# PART 1: CONFIGURATION SETUP
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG: dict = {
    # Core directories
    "buddy_dir":          str(Path.home() / "airbuddy"),
    "log_dir":            str(Path.home() / "airbuddy" / "logs"),
    "venv_dir":           str(Path.home() / "venv_firebase"),

    # Defaults for aircrack capture
    "aircrack_iface":     "wlan0mon",
    "aircrack_dir":       "logs/aircrack",
    "aircrack_capture":   30,           # seconds per capture
    "aircrack_interval":  300,          # seconds between captures
    "aircrack_channel":   None,
    "aircrack_total_limit": None,       # bytes; None → unlimited

    # Which services run in 'auto' mode?
    "auto_mqtt":          True,
    "auto_speedtest":     True,
    "auto_aircrack":      True,
}

CONFIG_ENV   = os.environ.get("AIRBUDDY_CONFIG")
CONFIG_LOCAL = Path.home() / ".airbuddy_config"
SETUP_MARKER = Path.home() / ".airbuddy_setup_complete"

# --------------------------------------------------------------------------- #
# PART 1.1: CONFIG FILE LOADERS
# --------------------------------------------------------------------------- #
def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception as e:
        logging.warning("JSON config %s unreadable: %s", path, e)
        return None


def _load_ini(path: Path) -> dict | None:
    cp = ConfigParser(interpolation=ExtendedInterpolation())
    try:
        if not cp.read(path):
            return None
        flat: dict = {}
        for section in cp.sections():
            for key, raw in cp.items(section):
                flat_key = f"{section.lower().replace('-', '_')}_{key.lower()}"
                raw_lower = raw.lower()
                if raw.isdigit():
                    flat[flat_key] = int(raw)
                elif raw_lower in {"none"}:
                    flat[flat_key] = None
                elif raw_lower in {"yes", "true", "on"}:
                    flat[flat_key] = True
                elif raw_lower in {"no", "false", "off"}:
                    flat[flat_key] = False
                else:
                    flat[flat_key] = raw
        return flat
    except Exception as e:
        logging.warning("INI config %s unreadable: %s", path, e)
        return None


def load_config(path: Path) -> dict:
    cfg = DEFAULT_CONFIG.copy()
    if not path.exists():
        logging.warning("Config %s not found – using defaults.", path)
        return cfg

    if path.suffix.lower() == ".json":
        overrides = _load_json(path) or {}
    else:
        overrides = _load_ini(path) or {}

    cfg.update(overrides)
    logging.debug("Loaded overrides: %s", overrides)
    return cfg

# --------------------------------------------------------------------------- #
# PART 2: MISC UTILITIES
# --------------------------------------------------------------------------- #
def _lazy(name: str) -> ModuleType:
    return sys.modules[name] if name in sys.modules else importlib.import_module(name)


def _mac() -> str:
    try:
        mac = Path("/sys/class/net/eth0/address").read_text().strip().upper()
        return mac.replace(":", "-")
    except Exception:
        return "00-00-00-00-00-00"

# --------------------------------------------------------------------------- #
# PART 3: COMMAND IMPLEMENTATIONS
# --------------------------------------------------------------------------- #
def run_mqtt(_: argparse.Namespace) -> None:
    _lazy("mqtt").main()


def run_speedtest(_: argparse.Namespace) -> None:
    _lazy("speedtest_logger").main()


def run_wifi_scan(args: argparse.Namespace) -> None:
    result = _lazy("wifi_scan").scan(args.iface)
    print(json.dumps(result, indent=2, sort_keys=True))


def run_upload(_: argparse.Namespace) -> None:
    fb = _lazy("firebase")
    logdir = Path(CFG["log_dir"]).expanduser()
    amount = fb.upload_directory(source_dir=logdir, mac=_mac())
    fb.push_data_used(_mac(), amount)
    print(f"Uploaded {amount:.3f} GB of logs.")


def run_heartbeat(_: argparse.Namespace) -> None:
    _lazy("firebase").push_heartbeat(_mac())
    print("Heartbeat sent.")


def run_aircrack(args: argparse.Namespace) -> None:
    target = _lazy("aircrack_capture").capture_loop
    if args.detach:
        t = threading.Thread(target=target, args=(args,), daemon=True)
        t.start()
        logging.info("Aircrack capture detached in thread id=%d", t.ident)
    else:
        target(args)


def _auto_supervisor() -> None:
    jobs: Dict[str, Callable[[argparse.Namespace], None]] = {}
    if CFG.get("auto_mqtt", True):
        jobs["mqtt"] = run_mqtt
    if CFG.get("auto_speedtest", True):
        jobs["speedtest"] = run_speedtest

    for name, fn in jobs.items():
        t = threading.Thread(target=fn, args=(argparse.Namespace(),), daemon=True)
        t.start()
        logging.info("Started %s thread id=%d", name, t.ident)

    if CFG.get("auto_aircrack", True) and CFG.get("aircrack_iface"):
        ac_ns = argparse.Namespace(
            iface       = CFG["aircrack_iface"],
            dir         = CFG["aircrack_dir"],
            capture     = CFG["aircrack_capture"],
            interval    = CFG["aircrack_interval"],
            channel     = CFG["aircrack_channel"],
            for_span    = None,
            until       = None,
            total_limit = CFG["aircrack_total_limit"],
            detach      = False,
        )
        t = threading.Thread(
            target=_lazy("aircrack_capture").capture_loop,
            args=(ac_ns,),
            daemon=True
        )
        t.start()
        logging.info("Started aircrack thread id=%d", t.ident)

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logging.info("Auto supervisor received Ctrl-C; exiting.")


def run_auto(_: argparse.Namespace) -> None:
    supervisor = threading.Thread(target=_auto_supervisor, daemon=False)
    supervisor.start()
    logging.info("Auto supervisor detached in thread id=%d", supervisor.ident)


def run_setup(args: argparse.Namespace) -> None:
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


# --------------------------------------------------------------------------- #
# PART 4: CLI DEFINITION
# --------------------------------------------------------------------------- #
def _parse_size(text: str) -> int:
    if text.isdigit():
        return int(text)
    units = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
    try:
        unit = text[-1].lower()
        val = float(text[:-1])
        return int(val * units[unit])
    except Exception:
        raise argparse.ArgumentTypeError(f"bad size '{text}'")


def _cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="airbuddy",
        description=(
            "Unified CLI for the AirBuddy / SigCap stack\n\n"
            "NOTE: pass --bypass-setup-warning *before* the sub-command to run "
            "without the setup marker."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )

    # --- GLOBAL FLAGS -------------------------------------------------------
    p.add_argument(
        "--config",
        metavar="PATH",
        help=f"path to config file (INI or JSON; default: {CONFIG_LOCAL})",
    )
    p.add_argument(
        "--bypass-setup-warning",
        action="store_true",
        help="skip the initialisation check (expert use only)",
    )

    sub = p.add_subparsers(dest="cmd", required=True)

    # -----------------------------------------------------------------------
    # Simple sub-commands
    sub.add_parser("mqtt").set_defaults(fn=run_mqtt)
    sub.add_parser("speedtest").set_defaults(fn=run_speedtest)
    sub.add_parser("upload").set_defaults(fn=run_upload)
    sub.add_parser("heartbeat").set_defaults(fn=run_heartbeat)

    # wifi-scan
    w = sub.add_parser("wifi-scan", help="one-off Wi-Fi beacon scan")
    w.add_argument("--iface", default=None, help="wireless interface")
    w.set_defaults(fn=run_wifi_scan)

    # aircrack
    a = sub.add_parser("aircrack", help="periodic airodump-ng capture")
    a.add_argument("--iface", help="monitor-mode interface")
    a.add_argument("--dir", help="output directory")
    a.add_argument("--capture", type=int, help="seconds per capture")
    a.add_argument("--interval", type=int, help="seconds between captures")
    a.add_argument("--channel", help="lock to a specific channel")
    a.add_argument("--for", dest="for_span", help="stop after span 3d/12h/…")
    a.add_argument("--until", help="stop at RFC-3339 timestamp")
    a.add_argument(
        "--total-limit",
        type=_parse_size,
        help="stop after cumulative TARs reach this limit (e.g. 500M, 2G)",
    )
    a.add_argument("--detach", action="store_true", help="background mode")
    a.set_defaults(fn=run_aircrack)

    # auto
    sub.add_parser("auto").set_defaults(fn=run_auto)

    # setup
    s = sub.add_parser("setup", help="run the one-shot installer")
    s.add_argument("stage", nargs="?", default="all", help="specific stage or 'all'")
    s.set_defaults(fn=run_setup)

    return p

# --------------------------------------------------------------------------- #
# PART 5: MAIN
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(message)s")

    args = _cli().parse_args()

    # -- Stage 1: special-case the setup command ----------------------------
    if args.cmd == "setup":
        run_setup(args)
        sys.exit(0)

    # -- Stage 2: resolve config path ---------------------------------------
    cfg_path = Path(args.config) if args.config else Path(CONFIG_ENV or CONFIG_LOCAL)

    # -- Stage 3: enforce / optionally bypass the setup guard ---------------
    if not (cfg_path.exists() and SETUP_MARKER.exists()):
        if args.bypass_setup_warning:
            logging.warning(
                "Bypassing setup check because --bypass-setup-warning was given."
            )
        else:
            logging.error(
                "❌ AirBuddy is not initialised on this machine.\n"
                "   Either run:  sudo ./main.py setup\n"
                "   …or (expert-only) re-run with --bypass-setup-warning"
            )
            sys.exit(1)

    # -- Stage 4: load configuration ----------------------------------------
    CFG: dict = load_config(cfg_path)

    # -- Stage 5: inject defaults into CLI namespaces -----------------------
    if args.cmd == "aircrack":
        for field in (
            "iface",
            "dir",
            "capture",
            "interval",
            "channel",
            "for_span",
            "until",
            "total_limit",
        ):
            if getattr(args, field) is None:
                setattr(args, field, CFG.get(f"aircrack_{field}"))
    elif args.cmd == "wifi-scan" and args.iface is None:
        args.iface = CFG.get("wifi_scan_iface", DEFAULT_CONFIG["aircrack_iface"])

    # -- Stage 6: dispatch ---------------------------------------------------
    args.fn(args)
