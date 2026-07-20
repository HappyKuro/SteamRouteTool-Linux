#!/usr/bin/env python3
"""
SteamRouteTool (Linux port)
===========================

A Linux reimplementation of HappyKuro / dfrood's SteamRouteTool -- a small
utility that blocks specific Valve SDR (Steam Datagram Relay) routes so
Steam's matchmaking picks a different one.

The original is a Windows Forms app that edits Windows Firewall rules via
the NetFwTypeLib COM API (see ClearTF2RoutingToolRules / SetRule in the
upstream Main.cs). Windows Firewall's COM API has no Linux equivalent, and
WinForms doesn't run on Linux -- so this isn't a recompile, it's a from
scratch reimplementation of the same idea on top of iptables:

  * Fetches the same Valve endpoint the original uses:
        https://api.steampowered.com/ISteamApps/GetSDRConfig/v1?appid=<id>
  * Shows ping to each relay.
  * Blocks/unblocks specific relays, or whole "pops" (data centers), using
    a dedicated iptables chain (STEAMROUTETOOL) hooked into OUTPUT, so it
    never touches your other firewall rules.
  * Derives "is this blocked?" live from iptables itself rather than
    keeping separate state, so it can never disagree with reality.

Requirements: Python 3.8+, iptables, and (optionally) ping. Must run as
root, for the same reason the original needs an Administrator prompt.

Usage:
  sudo ./steamroutetool.py                interactive menu
  sudo ./steamroutetool.py --list         print routes + current state, exit
  sudo ./steamroutetool.py --block fra    block every relay in pop "fra"
  sudo ./steamroutetool.py --unblock fra  unblock every relay in pop "fra"
  sudo ./steamroutetool.py --clear        remove every rule this tool made
  sudo ./steamroutetool.py --appid 730    use CS2's SDR config instead of TF2's
  sudo ./steamroutetool.py --dry-run      print iptables commands, don't run them

Credits: original tool by Froody (dfrood/SteamRouteTool), fork by HappyKuro.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

CHAIN = "STEAMROUTETOOL"
COMMENT_PREFIX = "SteamRouteTool:"
DEFAULT_APPID = 440  # Team Fortress 2. Try 730 for CS2.
CONFIG_URL = "https://api.steampowered.com/ISteamApps/GetSDRConfig/v1?appid={appid}"
PORT_RANGE = "27015:27202"  # matches the original tool's fixed RemotePorts range
PING_TIMEOUT = 1  # seconds
READONLY_FLAGS = {"-S", "-L", "-C"}

DRY_RUN = False
HAVE_PING = True
CURRENT_APPID = DEFAULT_APPID


# --------------------------------------------------------------------------
# terminal helpers
# --------------------------------------------------------------------------

def supports_color():
    return sys.stdout.isatty()


def c(text, code):
    return f"\033[{code}m{text}\033[0m" if supports_color() else text


def clear_screen():
    if supports_color():
        sys.stdout.write("\033[H\033[J")


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


# --------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------

class Route:
    """One Valve SDR 'pop' (point of presence / data center)."""

    __slots__ = ("name", "desc", "relays", "expanded", "ping")

    def __init__(self, name, desc=None):
        self.name = name
        self.desc = desc
        self.relays = []  # list[(ip, port_range)] in stable order
        self.expanded = False  # UI-only state
        self.ping = {}  # ip -> float ms, or None

    @property
    def label(self):
        return self.desc or self.name


def route_block_state(route, blocked):
    """Returns 'all', 'some', or 'none' for how much of this pop is blocked."""
    total = len(route.relays)
    count = sum(1 for ip, _ in route.relays if (route.name, ip) in blocked)
    if count == 0:
        return "none"
    if count == total:
        return "all"
    return "some"


# --------------------------------------------------------------------------
# fetching + parsing Valve's SDR config
# --------------------------------------------------------------------------

def fetch_routes(appid):
    url = CONFIG_URL.format(appid=appid)
    req = urllib.request.Request(url, headers={"User-Agent": "SteamRouteTool-Linux/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
    except urllib.error.URLError as e:
        die(f"Couldn't reach Steam's SDR config endpoint: {e}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        die(f"Steam returned something that wasn't valid JSON: {e}")

    pops = data.get("pops")
    if not isinstance(pops, dict):
        die("Unexpected response shape from GetSDRConfig (no 'pops' object) -- "
            "Valve may have changed the API.")

    routes = []
    for name, value in pops.items():
        if not isinstance(value, dict) or "relays" not in value:
            continue
        # mirrors the original's `rc.Value.ToString().Contains("cloud-test")` check
        if "cloud-test" in json.dumps(value):
            continue

        route = Route(name, desc=value.get("desc"))
        for relay in value.get("relays") or []:
            if not isinstance(relay, dict):
                continue
            ip = relay.get("ipv4")
            if not ip:
                continue
            route.relays.append((ip, relay.get("port_range", "")))

        if route.relays:
            routes.append(route)

    routes.sort(key=lambda r: r.label.lower())
    return routes


# --------------------------------------------------------------------------
# pinging
# --------------------------------------------------------------------------

def ping_ip(ip):
    if not HAVE_PING:
        return None
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(PING_TIMEOUT), ip],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=PING_TIMEOUT + 2,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    m = re.search(r"time[=<]\s*([\d.]+)", result.stdout)
    return float(m.group(1)) if m else None


def ping_routes(routes, only=None):
    """Ping every relay in `routes` (or just `only`), in parallel."""
    targets = only if only is not None else routes
    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = {}
        for route in targets:
            for ip, _ in route.relays:
                futures[pool.submit(ping_ip, ip)] = (route, ip)
        for fut in as_completed(futures):
            route, ip = futures[fut]
            try:
                route.ping[ip] = fut.result()
            except Exception:
                route.ping[ip] = None


# --------------------------------------------------------------------------
# firewall (iptables) backend
# --------------------------------------------------------------------------

class _FakeResult:
    returncode = 0
    stdout = ""


def run_iptables(args):
    mutating = args[0] not in READONLY_FLAGS
    if DRY_RUN and mutating:
        print(c("[dry-run]", "2"), "iptables " + " ".join(args))
        return _FakeResult()
    return subprocess.run(["iptables"] + args, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True)


def ensure_setup():
    run_iptables(["-N", CHAIN])  # non-zero just means "already exists" -- fine
    check = run_iptables(["-C", "OUTPUT", "-j", CHAIN])
    if check.returncode != 0:
        run_iptables(["-I", "OUTPUT", "-j", CHAIN])


def teardown_all():
    run_iptables(["-F", CHAIN])
    check = run_iptables(["-C", "OUTPUT", "-j", CHAIN])
    if check.returncode == 0:
        run_iptables(["-D", "OUTPUT", "-j", CHAIN])
    run_iptables(["-X", CHAIN])


_COMMENT_RE = re.compile(r'--comment "SteamRouteTool:([^:"]+):([^"]*)"')


def get_blocked_set():
    """Set of (pop_name, ip) tuples currently blocked, read live from iptables."""
    result = run_iptables(["-S", CHAIN])
    blocked = set()
    if result.returncode != 0:
        return blocked
    for line in result.stdout.splitlines():
        if not line.startswith("-A "):
            continue
        m = _COMMENT_RE.search(line)
        if m:
            blocked.add((m.group(1), m.group(2)))
    return blocked


def _add_block_rules(pop_name, ip):
    comment = f"{COMMENT_PREFIX}{pop_name}:{ip}"
    run_iptables(["-A", CHAIN, "-d", ip, "-p", "udp", "--dport", PORT_RANGE,
                  "-m", "comment", "--comment", comment, "-j", "DROP"])
    run_iptables(["-A", CHAIN, "-d", ip, "-p", "tcp", "--dport", PORT_RANGE,
                  "-m", "comment", "--comment", comment, "-j", "DROP"])
    run_iptables(["-A", CHAIN, "-d", ip, "-p", "icmp",
                  "-m", "comment", "--comment", comment, "-j", "DROP"])


def block_ip(pop_name, ip):
    ensure_setup()
    unblock_ip(pop_name, ip)  # avoid duplicate rules if partially blocked already
    _add_block_rules(pop_name, ip)


def unblock_ip(pop_name, ip):
    needle = f'--comment "{COMMENT_PREFIX}{pop_name}:{ip}"'
    while True:
        result = run_iptables(["-S", CHAIN])
        if result.returncode != 0:
            return
        lines = [l for l in result.stdout.splitlines() if l.startswith("-A ")]
        idx = next((i for i, l in enumerate(lines, start=1) if needle in l), None)
        if idx is None:
            return
        run_iptables(["-D", CHAIN, str(idx)])


def block_route(route):
    for ip, _ in route.relays:
        block_ip(route.name, ip)


def unblock_route(route):
    for ip, _ in route.relays:
        unblock_ip(route.name, ip)


def block_all_routes(routes, blocked):
    """Block every relay in every route -- e.g. so you can then manually unblock
    just the pop(s) you actually want Steam to use.

    Faster than calling block_route() in a loop: it only pays for the
    'is this already (partially) blocked?' check on relays that `blocked`
    (the live-read state from get_blocked_set()) says actually need it,
    instead of re-checking every single relay via iptables.
    """
    ensure_setup()
    for route in routes:
        for ip, _ in route.relays:
            key = (route.name, ip)
            if key in blocked:
                unblock_ip(route.name, ip)  # clear the existing rule first, avoid dupes
            _add_block_rules(route.name, ip)
            blocked.add(key)


# --------------------------------------------------------------------------
# environment checks
# --------------------------------------------------------------------------

def check_environment():
    if shutil.which("iptables") is None:
        die("iptables was not found. Install it, e.g.:\n"
            "  Debian/Ubuntu: sudo apt install iptables\n"
            "  Fedora:        sudo dnf install iptables-nft\n"
            "  Arch:          sudo pacman -S iptables-nft")
    global HAVE_PING
    HAVE_PING = shutil.which("ping") is not None
    if not HAVE_PING:
        print("Note: 'ping' not found -- latency columns will be blank.", file=sys.stderr)


# --------------------------------------------------------------------------
# non-interactive output
# --------------------------------------------------------------------------

def print_list(routes, blocked):
    ping_routes(routes)
    print(f"{'pop':<10} {'location':<28} {'state':<8} {'ping':>8}  relays")
    for route in routes:
        state = route_block_state(route, blocked)
        tag = {"all": "BLOCKED", "some": "PARTIAL", "none": "open"}[state]
        vals = [v for v in route.ping.values() if v is not None]
        ping_str = f"{sum(vals) / len(vals):.0f} ms" if vals else "--"
        print(f"{route.name:<10} {route.label:<28} {tag:<8} {ping_str:>8}  {len(route.relays)}")


# --------------------------------------------------------------------------
# interactive menu
# --------------------------------------------------------------------------

def render(routes, blocked, message=None):
    clear_screen()
    print(c("SteamRouteTool", "1;36") + c("  -- Linux port (iptables backend)", "2"))
    print(c(f"chain: {CHAIN}   appid: {CURRENT_APPID}" + ("   [DRY RUN]" if DRY_RUN else ""), "2"))
    print()
    header = f"{'#':>4}  {'':3} {'Location':<32} {'Ping':>8}"
    print(c(header, "1"))
    print("-" * len(header))

    for i, route in enumerate(routes, start=1):
        state = route_block_state(route, blocked)
        box = {"all": "[x]", "some": "[~]", "none": "[ ]"}[state]
        vals = [route.ping.get(ip) for ip, _ in route.relays if route.ping.get(ip) is not None]
        if vals:
            avg = sum(vals) / len(vals)
            color = "32" if avg <= 60 else ("33" if avg <= 110 else "31")
            ping_str = c(f"{avg:.0f} ms", color)
        else:
            ping_str = c("--", "2")

        extra = f" ({len(route.relays)} relays)" if len(route.relays) > 1 else ""
        label = (route.label + extra)[:32]
        print(f"{i:>4}  {box} {label:<32} {ping_str:>8}")

        if route.expanded:
            for j, (ip, _pr) in enumerate(route.relays, start=1):
                sub_blocked = (route.name, ip) in blocked
                sub_box = "[x]" if sub_blocked else "[ ]"
                p = route.ping.get(ip)
                if p is not None:
                    color = "32" if p <= 60 else ("33" if p <= 110 else "31")
                    p_str = c(f"{p:.0f} ms", color)
                else:
                    p_str = c("--", "2")
                print(f"      {i}.{j:<3}{sub_box} {ip:<22} {p_str:>8}")

    print()
    if message:
        print(c(message, "33"))
        print()
    print(c("commands:", "1") + " <n> toggle pop   e <n> expand/collapse   <n>.<m> toggle one relay")
    print("          a block ALL   r re-ping all   c clear all rules   q quit   ? help")


def interactive(routes, blocked):
    message = None
    print("Pinging routes for the first time...", file=sys.stderr)
    ping_routes(routes)
    while True:
        render(routes, blocked, message)
        message = None
        try:
            cmd = input(c("> ", "1")).strip()
        except EOFError:
            break

        if cmd == "":
            continue
        low = cmd.lower()

        if low in ("q", "quit", "exit"):
            break

        if low in ("r", "refresh"):
            render(routes, blocked, "Re-pinging all routes...")
            ping_routes(routes)
            continue

        if low in ("c", "clear"):
            confirm = input(c("Remove ALL firewall rules created by this tool? [y/N] ", "33")).strip().lower()
            if confirm == "y":
                teardown_all()
                blocked.clear()
                message = "Cleared all SteamRouteTool rules."
            else:
                message = "Cancelled."
            continue

        if low in ("a", "all", "block-all"):
            confirm = input(c(f"Block ALL {len(routes)} pops right now? [y/N] ", "33")).strip().lower()
            if confirm == "y":
                block_all_routes(routes, blocked)
                message = (f"Blocked all {len(routes)} pops. Toggle a number below "
                           f"to open a specific pop back up.")
            else:
                message = "Cancelled."
            continue

        if low in ("?", "help"):
            message = ("<n> block/unblock whole pop | e <n> expand/collapse | "
                       "<n>.<m> toggle one relay | a block ALL | r re-ping | c clear all | q quit")
            continue

        m = re.match(r"^e\s+(\d+)$", low)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(routes):
                routes[idx].expanded = not routes[idx].expanded
            else:
                message = "No such entry."
            continue

        m = re.match(r"^(\d+)\.(\d+)$", cmd)
        if m:
            idx, sub = int(m.group(1)) - 1, int(m.group(2)) - 1
            if 0 <= idx < len(routes) and 0 <= sub < len(routes[idx].relays):
                route = routes[idx]
                ip, _ = route.relays[sub]
                key = (route.name, ip)
                if key in blocked:
                    unblock_ip(route.name, ip)
                    blocked.discard(key)
                    message = f"Unblocked {ip} ({route.label})."
                else:
                    block_ip(route.name, ip)
                    blocked.add(key)
                    message = f"Blocked {ip} ({route.label})."
            else:
                message = "No such entry."
            continue

        m = re.match(r"^(\d+)$", cmd)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(routes):
                route = routes[idx]
                state = route_block_state(route, blocked)
                if state == "all":
                    unblock_route(route)
                    for ip, _ in route.relays:
                        blocked.discard((route.name, ip))
                    message = f"Unblocked {route.label}."
                else:
                    block_route(route)
                    for ip, _ in route.relays:
                        blocked.add((route.name, ip))
                    message = f"Blocked {route.label}."
            else:
                message = "No such entry."
            continue

        message = f"Unrecognized command: {cmd!r} (try '?' for help)"


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    global DRY_RUN, CURRENT_APPID

    parser = argparse.ArgumentParser(
        description="Block/unblock Valve SDR (Steam Datagram Relay) routes on Linux via iptables.")
    parser.add_argument("--appid", type=int, default=DEFAULT_APPID,
                         help=f"Steam AppID whose SDR config to fetch (default {DEFAULT_APPID} = TF2; try 730 for CS2)")
    parser.add_argument("--list", action="store_true", help="Print routes and current block state, then exit")
    parser.add_argument("--block", metavar="POP", help="Block every relay in the named pop, then exit")
    parser.add_argument("--unblock", metavar="POP", help="Unblock every relay in the named pop, then exit")
    parser.add_argument("--block-all", action="store_true",
                         help="Block every relay in every pop, then exit (use --unblock afterwards "
                              "to selectively re-open the ones you want)")
    parser.add_argument("--clear", action="store_true", help="Remove every rule this tool has created, then exit")
    parser.add_argument("--dry-run", action="store_true", help="Print iptables commands instead of running them")
    args = parser.parse_args()

    DRY_RUN = args.dry_run
    CURRENT_APPID = args.appid

    if os.geteuid() != 0:
        die("SteamRouteTool needs root to manage firewall rules.\n"
            f"Try: sudo {sys.argv[0]}" + (" " + " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""))

    check_environment()

    if args.clear:
        ensure_setup()
        teardown_all()
        print("Cleared all SteamRouteTool firewall rules.")
        return

    print(f"Fetching SDR config for appid {args.appid}...", file=sys.stderr)
    routes = fetch_routes(args.appid)
    if not routes:
        die("No routes found in the response -- Valve may not publish an SDR config for that AppID.")

    ensure_setup()
    blocked = get_blocked_set()

    if args.block_all:
        block_all_routes(routes, blocked)
        total_relays = sum(len(r.relays) for r in routes)
        print(f"Blocked all {len(routes)} pops ({total_relays} relay(s) total).")
        print(f"Use --unblock <pop> (or --list to see names) to selectively re-open the ones you want.")
        return

    if args.block or args.unblock:
        name = args.block or args.unblock
        match = next((r for r in routes if r.name == name or r.label.lower() == name.lower()), None)
        if not match:
            die(f"No pop named {name!r}. Use --list to see valid names.")
        if args.block:
            block_route(match)
            print(f"Blocked {match.label} ({len(match.relays)} relay(s)).")
        else:
            unblock_route(match)
            print(f"Unblocked {match.label}.")
        return

    if args.list:
        print_list(routes, blocked)
        return

    try:
        interactive(routes, blocked)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
