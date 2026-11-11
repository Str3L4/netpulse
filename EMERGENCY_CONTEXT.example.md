# Reference Architecture & Emergency Network Context

> **Template Document**: Copy this file to `ANTIGRAVITY_EMERGENCY_CONTEXT.md` to document your local infrastructure parameters. Personal context files are ignored by git to protect credentials.

---

## 1. Network Topology & Roles

| Node / Device | Typical IP | Role | Key Ports & Services |
| :--- | :--- | :--- | :--- |
| **Main Workstation (PC)** | `192.168.1.100` | IDE, Dev Tools, Local Proxifier | Direct LAN, ProxyBridge |
| **Router (e.g. MikroTik)** | `192.168.1.1` | Gateway, FastTrack, Mangle Routing | SSH (22), Web (80/443), DNS (53) |
| **Linux Gateway (Pi / VM)** | `192.168.1.254` | DPI Bypass (Zapret) & VLESS Tunnel (Xray) | SSH (22), SOCKS5 (10808), HTTP (10809) |
| **Upstream WAN** | `DHCP / PPPoE` | ISP Uplink & Google Global Cache (GGC) | Standard WAN |

---

## 2. Core Routing Principles

### A. Workstation Process Rules (ProxyBridge / Tun2Socks)
- **Direct LAN Connection:** The main IDE UI and Electron shell connect directly without local system proxy.
- **Selective Language Server Routing:** Backend language engines (`language_server.exe`) route via SOCKS5 tunnel on the Linux gateway (`10808`).
- **Windows System Proxy:** Stays strictly disabled (`ProxyEnable = 0`, WinINet direct `0x01`) to avoid unintended proxy leaks.

### B. Gateway Services
- **Zapret DPI Bypass:** Operates in NFQUEUE mode (`ALT11` / Fake-TLS) to handle direct ISP censorship bypass.
- **Xray Core:** Runs VLESS + Reality over TCP (XTLS-Vision) towards offshore VPS nodes.
- **Telegram Proxy:** Lightweight MTProto or SOCKS5 relay on dedicated ports.

### C. Router Firewall & FastTrack
- **FastTrack Acceleration:** Non-marked domestic traffic is accelerated with FastTrack (`connection-mark=no-mark`).
- **Targeted Mangle:** Bypassed domains and IP address lists (`UNBLOCK-ZAPRET`) are marked and routed through the Linux gateway.
- **Dynamic SNI Capture:** TLS SNI matcher (`*.googlevideo.com`, `*youtube*`) auto-adds CDN IPs to bypass address-lists with 1-week timeout.
