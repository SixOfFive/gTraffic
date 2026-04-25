# gTraffic

Console-only, [gping](https://github.com/orf/gping)-style live grapher for your
home gateway. Polls a UPnP/IGD router once per second for **upload** and
**download** byte counters and graphs the resulting throughput, alongside the
**ping RTT** to that gateway, all in your terminal using braille characters.

```
gTraffic — Actiontec xDSL Router
        ┌──────────────────────────────────────────────────────────────┐
 50Mbps │       ⡠⢄                              ⢀⠔⠁⢇                  │ down  12.4 Mbps
        │      ⡠⠁ ⠈⠢⡀          ⢀⠔⢄          ⢀⠔⠁    ⠘⠤⡀                │ up    188.0 Mbps
   ...
        └──────────────────────────────────────────────────────────────┘
        ┌──────────────────────────────────────────────────────────────┐
   8 ms │     ⢀⠔⠉⠉⠢⡀                                                   │ ping   5.0 ms
        │  ⢀⠔⠁     ⠈⠢⡀⢀⡠⠤⠤⠤⠔⠒⠒⠊⠉⠉⠉⠉⠉⠉⠉⠑⠒⠦⠤⢄⣀                          │
   ...
        └──────────────────────────────────────────────────────────────┘
```

## How it works

* **Throughput** — SSDP `M-SEARCH` to find your IGD, then SOAP
  `GetTotalBytesSent` / `GetTotalBytesReceived` against the
  `WANCommonInterfaceConfig:1` service. Counters are uint32 (octets), so
  wrap-around at 4 GiB is handled.
* **Ping** — wraps the system `ping` binary, so no admin/root needed.
  Overlaid on the same chart as throughput, with its own ms scale shown in
  the legend.
* **WAN status** — best-effort `GetStatusInfo` and `GetExternalIPAddress`
  on the `WANIPConnection:1` / `WANPPPConnection:1` service for a header
  showing public IP, link state, and connection uptime. Refreshed every 30s
  by default. If the router doesn't expose them, the header just shows what
  it could find — the app keeps running.
* **Render** — [`plotext`](https://github.com/piccolomo/plotext) with braille
  markers, redrawn once per sample.

## Requirements

* Python **3.10+** (uses PEP 604 `X | Y` and `tuple[int, int]` annotations).
* A router with **UPnP/IGD enabled** that exposes
  `WANCommonInterfaceConfig:1` (most consumer routers do; some may need it
  toggled on in the admin UI).
* One Python dependency:

  ```
  plotext>=5.2.8
  ```

  Install with:

  ```sh
  pip install -r requirements.txt
  ```

* On Windows, run inside Windows Terminal / PowerShell / a modern console
  that supports ANSI + UTF-8. Old `cmd.exe` may not render braille glyphs.

## Install

```sh
git clone https://github.com/SixOfFive/gTraffic.git
cd gTraffic
pip install -r requirements.txt
```

## Usage

```sh
python -m gtraffic <gateway-ip> [options]
```

### Quick start

```sh
python -m gtraffic 192.168.1.1
```

Press **Ctrl-C** to exit.

### Find your gateway / interfaces

```sh
python -m gtraffic --list-interfaces
```

Lists local IPv4 addresses you can pass to `--interface`. Pick the one on the
same subnet as your router.

### Common scenarios

Bind multicast SSDP to a specific NIC (useful on multi-homed Windows hosts
where SSDP otherwise leaves via the wrong adapter):

```sh
python -m gtraffic 192.168.1.1 --interface 192.168.1.50
```

Force unicast SSDP (skip multicast — useful when a switch / firewall
swallows IGMP):

```sh
python -m gtraffic 192.168.1.1 --method unicast
```

Skip discovery entirely if you already know the device-description URL:

```sh
python -m gtraffic --description-url http://192.168.1.1:5431/dyndev/uuid:...
```

Sample faster (e.g. every 0.5s) and keep more history:

```sh
python -m gtraffic 192.168.1.1 --interval 0.5 --history 240
```

### All options

| Flag | Default | Description |
| --- | --- | --- |
| `host` (positional) | — | Gateway IP. Required unless `--description-url` is given. |
| `-i, --interface IP` | auto | Local IPv4 to bind the SSDP socket to. |
| `--list-interfaces` | — | Print local IPv4 addresses and exit. |
| `-p, --port PORT` | `1900` | SSDP destination port. |
| `-m, --method {auto,multicast,unicast}` | `auto` | SSDP M-SEARCH method. `auto` tries multicast first, then unicast. |
| `--description-url URL` | — | Skip SSDP and fetch the device description directly. |
| `--interval SECS` | `1.0` | Sample / redraw interval in seconds. |
| `--history N` | `120` | Number of samples to keep on the graph. |
| `--ping-timeout MS` | `1000` | Per-ping timeout in milliseconds. |
| `--discover-timeout SECS` | `3.0` | SSDP discovery timeout per attempt. |
| `--meta-interval SECS` | `30.0` | How often to refresh WAN status / external IP. |

## Troubleshooting

* **`No UPnP IGD WANCommonInterfaceConfig found`** — make sure UPnP is enabled
  on the router. On multi-homed hosts, pass `--interface` with the IP on the
  router's subnet. If multicast is blocked, try `--method unicast`. As a last
  resort, find the description URL in the router's admin UI / by browsing
  `http://<gateway>:<port>/` and pass it via `--description-url`.
* **Glyphs look like boxes** — your terminal lacks UTF-8 / braille font
  support. Use Windows Terminal, iTerm2, kitty, alacritty, etc.
* **Counters look stuck** — some routers update their counters slowly (every
  several seconds). Try `--interval 5`.

## License

MIT
