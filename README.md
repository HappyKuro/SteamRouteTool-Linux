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
   #      Location                             Ping
---------------------------------------------------
   1  [ ] Frankfurt (Germany)                   24 ms
   2  [x] Chicago (Illinois)                   122 ms
   3  [~] Stockholm - Kista (Sweden)             25 ms

commands: <n> toggle pop   e <n> expand/collapse   <n>.<m> toggle one relay
          a block ALL   r re-ping all   c clear all rules   q quit   ? help
```

## Features

- Fetches the same live config the original does: `GetSDRConfig` for any Steam AppID
  (defaults to TF2's `440`; CS2 is `730`)
- Shows ping to every relay
- Block or unblock a whole pop (data center), or one specific relay within it
- **Block everything, then selectively unblock** just the pop(s) you want Steam to use
  — useful for forcing your game onto a specific data center
- Blocked/unblocked state is always read live from `iptables`, never cached — so it
  can't show you a stale picture even if something else touches the firewall
- All rules live in their own `iptables` chain (`STEAMROUTETOOL`), so this never
  touches your other firewall rules, `ufw` config, etc.
- `--dry-run` mode to preview exactly what would change before it does
- Zero dependencies beyond Python 3 + `iptables`

## Requirements

- Linux with `iptables` (present by default on virtually every distro — including
  ones that run nftables under the hood via the `iptables-nft` compatibility shim:
  Ubuntu, Fedora, Arch, SteamOS, etc.)
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
Python standard library.

> **`Permission denied` when running `./steamroutetool.py`?** The executable bit
> sometimes doesn't survive a download/upload. Run `chmod +x steamroutetool.py` once
> — if you're the repo maintainer, commit that change (`git add` + commit after the
> `chmod`) so future clones already have it set.

## Usage

### Interactive menu

```bash
sudo ./steamroutetool.py
```

| Command | Action |
|---|---|
| `<n>` | Block/unblock every relay in pop `n` |
| `e <n>` | Expand pop `n` to see its individual relay IPs |
| `<n>.<m>` | Toggle just relay `m` within pop `n` (only once expanded) |
| `a` | Block **every** pop at once (asks to confirm) |
| `r` | Re-ping everything |
| `c` | Remove every rule this tool created (asks to confirm) |
| `?` | Show the command list |
| `q` | Quit |

`[x]` = fully blocked, `[ ]` = open, `[~]` = partially blocked (some relays in that
pop only).

### Command-line flags

```bash
sudo ./steamroutetool.py --list                # print routes + current state, exit
sudo ./steamroutetool.py --block fra           # block every relay in pop "fra"
sudo ./steamroutetool.py --unblock fra         # unblock it
sudo ./steamroutetool.py --block-all           # block every pop in one shot
sudo ./steamroutetool.py --clear               # remove every rule this tool made
sudo ./steamroutetool.py --appid 730           # use CS2's SDR config instead of TF2's
sudo ./steamroutetool.py --dry-run --block fra # preview the iptables commands, don't run them
```

### Forcing Steam onto specific data center(s)

Block everything, then re-open just the pop(s) you want your game to actually use:

```bash
sudo ./steamroutetool.py --block-all
sudo ./steamroutetool.py --unblock fra
```

or in the interactive menu: `a` to block everything, then type a pop's number (or
`<n>.<m>` for a single relay) to open it back up.

## How it works

- Fetches `https://api.steampowered.com/ISteamApps/GetSDRConfig/v1?appid=<id>`, the
  same endpoint the original tool uses, and parses out each pop's relay IPs.
- Blocking a relay adds three rules to a dedicated `STEAMROUTETOOL` chain (hooked into
  `OUTPUT`): drop UDP and TCP on `27015:27202` (the same port range the original
  hardcodes) plus ICMP, scoped to that relay's IP and tagged with an iptables comment
  so the rule can be found and removed again later.
- "Is this blocked?" is never stored separately — every read (`--list`, the menu,
  `[x]`/`[ ]` state) comes from parsing `iptables -S STEAMROUTETOOL` live.

## Notes

- IPv4 only, matching the original (Valve's SDR only publishes IPv4 relays).
- If you also run `ufw` or `firewalld`, this tool doesn't touch their rules — it only
  adds its own chain and a single jump rule from `OUTPUT`. Some firewall managers do
  flush custom chains on reload; if that happens, `--list` will correctly show
  everything as unblocked again rather than lying about stale state — just re-block
  what you need.
- `--dry-run` still performs the read-only checks for real, so its output reflects
  your actual current state; only the rule-adding/removing commands are skipped.

## License

GPLv3, matching the original project. See [LICENSE](LICENSE).

## Credits

- Original design and Windows tool: [Froody](https://github.com/dfrood)
  ([dfrood/SteamRouteTool](https://github.com/dfrood/SteamRouteTool))
- Windows fork: [HappyKuro/SteamRouteTool](https://github.com/HappyKuro/SteamRouteTool)
- This Linux port: [HappyKuro](https://github.com/HappyKuro)
  ([HappyKuro/SteamRouteTool-Linux](https://github.com/HappyKuro/SteamRouteTool-Linux))
