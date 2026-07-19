# SteamRouteTool (Linux port)

A Linux-native reimplementation of [HappyKuro/SteamRouteTool](https://github.com/HappyKuro/SteamRouteTool)
(originally by [Froody / dfrood](https://github.com/dfrood/SteamRouteTool)) — a tool for
blocking specific Valve SDR (Steam Datagram Relay) routes so Steam's matchmaking picks
a different one, instead of routing you through a relay with bad latency or packet loss.

## Why this isn't just a recompile

The original is a Windows Forms app that manages Windows Firewall rules through the
`NetFwTypeLib` COM API, and its manifest requests UAC elevation to do so. Neither
WinForms nor the Windows Firewall COM API exist on Linux, so there's nothing to port
directly — this is a from-scratch reimplementation of the same *behavior* using
Linux's `iptables` instead:

| Original (Windows) | This port (Linux) |
|---|---|
| WinForms grid UI | Terminal menu |
| Windows Firewall (`NetFwTypeLib` / `HNetCfg.FwPolicy2`) | A dedicated `iptables` chain (`STEAMROUTETOOL`) |
| Runs elevated via UAC manifest | Run with `sudo` |
| Rules named `SteamRouteTool-TCP-<pop>` etc. | Rules tagged with an iptables comment `SteamRouteTool:<pop>:<ip>` |

It fetches the same endpoint the original does — Valve's
`https://api.steampowered.com/ISteamApps/GetSDRConfig/v1?appid=<id>` — and blocks the
same UDP/TCP port range (27015–27202) plus ICMP, per relay IP.

Blocked/unblocked state is never stored separately — it's always read live from
`iptables` — so the tool can't show you a stale or incorrect picture of what's actually
blocked, even if something else (a `ufw`/`firewalld` reload, a reboot, etc.) clears the
rules out from under it.

## Requirements

- Python 3.8+
- `iptables` (present on virtually all distros, including ones that use nftables under
  the hood via the `iptables-nft` compatibility layer — Ubuntu, Fedora, Arch, SteamOS, etc.)
- `ping` (optional — only used for the latency column)
- Root, to create/modify firewall rules

## Usage

```bash
chmod +x steamroutetool.py

# interactive menu
sudo python3 steamroutetool.py

# non-interactive
sudo python3 steamroutetool.py --list                # show routes + current state
sudo python3 steamroutetool.py --block fra           # block every relay in pop "fra"
sudo python3 steamroutetool.py --unblock fra         # unblock it
sudo python3 steamroutetool.py --clear               # remove every rule this tool made
sudo python3 steamroutetool.py --appid 730           # use CS2's SDR config instead of TF2's (440)
sudo python3 steamroutetool.py --dry-run --block fra # preview the iptables commands, don't run them
```

### Interactive menu

```
   #      Location                             Ping
---------------------------------------------------
   1  [ ] Frankfurt (3 relays)                  38 ms
   2  [x] London                               112 ms
   3  [ ] sto2 (2 relays)                        --

commands: <n> toggle pop   e <n> expand/collapse   <n>.<m> toggle one relay
          r re-ping all   c clear all rules   q quit   ? help
```

- `<n>` — block/unblock every relay in that pop
- `e <n>` — expand a pop to see (and toggle) its individual relay IPs
- `<n>.<m>` — toggle just one relay within an expanded pop
- `[x]` fully blocked, `[ ]` open, `[~]` partially blocked (some relays in that pop only)
- `r` — re-ping everything · `c` — wipe all rules this tool created · `q` — quit

## Notes

- Only IPv4 is used, matching the original (Valve's SDR only publishes IPv4 relays).
- If you also run `ufw` or `firewalld`, this tool doesn't touch their rules or chains —
  it only adds its own chain and a single jump from `OUTPUT` into it. Some firewall
  managers do flush custom chains on reload/restart, though; if that happens, `--list`
  will correctly show everything as unblocked again (nothing to clean up, nothing lying
  to you) — just re-block what you need.
- `--dry-run` still performs read-only checks (`-S`/`-C`) for real, so what it prints
  reflects your actual current state — only the rule-adding/removing commands are
  skipped.

## Credits

- Original tool and design: [Froody](https://github.com/dfrood) ([dfrood/SteamRouteTool](https://github.com/dfrood/SteamRouteTool))
- Fork: [HappyKuro](https://github.com/HappyKuro/SteamRouteTool)
- This Linux port: independent reimplementation of the same behavior on `iptables`.
