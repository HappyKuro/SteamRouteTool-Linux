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
  * Applies every change as one atomic iptables-restore batch: either all
    of it lands or none of it does, and failures are reported, not hidden.
  * Derives "is this blocked?" live from iptables itself rather than
    keeping separate state, so it can never disagree with reality.

Requirements: Python 3.8+, iptables (incl. iptables-restore), and
(optionally) ping. Must run as root, for the same reason the original
needs an Administrator prompt.

Usage:
  sudo ./steamroutetool.py                    interactive menu
  sudo ./steamroutetool.py --list             print routes + current state, exit
  sudo ./steamroutetool.py --block fra ams    block every relay in those pops
  sudo ./steamroutetool.py --unblock fra      unblock every relay in pop "fra"
  sudo ./steamroutetool.py --only fra         block every pop EXCEPT "fra"
  sudo ./steamroutetool.py --block-all        block every pop
  sudo ./steamroutetool.py --clear            remove every rule this tool made
  sudo ./steamroutetool.py --appid 730        use CS2's SDR config instead of TF2's
  sudo ./steamroutetool.py --dry-run ...      print the changes, don't apply them

Credits: original tool by Froody (dfrood/SteamRouteTool), fork by HappyKuro.
"""

import argparse
import ipaddress
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

__version__ = "1.1"

CHAIN = "STEAMROUTETOOL"
COMMENT_PREFIX = "SteamRouteTool:"
DEFAULT_APPID = 440  # Team Fortress 2. Try 730 for CS2.
CONFIG_URL = "https://api.steampowered.com/ISteamApps/GetSDRConfig/v1?appid={appid}"
PORT_RANGE = "27015:27202"  # matches the original tool's fixed RemotePorts range
PING_TIMEOUT = 1  # seconds
PING_GOOD, PING_OK = 60, 110  # ms thresholds for green / yellow / red

HAVE_PING = True

_POP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class FirewallError(Exception):
    pass


# --------------------------------------------------------------------------
# terminal helpers
# --------------------------------------------------------------------------

def supports_color():
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def c(text, code):
    return f"\033[{code}m{text}\033[0m" if supports_color() else text


def clear_screen():
    if supports_color():
        sys.stdout.write("\033[H\033[J")


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def ask(prompt):
    """input() that treats Ctrl+D / Ctrl+C as 'no' instead of crashing."""
    try:
        return input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def fmt_ping(ms):
    if ms is None:
        return c("--", "2")
    color = "32" if ms <= PING_GOOD else ("33" if ms <= PING_OK else "31")
    return c(f"{ms:.0f} ms", color)


def pad(text, width):
    """Right-align `text` to `width`, ignoring ANSI color codes."""
    visible = len(re.sub(r"\033\[[0-9;]*m", "", text))
    return " " * max(0, width - visible) + text


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

    @property
    def keys(self):
        return {(self.name, ip) for ip, _ in self.relays}

    def avg_ping(self):
        vals = [self.ping.get(ip) for ip, _ in self.relays]
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None


def route_block_state(route, blocked):
    """Returns 'all', 'some', or 'none' for how much of this pop is blocked."""
    count = len(route.keys & blocked)
    if count == 0:
        return "none"
    if count == len(route.relays):
        return "all"
    return "some"


def sort_routes(routes, by):
    if by == "ping":
        # unreachable / unpinged pops sink to the bottom
        return sorted(routes, key=lambda r: (r.avg_ping() is None, r.avg_ping() or 0,
                                             r.label.lower()))
    return sorted(routes, key=lambda r: r.label.lower())


def find_routes(routes, names):
    """Resolve pop names (or location labels), case-insensitively."""
    found, missing = [], []
    for name in names:
        low = name.lower()
        match = next((r for r in routes if r.name.lower() == low or r.label.lower() == low), None)
        if match is None:
            missing.append(name)
        elif match not in found:
            found.append(match)
    return found, missing


# --------------------------------------------------------------------------
# fetching + parsing Valve's SDR config
# --------------------------------------------------------------------------

def parse_routes(data):
    pops = data.get("pops") if isinstance(data, dict) else None
    if not isinstance(pops, dict):
        raise ValueError("Unexpected response shape from GetSDRConfig (no 'pops' object) -- "
                         "Valve may have changed the API.")

    routes = []
    for name, value in pops.items():
        if not isinstance(value, dict) or "relays" not in value:
            continue
        # mirrors the original's `rc.Value.ToString().Contains("cloud-test")` check
        if "cloud-test" in json.dumps(value):
            continue
        # the pop name ends up inside an iptables comment -- keep it boring
        if not _POP_NAME_RE.match(name):
            continue

        route = Route(name, desc=value.get("desc"))
        seen = set()
        for relay in value.get("relays") or []:
            if not isinstance(relay, dict):
                continue
            ip = relay.get("ipv4")
            try:
                ip = str(ipaddress.IPv4Address(ip))
            except (ipaddress.AddressValueError, ValueError, TypeError):
                continue
            if ip in seen:
                continue
            seen.add(ip)
            route.relays.append((ip, relay.get("port_range", "")))

        if route.relays:
            routes.append(route)

    return sort_routes(routes, "name")


def fetch_routes(appid):
    url = CONFIG_URL.format(appid=appid)
    req = urllib.request.Request(url, headers={"User-Agent": f"SteamRouteTool-Linux/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
    except (urllib.error.URLError, OSError) as e:
        die(f"Couldn't reach Steam's SDR config endpoint: {e}")

    try:
        return parse_routes(json.loads(raw))
    except json.JSONDecodeError as e:
        die(f"Steam returned something that wasn't valid JSON: {e}")
    except ValueError as e:
        die(str(e))


# --------------------------------------------------------------------------
# pinging
# --------------------------------------------------------------------------

def ping_ip(ip):
    if not HAVE_PING:
        return None
    try:
        result = subprocess.run(
            ["ping", "-n", "-c", "1", "-W", str(PING_TIMEOUT), ip],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=PING_TIMEOUT + 2,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    m = re.search(r"time[=<]\s*([\d.]+)", result.stdout)
    return float(m.group(1)) if m else None


def ping_routes(routes):
    """Ping every relay in `routes`, in parallel."""
    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = {}
        for route in routes:
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

_COMMENT_RE = re.compile(r'--comment "?SteamRouteTool:([^:"\s]+):([^"\s]*)"?')


def comment_for(pop_name, ip):
    return f"{COMMENT_PREFIX}{pop_name}:{ip}"


def block_rules(pop_name, ip):
    """The iptables rule specs (minus the -A/-D CHAIN prefix) that block one relay."""
    # pop names and IPs are validated in parse_routes(), so plain quoting is safe
    tail = f'-m comment --comment "{comment_for(pop_name, ip)}" -j DROP'
    return [
        f"-d {ip} -p udp --dport {PORT_RANGE} {tail}",
        f"-d {ip} -p tcp --dport {PORT_RANGE} {tail}",
        f"-d {ip} -p icmp {tail}",
    ]


def parse_rules(listing):
    """Map (pop_name, ip) -> list of `iptables -S` rule lines for that relay."""
    rules = {}
    for line in listing.splitlines():
        if not line.startswith(f"-A {CHAIN} "):
            continue
        m = _COMMENT_RE.search(line)
        if m:
            rules.setdefault((m.group(1), m.group(2)), []).append(line)
    return rules


def build_restore_script(current_rules, block, unblock):
    """iptables-restore input that unblocks `unblock` and (re)blocks `block`.

    Existing rules for anything in `block` are deleted first too, so
    re-blocking a partially blocked relay never leaves duplicate rules.
    Returns None if there is nothing to do.
    """
    lines = []
    for key in sorted((block | unblock) & current_rules.keys()):
        for rule in current_rules[key]:
            lines.append("-D" + rule[2:])  # "-A CHAIN ..." -> "-D CHAIN ..."
    for pop_name, ip in sorted(block):
        for spec in block_rules(pop_name, ip):
            lines.append(f"-A {CHAIN} {spec}")
    if not lines:
        return None
    return "*filter\n" + "\n".join(lines) + "\nCOMMIT\n"


class Firewall:
    """Thin wrapper around iptables. In dry-run mode, read-only queries still
    hit the real firewall but changes are printed and simulated instead."""

    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self._sim = None  # simulated blocked set, dry-run only

    # -- low level ---------------------------------------------------------

    def _run(self, args, stdin=None, cmd="iptables"):
        return subprocess.run([cmd, "-w"] + args, input=stdin, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)

    def _mutate(self, args):
        if self.dry_run:
            print(c("[dry-run]", "2"), "iptables " + " ".join(args))
            return
        result = self._run(args)
        if result.returncode != 0:
            raise FirewallError(f"iptables {' '.join(args)} failed: "
                                f"{result.stderr.strip() or 'exit code ' + str(result.returncode)}")

    def _chain_exists(self):
        return self._run(["-S", CHAIN]).returncode == 0

    def _jump_exists(self):
        return self._run(["-C", "OUTPUT", "-j", CHAIN]).returncode == 0

    def _listing(self):
        result = self._run(["-S", CHAIN])
        return result.stdout if result.returncode == 0 else ""

    # -- public API ----------------------------------------------------------

    def ensure_setup(self):
        if not self._chain_exists():
            self._mutate(["-N", CHAIN])
        if not self._jump_exists():
            self._mutate(["-I", "OUTPUT", "-j", CHAIN])

    def blocked(self):
        """Set of (pop_name, ip) tuples currently blocked."""
        if self._sim is not None:
            return set(self._sim)
        return set(parse_rules(self._listing()))

    def apply(self, block=frozenset(), unblock=frozenset()):
        """Atomically block and/or unblock sets of (pop_name, ip) keys."""
        block, unblock = set(block), set(unblock) - set(block)
        if not block and not unblock:
            return
        if block:
            self.ensure_setup()
        script = build_restore_script(parse_rules(self._listing()), block, unblock)
        if script is None:
            return

        if self.dry_run:
            print(c("[dry-run] iptables-restore --noflush <<EOF", "2"))
            print(script.rstrip())
            print(c("EOF", "2"))
            self._sim = (self.blocked() - unblock) | block
            return

        result = self._run(["--noflush"], stdin=script, cmd="iptables-restore")
        if result.returncode != 0:
            raise FirewallError("iptables-restore rejected the change (nothing was applied): "
                                + (result.stderr.strip() or f"exit code {result.returncode}"))

    def clear(self):
        """Remove the chain, its rules and the OUTPUT jump."""
        if self._jump_exists():
            self._mutate(["-D", "OUTPUT", "-j", CHAIN])
        if self._chain_exists():
            self._mutate(["-F", CHAIN])
            self._mutate(["-X", CHAIN])
        if self.dry_run:
            self._sim = set()


# --------------------------------------------------------------------------
# environment checks
# --------------------------------------------------------------------------

def check_environment():
    for tool in ("iptables", "iptables-restore"):
        if shutil.which(tool) is None:
            die(f"{tool} was not found. Install iptables, e.g.:\n"
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

def print_list(routes, blocked, sort="name"):
    ping_routes(routes)
    width = max([len("location")] + [len(r.label) for r in routes])
    print(f"{'pop':<8} {'location':<{width}} {'state':<8} {'ping':>7}  relays")
    for route in sort_routes(routes, sort):
        tag = {"all": "BLOCKED", "some": "PARTIAL", "none": "open"}[route_block_state(route, blocked)]
        avg = route.avg_ping()
        ping_str = f"{avg:.0f} ms" if avg is not None else "--"
        print(f"{route.name:<8} {route.label:<{width}} {tag:<8} {ping_str:>7}  {len(route.relays)}")


# --------------------------------------------------------------------------
# interactive menu
# --------------------------------------------------------------------------

HELP_TEXT = """\
<sel> is one or more entries, e.g.  3   3 7 12   4-9   2.1 (pop 2, relay 1)

  <sel>     toggle: block if open/partial, unblock if fully blocked
  b <sel>   block              u <sel>   unblock
  o <sel>   ONLY allow these -- block every other pop
  e [sel]   expand/collapse to show individual relays (no sel = all)
  a         block ALL pops     c         clear all rules (unblock everything)
  s         sort by name/ping  r         re-ping everything
  q         quit"""

FOOTER = ("commands: <n> toggle   b/u <n> block/unblock   o <n> only allow   e <n> expand\n"
          "          a block ALL   c clear all   s sort   r re-ping   q quit   ? help")


def parse_selection(text, view):
    """Parse '3 7 4-6 2.1' against the displayed `view` into
    (list[Route], list[(Route, ip)]). Raises ValueError on bad input."""
    pops, relays = [], []
    tokens = [t for t in re.split(r"[\s,]+", text.strip()) if t]
    if not tokens:
        raise ValueError("Nothing selected.")
    for tok in tokens:
        m = re.fullmatch(r"(\d+)\.(\d+)", tok)
        if m:
            i, j = int(m.group(1)), int(m.group(2))
            if not (1 <= i <= len(view) and 1 <= j <= len(view[i - 1].relays)):
                raise ValueError(f"No such entry: {tok}")
            relays.append((view[i - 1], view[i - 1].relays[j - 1][0]))
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", tok)
        if not m:
            raise ValueError(f"Not an entry number: {tok!r}")
        lo = int(m.group(1))
        hi = int(m.group(2) or lo)
        if lo > hi:
            lo, hi = hi, lo
        if lo < 1 or hi > len(view):
            raise ValueError(f"No such entry: {tok} (valid: 1-{len(view)})")
        for i in range(lo, hi + 1):
            if view[i - 1] not in pops:
                pops.append(view[i - 1])
    return pops, relays


def describe(items):
    names = [i if isinstance(i, str) else i.label for i in items]
    if len(names) > 3:
        return f"{', '.join(names[:3])} and {len(names) - 3} more"
    return ", ".join(names)


def render(view, blocked, ctx, message=None):
    clear_screen()
    n_blocked = sum(1 for r in view if route_block_state(r, blocked) == "all")
    print(c("SteamRouteTool", "1;36") + c(f"  -- Linux port v{__version__} (iptables backend)", "2"))
    print(c(f"appid: {ctx['appid']}   pops: {len(view)} ({n_blocked} blocked)   "
            f"sorted by: {ctx['sort']}" + ("   [DRY RUN]" if ctx["dry_run"] else ""), "2"))
    print()
    header = f"{'#':>4}  {'':3} {'Location':<36} {'Ping':>7}"
    print(c(header, "1"))
    print("-" * len(header))

    for i, route in enumerate(view, start=1):
        state = route_block_state(route, blocked)
        box = {"all": c("[x]", "31"), "some": c("[~]", "33"), "none": "[ ]"}[state]
        extra = f" ({len(route.relays)} relays)" if len(route.relays) > 1 else ""
        label = (route.label + extra)[:36]
        print(f"{i:>4}  {box} {label:<36} {pad(fmt_ping(route.avg_ping()), 7)}")

        if route.expanded:
            for j, (ip, _pr) in enumerate(route.relays, start=1):
                sub_box = c("[x]", "31") if (route.name, ip) in blocked else "[ ]"
                num = f"{i}.{j}"
                print(f"      {num:<6}{sub_box} {ip:<30} {pad(fmt_ping(route.ping.get(ip)), 7)}")

    print()
    if message:
        print(c(message, "33"))
        print()
    print(c(FOOTER, "2"))


def interactive(routes, fw, ctx):
    message = None
    print("Pinging routes for the first time...", file=sys.stderr)
    ping_routes(routes)
    while True:
        blocked = fw.blocked()  # always re-read: never trust a cached copy
        view = sort_routes(routes, ctx["sort"])
        render(view, blocked, ctx, message)
        message = None
        try:
            cmd = input(c("> ", "1")).strip()
        except EOFError:
            break
        if not cmd:
            continue

        verb, _, rest = cmd.partition(" ")
        verb = verb.lower()
        if re.match(r"^\d", verb):  # bare selection -> toggle
            verb, rest = "toggle", cmd

        try:
            if verb in ("q", "quit", "exit"):
                break

            elif verb in ("?", "h", "help"):
                message = HELP_TEXT

            elif verb in ("r", "refresh"):
                render(view, blocked, ctx, "Re-pinging all routes...")
                ping_routes(routes)

            elif verb in ("s", "sort"):
                ctx["sort"] = "ping" if ctx["sort"] == "name" else "name"
                message = f"Sorted by {ctx['sort']}."

            elif verb in ("c", "clear"):
                if ask(c("Remove ALL firewall rules created by this tool? [y/N] ", "33")) == "y":
                    fw.clear()
                    message = "Cleared all SteamRouteTool rules."
                else:
                    message = "Cancelled."

            elif verb in ("a", "all"):
                if ask(c(f"Block ALL {len(routes)} pops right now? [y/N] ", "33")) == "y":
                    fw.apply(block=set().union(*(r.keys for r in routes)))
                    message = (f"Blocked all {len(routes)} pops. Use 'u <n>' to open "
                               f"a specific pop back up.")
                else:
                    message = "Cancelled."

            elif verb in ("e", "expand"):
                if rest.strip():
                    pops, relays = parse_selection(rest, view)
                    for route in pops + [r for r, _ in relays]:
                        route.expanded = not route.expanded
                else:
                    expand = not any(r.expanded for r in routes)
                    for route in routes:
                        route.expanded = expand

            elif verb in ("toggle", "b", "block", "u", "unblock"):
                pops, relays = parse_selection(rest, view)
                block, unblock = set(), set()
                for route in pops:
                    if verb in ("b", "block"):
                        want_block = True
                    elif verb in ("u", "unblock"):
                        want_block = False
                    else:
                        want_block = route_block_state(route, blocked) != "all"
                    (block if want_block else unblock).update(route.keys)
                for route, ip in relays:
                    key = (route.name, ip)
                    if verb in ("b", "block"):
                        want_block = True
                    elif verb in ("u", "unblock"):
                        want_block = False
                    else:
                        want_block = key not in blocked
                    (block if want_block else unblock).add(key)
                fw.apply(block=block, unblock=unblock)
                parts = []
                if block:
                    parts.append(f"Blocked {describe(_targets(pops, relays, block))}.")
                if unblock:
                    parts.append(f"Unblocked {describe(_targets(pops, relays, unblock))}.")
                message = " ".join(parts) or "Nothing to change."

            elif verb in ("o", "only"):
                pops, relays = parse_selection(rest, view)
                keep = set().union(*(r.keys for r in pops)) | {(r.name, ip) for r, ip in relays}
                everything = set().union(*(r.keys for r in routes))
                fw.apply(block=everything - keep, unblock=keep)
                message = f"Only allowing {describe(_targets(pops, relays, keep))}; everything else blocked."

            else:
                message = f"Unrecognized command: {cmd!r} (try '?' for help)"

        except ValueError as e:
            message = str(e)
        except FirewallError as e:
            message = c(f"Error: {e}", "31")


def _targets(pops, relays, keys):
    """Human-readable names for the selected pops/relays whose keys are in `keys`."""
    out = [r for r in pops if r.keys & keys]
    out += [f"{ip} ({r.label})" for r, ip in relays if (r.name, ip) in keys]
    return out


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description="Block/unblock Valve SDR (Steam Datagram Relay) routes on Linux via iptables.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--appid", type=int, default=DEFAULT_APPID,
                        help=f"Steam AppID whose SDR config to fetch (default {DEFAULT_APPID} = TF2; "
                             "try 730 for CS2)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the iptables changes instead of applying them")
    parser.add_argument("--sort", choices=("name", "ping"), default="name",
                        help="Sort order for --list and the menu (default: name)")

    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--list", action="store_true", help="Print routes and current block state, then exit")
    actions.add_argument("--block", metavar="POP", nargs="+", help="Block every relay in the named pop(s)")
    actions.add_argument("--unblock", metavar="POP", nargs="+", help="Unblock every relay in the named pop(s)")
    actions.add_argument("--only", metavar="POP", nargs="+",
                         help="Block every pop EXCEPT the named one(s) -- forces Steam onto them")
    actions.add_argument("--block-all", action="store_true",
                         help="Block every relay in every pop (use --unblock afterwards "
                              "to selectively re-open the ones you want)")
    actions.add_argument("--clear", action="store_true", help="Remove every rule this tool has created")
    return parser


def run(args):
    if os.geteuid() != 0:
        die("SteamRouteTool needs root to manage firewall rules.\n"
            f"Try: sudo {' '.join(shlex.quote(a) for a in sys.argv)}")

    check_environment()
    fw = Firewall(dry_run=args.dry_run)

    if args.clear:
        fw.clear()
        print("Cleared all SteamRouteTool firewall rules.")
        return

    print(f"Fetching SDR config for appid {args.appid}...", file=sys.stderr)
    routes = fetch_routes(args.appid)
    if not routes:
        die("No routes found in the response -- Valve may not publish an SDR config for that AppID.")

    if args.list:
        print_list(routes, fw.blocked(), sort=args.sort)
        return

    if args.block_all:
        fw.apply(block=set().union(*(r.keys for r in routes)))
        total_relays = sum(len(r.relays) for r in routes)
        print(f"Blocked all {len(routes)} pops ({total_relays} relay(s) total).")
        print("Use --unblock <pop> (or --list to see names) to selectively re-open the ones you want.")
        return

    names = args.block or args.unblock or args.only
    if names:
        matches, missing = find_routes(routes, names)
        if missing:
            die(f"No pop named {describe(missing)}. Use --list to see valid names.")
        keys = set().union(*(r.keys for r in matches))
        if args.block:
            fw.apply(block=keys)
            print(f"Blocked {describe(matches)} ({len(keys)} relay(s)).")
        elif args.unblock:
            fw.apply(unblock=keys)
            print(f"Unblocked {describe(matches)}.")
        else:
            everything = set().union(*(r.keys for r in routes))
            fw.apply(block=everything - keys, unblock=keys)
            print(f"Only allowing {describe(matches)}; blocked the other {len(routes) - len(matches)} pop(s).")
        return

    interactive(routes, fw, {"appid": args.appid, "sort": args.sort, "dry_run": args.dry_run})


def main():
    args = build_parser().parse_args()
    try:
        run(args)
    except FirewallError as e:
        die(f"Error: {e}")
    except KeyboardInterrupt:
        print()
        sys.exit(130)
    except BrokenPipeError:  # e.g. `--list | head`
        sys.stderr.close()
        os._exit(0)


if __name__ == "__main__":
    main()
