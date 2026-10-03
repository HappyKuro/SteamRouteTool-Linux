# SteamRouteTool for Linux

Block bad Valve SDR (Steam Datagram Relay) routes so Steam's matchmaking picks a
different one — a Linux-native reimplementation of
[SteamRouteTool](https://github.com/HappyKuro/SteamRouteTool), originally by
[Froody](https://github.com/dfrood/SteamRouteTool).

The original is a Windows Forms app that manages rules through the Windows Firewall
COM API. Neither WinForms nor that API exist on Linux, so this isn't a recompile — it's
a from-scratch reimplementation of the same behavior on top of `iptables`, with a
terminal menu in place of the grid UI.

```
   #      Location                                Ping
------------------------------------------------------
   1  [ ] Frankfurt (Germany)                    24 ms
   2  [x] Chicago (Illinois)                    122 ms
   3  [~] Stockholm - Kista (Sweden) (2 relays)   25 ms
      3.1   [x] 146.66.157.1                     25 ms
      3.2   [ ] 146.66.157.2                     26 ms

commands: <n> toggle   b/u <n> block/unblock   o <n> only allow   e <n> expand
          a block ALL   c clear all   s sort   r re-ping   q quit   ? help
```

## Features

- Fetches the same live config the original does: `GetSDRConfig` for any Steam AppID
  (defaults to TF2's `440`; CS2 is `730`)
- Shows ping to every relay
- Block or unblock a whole pop (data center), or one specific relay within it
- **Block everything, then selectively unblock** just the pop(s) you want Steam to use
  — useful for forcing your game onto a specific data center. `--only fra` (or `o <n>`
  in the menu) does both steps at once
- Select several entries at once: `3 7 12`, ranges like `4-9`, single relays like `2.1`
- Sort by name or by ping, so the closest data centers are at the top
- Every change is applied as **one atomic `iptables-restore` batch**: blocking all ~160
  relays takes a fraction of a second, and if anything fails, nothing is half-applied
  and you get the actual error instead of a false "Blocked"
- Blocked/unblocked state is always read live from `iptables`, never cached — so it
  can't show you a stale picture even if something else touches the firewall
- All rules live in their own `iptables` chain (`STEAMROUTETOOL`), so this never
  touches your other firewall rules, `ufw` config, etc.
- `--dry-run` mode to preview exactly what would change before it does
- Zero dependencies beyond Python 3 + `iptables`

## Requirements

- Linux with `iptables` and `iptables-restore` (same package; present by default on
  virtually every distro — including ones that run nftables under the hood via the
  `iptables-nft` compatibility shim: Ubuntu, Fedora, Arch, SteamOS, etc.)
- Python 3.8+
- `ping` (optional, only used for the latency column)
- Root, to create/modify firewall rules — same reason the original needs an
  Administrator prompt on Windows

## Installation

```bash
git clone https://github.com/HappyKuro/SteamRouteTool-Linux.git
cd SteamRouteTool-Linux
chmod +x steamroutetool.py
```

No build step, no `pip install` — it's a single self-contained script using only the
Python standard library. (If you downloaded it as a zip and get `Permission denied`,
run `chmod +x steamroutetool.py` once, or use `sudo python3 steamroutetool.py`.)

## Usage

### Interactive menu

```bash
sudo ./steamroutetool.py
```

`<sel>` is one or more entries: `3`, `3 7 12`, `4-9`, or `2.1` for relay 1 of pop 2
(numbers refer to the list as currently shown).

| Command | Action |
|---|---|
| `<sel>` | Toggle: block if open/partial, unblock if fully blocked |
| `b <sel>` / `u <sel>` | Block / unblock, regardless of current state |
| `o <sel>` | **Only** allow these — block every other pop |
| `e [sel]` | Expand/collapse to see individual relay IPs (no selection = all) |
| `a` | Block **every** pop at once (asks to confirm) |
| `c` | Remove every rule this tool created (asks to confirm) |
| `s` | Toggle sorting by name / by ping |
| `r` | Re-ping everything |
| `?` | Show the command list |
| `q` | Quit |

`[x]` = fully blocked, `[ ]` = open, `[~]` = partially blocked (some relays in that
pop only).

### Command-line flags

```bash
sudo ./steamroutetool.py --list                # print routes + current state, exit
sudo ./steamroutetool.py --list --sort ping    # ...closest data centers first
sudo ./steamroutetool.py --block fra ams       # block every relay in pops "fra" and "ams"
sudo ./steamroutetool.py --unblock fra         # unblock it
sudo ./steamroutetool.py --only fra            # block every pop EXCEPT "fra"
sudo ./steamroutetool.py --block-all           # block every pop in one shot
sudo ./steamroutetool.py --clear               # remove every rule this tool made
sudo ./steamroutetool.py --appid 730           # use CS2's SDR config instead of TF2's
sudo ./steamroutetool.py --dry-run --block fra # preview the iptables changes, don't apply them
```

### Forcing Steam onto specific data center(s)

Block everything except the pop(s) you want your game to actually use:

```bash
sudo ./steamroutetool.py --only fra ams
```

That's the same as `--block-all` followed by `--unblock fra ams`, but done in one
atomic step. In the interactive menu: `o 3 5` (or `a`, then `u <n>`). Pop names are
case-insensitive and you can also use the location shown by `--list`.

## How it works

- Fetches `https://api.steampowered.com/ISteamApps/GetSDRConfig/v1?appid=<id>`, the
  same endpoint the original tool uses, and parses out each pop's relay IPs.
- Blocking a relay adds three rules to a dedicated `STEAMROUTETOOL` chain (hooked into
  `OUTPUT`): drop UDP and TCP on `27015:27202` (the same port range the original
  hardcodes) plus ICMP, scoped to that relay's IP and tagged with an iptables comment
  so the rule can be found and removed again later.
- Every change (block, unblock, "only", block-all) is computed against the chain's
  current contents and handed to `iptables-restore --noflush` as a single batch, so it
  is applied atomically. All iptables calls use `-w`, so they wait for the xtables
  lock instead of failing if Docker, `ufw`, etc. happen to be touching the firewall.
- "Is this blocked?" is never stored separately — every read (`--list`, the menu,
  `[x]`/`[ ]` state) comes from parsing `iptables -S STEAMROUTETOOL` live.

## Notes

- IPv4 only, matching the original (Valve's SDR only publishes IPv4 relays).
- If you also run `ufw` or `firewalld`, this tool doesn't touch their rules — it only
  adds its own chain and a single jump rule from `OUTPUT`. Some firewall managers do
  flush custom chains on reload; if that happens, `--list` will correctly show
  everything as unblocked again rather than lying about stale state — just re-block
  what you need.
- Rules are not persistent: they're gone after a reboot (or a firewall reload). That's
  usually what you want for a matchmaking tweak — just re-run the tool when needed.
- `--dry-run` still performs the read-only checks for real, so its output reflects
  your actual current state; only the rule-adding/removing commands are skipped.

## License

GPLv3, matching the original project. See [LICENSE](LICENSE).

## Credits

- Original design and Windows tool: [Froody](https://github.com/dfrood)
  ([dfrood/SteamRouteTool](https://github.com/dfrood/SteamRouteTool))
- Windows fork and this Linux port: [HappyKuro](https://github.com/HappyKuro)
