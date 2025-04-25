#!/usr/bin/env python3
"""
airbuddy_aircrack.py – periodic airodump‑ng capture.

New flags
---------
  --for    3d / 12h / 45m / 600s     stop after this span
  --until  2025‑04‑30T23:59:00‑04:00 stop at absolute time

Either flag ends the loop automatically.  Omit both for “run forever”.
"""
from __future__ import annotations
import argparse, datetime as dt, logging, os, signal, subprocess, sys, time
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
_TIME_UNITS = {"s":1,"m":60,"h":3600,"d":86400}
def parse_span(text:str)->int:
    """
    Parse '3d', '12h', '45m', '600s' into seconds.
    """
    try:
        unit = text[-1].lower(); val=float(text[:-1])
        return int(val * _TIME_UNITS[unit])
    except Exception: raise argparse.ArgumentTypeError(f"bad span '{text}'")

def ensure_monitor(iface:str)->None:
    if "type monitor" in subprocess.run(
            f"iw dev {iface} info",shell=True,text=True,
            stdout=subprocess.PIPE).stdout.lower():
        return
    logging.info("Switching %s to monitor mode", iface)
    subprocess.run(f"ip link set {iface} down",shell=True,check=True)
    subprocess.run(f"iw dev {iface} set type monitor",shell=True,check=True)
    subprocess.run(f"ip link set {iface} up",shell=True,check=True)

def run_airodump_once(iface:str,out:Path,chan:Optional[str],dur:int)->None:
    ts = dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")
    base = out / f"airodump_{ts.replace(':','-')}"
    cmd = ["airodump-ng","-w",str(base),"--output-format","pcap,csv"]
    if chan: cmd += ["--channel",chan]
    cmd.append(iface)
    logging.info("▶ %s"," ".join(cmd))
    proc = subprocess.Popen(cmd,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    try:
        time.sleep(dur); proc.send_signal(signal.SIGINT); proc.wait(timeout=15)
    except Exception:  proc.kill()
    if proc.stderr and (err:=proc.stderr.read().decode().strip()):
        logging.warning("airodump‑ng stderr:\n%s",err)

# --------------------------------------------------------------------------- #
# capture loop
# --------------------------------------------------------------------------- #
def capture_loop(args:argparse.Namespace)->None:
    iface,out_dir,capture,interval,chan = (
        args.iface, Path(args.dir).expanduser(), args.capture,
        args.interval, args.channel)
    if interval<capture: logging.error("--interval < --capture"); sys.exit(1)
    ensure_monitor(iface); out_dir.mkdir(parents=True,exist_ok=True)

    end: Optional[float]=None
    if args.for_span:  end=time.time()+args.for_span
    if args.until:     end=args.until.timestamp()

    logging.info("Writing captures to %s",out_dir)
    while True:
        if end and time.time()>end: break
        run_airodump_once(iface,out_dir,chan,capture)
        sleep_for = interval-capture
        if sleep_for>0: time.sleep(min(sleep_for,max(0,end-time.time())) if end else sleep_for)
    logging.info("Capture loop finished.")

# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _cli()->argparse.ArgumentParser:
    p=argparse.ArgumentParser(prog="aircrack_capture")
    p.add_argument("--iface",required=True,help="monitor‑mode interface")
    p.add_argument("--dir",default="logs/aircrack",help="output directory")
    p.add_argument("--capture",type=int,default=30,metavar="SEC",
                   help="length of each capture (default 30)")
    p.add_argument("--interval",type=int,default=300,metavar="SEC",
                   help="capture+sleep cycle length (default 300)")
    p.add_argument("--channel",metavar="CH",help="lock to specific channel")
    p.add_argument("--for",dest="for_span",type=parse_span,
                   help="stop after span (e.g. 3d, 12h, 45m, 600s)")
    p.add_argument("--until",type=lambda s:dt.datetime.fromisoformat(s),
                   help="stop at RFC‑3339 timestamp (local TZ)")
    return p

def main()->None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(message)s")
    args=_cli().parse_args(); capture_loop(args)

if __name__=="__main__": main()
