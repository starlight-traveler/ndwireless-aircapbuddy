#!/usr/bin/env python3
"""
main.py – unified CLI for the AirBuddy / SigCap stack (multi-radio edition).

Key additions
-------------
* Multiple Aircrack profiles via [aircrack_<id>] sections.
* New flag    --profile <id>          (default: 'default')
* Auto-mode   launches every profile whose toggle in [auto] is 'yes'.
* Each Aircrack profile runs in its **own process** (multiprocessing) so
  distinct radios can capture concurrently without Python’s GIL.

All other features remain unchanged (MQTT, Speedtest, bypass-setup, etc.).
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import multiprocessing as mp
import os
import subprocess
import sys
import threading
import time
from configparser import ConfigParser, ExtendedInterpolation
from pathlib import Path
from types import ModuleType
from typing import Dict, List, Tuple

# --------------------------------------------------------------------------- #
# PART 1: CONFIGURATION SETUP
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG: dict = {
    # Core directories
    "buddy_dir":          str(Path.home() / "airbuddy"),
    "log_dir":            str(Path.home() / "airbuddy" / "logs"),
    "venv_dir":           str(Path.home() / "venv_firebase"),
}

CONFIG_ENV   = os.environ.get("AIRBUDDY_CONFIG")
CONFIG_LOCAL = Path.home() / ".airbuddy_config"
SETUP_MARKER = Path.home() / ".airbuddy_setup_complete"

# ---------- helpers to load JSON / INI ------------------------------------- #
def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception as e:
        logging.warning("JSON config %s unreadable: %s", path, e)
        return None


def _load_ini(path: Path) -> Tuple[dict, Dict[str, dict], Dict[str, str]]:
    """
    Returns:
        flat_defaults   – legacy flat keys
        aircrack_profiles – id -> dict
        auto_toggles    – id -> yes/no
    """
    cp = ConfigParser(interpolation=ExtendedInterpolation())
    if not cp.read(path):
        return {}, {}, {}

    flat, profiles, auto = {}, {}, {}
    for section in cp.sections():
        sec_l = section.lower()
        if sec_l.startswith("aircrack_"):
            pid = sec_l.split("aircrack_", 1)[1] or "default"
            profiles[pid] = {k.lower(): v for k, v in cp.items(section)}
        elif sec_l == "auto":
            auto = {k.lower(): v for k, v in cp.items(section)}
        else:
            for key, raw in cp.items(section):
                flat[f"{sec_l}_{key.lower()}"] = raw
    return flat, profiles, auto


def load_config(path: Path) -> Tuple[dict, Dict[str, dict], Dict[str, str]]:
    """
    Loads defaults, profiles, and auto-toggles.
    """
    if not path.exists():
        logging.warning("Config %s not found – using defaults only.", path)
        return DEFAULT_CONFIG.copy(), {}, {}

    if path.suffix.lower() == ".json":
        logging.error("JSON config no longer supported for multi-profile mode")
        sys.exit(1)

    flat, profiles, auto = _load_ini(path)
    base = DEFAULT_CONFIG.copy()
    base.update(flat)
    return base, profiles, auto


# --------------------------------------------------------------------------- #
# PART 2: UTILITIES
# --------------------------------------------------------------------------- #
def _lazy(name: str) -> ModuleType:
    return sys.modules[name] if name in sys.modules else importlib.import_module(name)


# --------------------------------------------------------------------------- #
# PART 3: AIRCRACK PROCESS SPAWNER
# --------------------------------------------------------------------------- #
def _spawn_aircrack(profile_id: str, cfg: dict) -> mp.Process:
    """
    Build an argparse.Namespace and launch aircrack_capture.capture_loop()
    in a separate process.
    """
    ns = argparse.Namespace(
        iface       = cfg.get("iface"),
        dir         = cfg.get("dir", "logs/aircrack"),
        capture     = int(cfg.get("capture", 30)),
        interval    = int(cfg.get("interval", 300)),
        channel     = cfg.get("channel"),
        for_span    = cfg.get("for_span"),
        until       = None if cfg.get("until", "none").lower() == "none"
                      else _lazy("datetime").datetime.fromisoformat(cfg["until"]),
        total_limit = None,  # parsed inside aircrack_capture if present
    )
    # total_limit parsing (allow raw strings like 1G)
    tl = cfg.get("total_limit")
    if tl and tl.lower() not in {"none", ""}:
        import re
        m = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmMgGtT]?)", tl.strip())
        if m:
            num, unit = m.groups()
            unit_mult = {"":1,"k":1024,"m":1024**2,"g":1024**3,"t":1024**4}
            ns.total_limit = int(float(num) * unit_mult[unit.lower()])
    proc = mp.Process(
        target=_lazy("aircrack_capture").capture_loop,
        name=f"aircrack-{profile_id}",
        args=(ns,),
        daemon=True,
    )
    proc.start()
    logging.info("Started Aircrack profile '%s' in PID %d", profile_id, proc.pid)
    return proc


# --------------------------------------------------------------------------- #
# PART 4: CLI DEFINITION
# --------------------------------------------------------------------------- #
def _parse_size(text: str) -> int:
    if text.isdigit():
        return int(text)
    units = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
    unit = text[-1].lower()
    return int(float(text[:-1]) * units[unit])


def build_cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="airbuddy",
        description=(
            "Unified CLI for the AirBuddy / SigCap stack (multi-radio).\n"
            "Global flags must appear *before* the sub-command."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )

    p.add_argument("--config", metavar="PATH",
                   help=f"config file (default {CONFIG_LOCAL})")
    p.add_argument("--bypass-setup-warning", action="store_true",
                   help="skip the initialisation check (expert only)")

    sub = p.add_subparsers(dest="cmd", required=True)

    # --- trivial commands --------------------------------------------------
    sub.add_parser("mqtt").set_defaults(cmd_fn=lambda _: _lazy("mqtt").main())
    sub.add_parser("speedtest").set_defaults(cmd_fn=lambda _: _lazy("speedtest_logger").main())
    sub.add_parser("upload").set_defaults(cmd_fn=lambda _: _lazy("firebase").upload_directory())
    sub.add_parser("heartbeat").set_defaults(cmd_fn=lambda _: _lazy("firebase").push_heartbeat())

    # wifi-scan
    ws = sub.add_parser("wifi-scan")
    ws.add_argument("--iface", required=True)
    ws.set_defaults(cmd_fn=lambda a: print(json.dumps(
        _lazy("wifi_scan").scan(a.iface), indent=2)))

    # aircrack (single profile)
    ac = sub.add_parser("aircrack")
    ac.add_argument("--profile", default="default",
                    help="profile id matching [aircrack_<id>] (default)")
    ac.set_defaults(cmd_fn="aircrack")  # handled later

    # auto (all toggled profiles)
    sub.add_parser("auto").set_defaults(cmd_fn="auto")

    # setup passthrough
    su = sub.add_parser("setup")
    su.add_argument("stage", nargs="?", default="all")
    su.set_defaults(cmd_fn="setup")

    return p


# --------------------------------------------------------------------------- #
# PART 5: MAIN
# --------------------------------------------------------------------------- #

def main() -> None:
    mp.set_start_method("fork")  # cheap on Linux

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(message)s")

    args = build_cli().parse_args()

    # --- resolve config ----------------------------------------------------
    cfg_path = Path(args.config) if args.config else Path(CONFIG_ENV or CONFIG_LOCAL)

    if not (cfg_path.exists() and SETUP_MARKER.exists()):
        if not args.bypass_setup_warning and args.cmd_fn not in {"setup"}:
            logging.error("❌ AirBuddy not initialised. Run setup or use --bypass-setup-warning.")
            sys.exit(1)

    GLOBALS, PROFILES, AUTO = load_config(cfg_path)

    # --- dispatch ----------------------------------------------------------
    if args.cmd_fn == "setup":
        setup_script = Path(__file__).with_name("sigcap_setup.py")
        if not setup_script.exists():
            logging.error("Setup script missing: %s", setup_script)
            sys.exit(1)
        cmd = [sys.executable, str(setup_script)]
        if args.stage != "all":
            cmd.append(args.stage)
        if os.geteuid() != 0:
            cmd.insert(0, "sudo")
        subprocess.run(cmd, check=True)
        return

    if args.cmd_fn == "aircrack":
        pid = args.profile.lower()
        if pid not in PROFILES:
            logging.error("Profile [aircrack_%s] not found in config.", pid)
            sys.exit(1)
        _spawn_aircrack(pid, PROFILES[pid]).join()  # run in foreground
        return

    if args.cmd_fn == "auto":
        procs: List[mp.Process] = []
        for pid, cfg in PROFILES.items():
            toggle = AUTO.get(f"aircrack_{pid}", "yes").lower()
            if toggle not in {"yes", "true", "on", "1"}:
                continue
            procs.append(_spawn_aircrack(pid, cfg))

        # also start mqtt / speedtest if toggled
        if AUTO.get("mqtt", "no").lower() in {"yes", "true", "on", "1"}:
            threading.Thread(target=_lazy("mqtt").main, daemon=True).start()
        if AUTO.get("speedtest", "no").lower() in {"yes", "true", "on", "1"}:
            threading.Thread(target=_lazy("speedtest_logger").main,
                             daemon=True).start()

        # Wait until Ctrl-C
        try:
            while any(p.is_alive() for p in procs):
                time.sleep(5)
        except KeyboardInterrupt:
            logging.info("SIGINT received – terminating children…")
            for p in procs:
                p.terminate()
            for p in procs:
                p.join()
        return

    # other trivial commands
    if callable(args.cmd_fn):
        args.cmd_fn(args)
    else:
        logging.error("Unknown command handler")
        sys.exit(1)


if __name__ == "__main__":
    main()
