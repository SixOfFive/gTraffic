"""gTraffic main loop: poll IGD throughput + ping once per second, render with plotext."""
from __future__ import annotations

import argparse
import math
import sys
import time
from collections import deque
from typing import Deque, Optional

# plotext draws braille / box-drawing characters that need a UTF-8 stdout.
# Windows defaults to cp1252, so reconfigure before importing plotext.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

import plotext as plt

from .ping import ping_once
from .upnp import (
    IgdService,
    diff_counter,
    discover_igd,
    from_description_url,
    list_local_ipv4,
)


def _fmt_bps(bps: float) -> str:
    if bps != bps or bps < 0:  # NaN or weirdness
        return "   --   "
    units = ["bps", "Kbps", "Mbps", "Gbps", "Tbps"]
    v = bps
    for u in units:
        if v < 1000:
            return f"{v:6.2f} {u}"
        v /= 1000
    return f"{v:6.2f} Pbps"


def _fmt_ms(ms: Optional[float]) -> str:
    if ms is None:
        return "   --  "
    return f"{ms:5.1f} ms"


def _build_service(args: argparse.Namespace) -> IgdService:
    if args.description_url:
        return from_description_url(args.description_url)
    return discover_igd(
        args.host,
        interface_ip=args.interface,
        method=args.method,
        ssdp_port=args.port,
        timeout=args.discover_timeout,
    )


def run(args: argparse.Namespace) -> int:
    print(f"gTraffic — discovering IGD ({args.host or 'broadcast'})...", flush=True)
    try:
        svc = _build_service(args)
    except Exception as e:
        print(f"discovery failed: {e}", file=sys.stderr)
        return 2
    print(f"  {svc.friendly_name}  ->  {svc.control_url}", flush=True)
    time.sleep(0.5)

    history = args.history
    up_bps: Deque[float] = deque(maxlen=history)
    down_bps: Deque[float] = deque(maxlen=history)
    rtts: Deque[float] = deque(maxlen=history)

    from .upnp import get_throughput_counters

    prev_sent, prev_recv = get_throughput_counters(svc)
    prev_t = time.time()

    ping_target = args.host or _hostname_from_url(svc.control_url)

    try:
        while True:
            tick_start = time.time()
            try:
                cur_sent, cur_recv = get_throughput_counters(svc)
                now = time.time()
                dt = max(1e-3, now - prev_t)
                up = diff_counter(prev_sent, cur_sent) * 8 / dt
                down = diff_counter(prev_recv, cur_recv) * 8 / dt
                prev_sent, prev_recv, prev_t = cur_sent, cur_recv, now
            except Exception:
                up = float("nan")
                down = float("nan")

            rtt = ping_once(ping_target, timeout_ms=args.ping_timeout) if ping_target else None

            up_bps.append(up)
            down_bps.append(down)
            rtts.append(rtt if rtt is not None else float("nan"))

            _render(args, svc, up_bps, down_bps, rtts)

            elapsed = time.time() - tick_start
            time.sleep(max(0.0, args.interval - elapsed))
    except KeyboardInterrupt:
        print()
        return 0


def _hostname_from_url(url: str) -> Optional[str]:
    import urllib.parse
    return urllib.parse.urlparse(url).hostname


def _last_finite(seq: Deque[float]) -> float:
    for v in reversed(seq):
        if v == v and not math.isinf(v):
            return v
    return float("nan")


def _render(
    args: argparse.Namespace,
    svc: IgdService,
    up: Deque[float],
    down: Deque[float],
    rtts: Deque[float],
) -> None:
    plt.clt()  # clear terminal
    plt.clf()  # clear figure
    plt.subplots(2, 1)

    x = list(range(-len(up) + 1, 1))  # negative seconds = age
    label_down = f"down {_fmt_bps(_last_finite(down))}"
    label_up = f"up   {_fmt_bps(_last_finite(up))}"

    plt.subplot(1, 1)
    plt.title(f"gTraffic — {svc.friendly_name or svc.location}")
    plt.plot(x, list(down), label=label_down, color="green", marker="braille")
    plt.plot(x, list(up), label=label_up, color="cyan", marker="braille")
    plt.ylabel("throughput")
    plt.xlabel("seconds (now = 0)")
    plt.ylim(lower=0)
    plt.theme("pro")

    last_rtt = _last_finite(rtts)
    rtt_label = f"ping {_fmt_ms(last_rtt if last_rtt == last_rtt else None)}"
    plt.subplot(2, 1)
    plt.plot(x, list(rtts), label=rtt_label, color="magenta", marker="braille")
    plt.ylabel("ms")
    plt.xlabel(f"target {args.host or _hostname_from_url(svc.control_url)}")
    plt.ylim(lower=0)
    plt.theme("pro")

    plt.show()


def _list_interfaces_and_exit() -> int:
    ips = list_local_ipv4()
    if not ips:
        print("(no local IPv4 addresses found)")
        return 1
    print("Local IPv4 interfaces:")
    for ip in ips:
        print(f"  {ip}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="gtraffic",
        description="Console grapher for UPnP/IGD throughput + ping RTT.",
    )
    p.add_argument(
        "host",
        nargs="?",
        help="Gateway IP (UPnP IGD device), e.g. 192.168.1.1. "
             "Optional if --description-url is given.",
    )
    p.add_argument(
        "-i", "--interface",
        metavar="IP",
        help="Local IPv4 to bind the SSDP socket to. Useful on multi-homed hosts.",
    )
    p.add_argument(
        "--list-interfaces",
        action="store_true",
        help="List local IPv4 addresses and exit.",
    )
    p.add_argument(
        "-p", "--port",
        type=int,
        default=1900,
        help="SSDP destination port (default: 1900).",
    )
    p.add_argument(
        "-m", "--method",
        choices=("auto", "multicast", "unicast"),
        default="auto",
        help="SSDP M-SEARCH method. 'auto' tries multicast then unicast (default).",
    )
    p.add_argument(
        "--description-url",
        metavar="URL",
        help="Skip SSDP and fetch the device description directly from this URL.",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Sample interval in seconds (default: 1.0).",
    )
    p.add_argument(
        "--history",
        type=int,
        default=120,
        help="Number of samples to keep on the graph (default: 120).",
    )
    p.add_argument(
        "--ping-timeout",
        type=int,
        default=1000,
        help="Per-ping timeout in milliseconds (default: 1000).",
    )
    p.add_argument(
        "--discover-timeout",
        type=float,
        default=3.0,
        help="SSDP discovery timeout in seconds (default: 3.0).",
    )
    args = p.parse_args(argv)

    if args.list_interfaces:
        return _list_interfaces_and_exit()

    if not args.host and not args.description_url:
        p.error("host is required (or pass --description-url)")

    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
