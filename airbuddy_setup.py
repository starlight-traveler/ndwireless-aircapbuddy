#!/usr/bin/env python3
"""
airbuddy_setup.py
===============

One‑shot installer & configurator for the airbuddy stack.
Creates ~/.airbuddy_config.json and ~/.airbuddy_setup_complete
so other tools know initialisation has happened.

Run with sudo:
    sudo ./airbuddy.py          # run every stage
    sudo ./airbuddy.py services # (re)install only the systemd units
"""
from __future__ import annotations
import argparse, getpass, json, logging, os, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List

# --------------------------------------------------------------------------- #
# CONSTANTS / PATHS
# --------------------------------------------------------------------------- #
USER      = os.environ.get("SUDO_USER") or getpass.getuser()
HOME      = Path(f"/home/{USER}")
BUDDY_DIR = HOME / "sigcap-buddy"
LOG_DIR   = BUDDY_DIR / "logs"
VENV_DIR  = HOME / "venv_firebase"
PYTHON    = sys.executable

CONFIG_PATH   = Path.home() / ".airbuddy_config.json"
SETUP_MARKER  = Path.home() / ".airbuddy_setup_complete"
CONFIG_VER    = 1                                 # bump if the schema changes

APT_PKGS = [
    "build-essential", "git", "iperf3",
    "python3", "python3-pip", "python3-venv",
    "wireshark-common", "wireless-tools",
]

DEBCONF_PRESEED = [
    ("wireshark-common", "wireshark-common/install-setuid", "boolean", "true"),
    ("iperf3",          "iperf3/start_daemon",            "boolean", "false"),
]

# systemd unit file contents ------------------------------------------------- #
SERVICE_SPEEDTEST = f"""\
[Unit]
Description=Speedtest Logger Service
After=network.target

[Service]
ExecStart={VENV_DIR}/bin/python {BUDDY_DIR}/speedtest_logger.py
Restart=always
User={USER}
WorkingDirectory={BUDDY_DIR}
StandardOutput=append:{BUDDY_DIR}/speedtest_logger.out
StandardError=append:{BUDDY_DIR}/speedtest_logger.err

[Install]
WantedBy=multi-user.target
"""

SERVICE_MQTT = f"""\
[Unit]
Description=MQTT Reporter Service
After=network.target

[Service]
ExecStart={VENV_DIR}/bin/python {BUDDY_DIR}/mqtt.py
Restart=always
User={USER}
WorkingDirectory={BUDDY_DIR}
StandardOutput=append:{BUDDY_DIR}/mqtt.out
StandardError=append:{BUDDY_DIR}/mqtt.err

[Install]
WantedBy=multi-user.target
"""

SERVICE_IPERF = """\
[Unit]
Description=iperf3 server
After=syslog.target network.target auditd.service

[Service]
Restart=on-failure
RestartSec=5s
ExecStart=/usr/bin/iperf3 -s -p %i -4 --bind-dev eth0

[Install]
WantedBy=multi-user.target
"""

CRON_LINE = ("{minute} 0 * * * wget -q -O - "
             "https://raw.githubusercontent.com/adstriegel/sigcap-buddy/{branch}/pi-setup.sh | /bin/bash")

# --------------------------------------------------------------------------- #
# SMALL RUNNER
# --------------------------------------------------------------------------- #
def run(cmd: str | List[str], check: bool = True, **popen_kwargs):
    """thin wrapper around subprocess.run with logging + error‑exit"""
    import shlex
    disp = cmd if isinstance(cmd, str) else " ".join(map(shlex.quote, cmd))
    logging.info("▶ %s", disp)
    r = subprocess.run(cmd, shell=isinstance(cmd, str),
                       text=True, capture_output=True, **popen_kwargs)
    if r.stdout: logging.debug(r.stdout.rstrip())
    if r.stderr: logging.debug(r.stderr.rstrip())
    if check and r.returncode != 0:
        logging.error("Command failed: %s", disp); sys.exit(r.returncode)
    return r
# --------------------------------------------------------------------------- #
# STAGES
# --------------------------------------------------------------------------- #
def stage_packages() -> None:
    for pkg, key, typ, val in DEBCONF_PRESEED:
        run(f'echo "{pkg} {key} {typ} {val}" | debconf-set-selections')
    run(r"sed -i 's/#deb-src/deb-src/g' /etc/apt/sources.list")
    run("apt update")
    run("DEBIAN_FRONTEND=noninteractive apt install -y " + " ".join(APT_PKGS))

def stage_patch_wireless() -> None:
    patched = Path("/usr/local/sbin/iwlist")
    if patched.exists():
        logging.info("wireless-tools already patched; skipping."); return
    os.chdir(HOME); run("apt source wireless-tools")
    wt_dir = next(Path(HOME).glob("wireless-tools-*")); os.chdir(wt_dir)
    run(r"sed -i 's/timeout = 15000000/timeout = 30000000/' iwlist.c")
    run("make"); run("make install")

def stage_venv() -> None:
    if not VENV_DIR.exists(): run([PYTHON, "-m", "venv", str(VENV_DIR)])
    run([VENV_DIR / "bin/pip", "install", "--upgrade", "pip"])
    run([VENV_DIR / "bin/pip", "install", "firebase-admin", "jc", "paho-mqtt"])

def stage_clone() -> str:
    branch = "experimental" if (HOME/".experimental").exists() else \
             "testing"      if (HOME/".testing").exists()      else "main"
    if not BUDDY_DIR.exists():
        run(["git", "clone", "-b", branch,
             "https://github.com/adstriegel/sigcap-buddy", str(BUDDY_DIR)])
    else:
        os.chdir(BUDDY_DIR)
        if run(["git", "branch", "--list", branch], check=False).stdout.strip()=="":
            run(["git", "branch", branch, f"origin/{branch}"])
        run(["git", "checkout", branch]); run(["git", "pull"])
    return branch

def stage_logdirs() -> None:
    for sub in ["fmnc-log","iperf-log","pcap-log","ping-log",
                "speedtest-log","wifi-scan"]:
        (LOG_DIR/sub).mkdir(parents=True, exist_ok=True)

def stage_speedtest() -> None:
    arch = run("uname -m", check=False).stdout.strip()
    tmp  = Path("/tmp")/f"ookla-speedtest-{arch}.tgz"; tmp.unlink(missing_ok=True)
    url  = f"https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-linux-{arch}.tgz"
    run(["wget","-P","/tmp",url]); run(["tar","-xf",str(tmp),"-C",str(BUDDY_DIR)])
    run([BUDDY_DIR/"speedtest","--accept-license","--progress=no"])

def stage_secrets() -> None:
    base = "http://ns-mn1.cse.nd.edu/firebase"
    for name in ["nd-schmidt-firebase-adminsdk-d1gei-43db929d8a.json",
                 ".mqtt-config.json"]:
        dest = BUDDY_DIR/name
        if dest.exists(): continue
        run(["wget","--user","nsadmin","--ask-password","-P",str(BUDDY_DIR),
             f"{base}/{name}"])

def stage_services() -> None:
    sd = Path("/etc/systemd/system")
    (sd/"speedtest_logger.service").write_text(SERVICE_SPEEDTEST)
    (sd/"mqtt.service").write_text(SERVICE_MQTT)
    (sd/"iperf3_@.service").write_text(SERVICE_IPERF)
    run("systemctl daemon-reload")
    for svc in ["speedtest_logger.service","mqtt.service"]:
        run(["systemctl","enable","--now",svc])
    run(["systemctl","enable","--now","iperf3_@5201.service"])

def stage_cron(branch: str) -> None:
    raw = subprocess.run(["crontab","-l"],text=True,capture_output=True).stdout
    tgt = CRON_LINE.format(minute=datetime.now(timezone.utc).minute,branch=branch)
    if "pi-setup.sh" not in raw: raw += "\n"+tgt+"\n"
    elif branch not in raw: raw = raw.replace("buddy/*/pi-",f"buddy/{branch}/pi-")
    run("echo '{}' | crontab -".format(raw.replace("'","'\\''")))

def stage_write_config(branch: str) -> None:
    CONFIG_PATH.write_text(json.dumps({
        "version":    CONFIG_VER,
        "user":       USER,
        "home":       str(HOME),
        "buddy_dir":  str(BUDDY_DIR),
        "log_dir":    str(LOG_DIR),
        "venv_dir":   str(VENV_DIR),
        "branch":     branch,
        "created_at": datetime.now(timezone.utc).isoformat()
    }, indent=2))
    SETUP_MARKER.touch()

# --------------------------------------------------------------------------- #
# ORCHESTRATION
# --------------------------------------------------------------------------- #
STAGES: list[str] = ["packages","wireless","venv","clone","logdirs",
                     "speedtest","secrets","services","cron","config"]

def main() -> None:
    if os.geteuid()!=0: logging.error("Run this script with sudo."); sys.exit(1)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(message)s")
    parser = argparse.ArgumentParser(); parser.add_argument("stage",nargs="?",
                      default="all",help=f"one stage or all ({', '.join(STAGES)})")
    arg=parser.parse_args(); wanted=STAGES if arg.stage=="all" else [arg.stage]
    branch="main"
    for s in wanted:
        logging.info("=== Stage: %s ===", s)
        if   s=="packages":      stage_packages()
        elif s=="wireless":      stage_patch_wireless()
        elif s=="venv":          stage_venv()
        elif s=="clone":         branch=stage_clone()
        elif s=="logdirs":       stage_logdirs()
        elif s=="speedtest":     stage_speedtest()
        elif s=="secrets":       stage_secrets()
        elif s=="services":      stage_services()
        elif s=="cron":          stage_cron(branch)
        elif s=="config":        stage_write_config(branch)
        else: logging.error("Unknown stage %s",s); sys.exit(1)
    logging.info("Setup complete – happy hacking!")

if __name__=="__main__": main()
