"""gTraffic main loop: poll IGD throughput + ping once per second, render with plotext."""
from __future__ import annotations

import argparse
import math
import sys
import time
from collections import deque
from dataclasses import dataclass
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
    WanStatus,
    diff_counter,
    discover_igd,
    from_description_url,
    get_external_ip,
    get_wan_status,
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


def _fmt_uptime(seconds: int) -> str:
    if seconds < 0:
        return "?"
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


_BPS_UNITS = (
    ("Tbps", 1e12),
    ("Gbps", 1e9),
    ("Mbps", 1e6),
    ("Kbps", 1e3),
    ("bps", 1.0),
)


def _fmt_bps_short(bps: float) -> str:
    """Compact y-tick label, e.g. '0', '50 Mbps', '1.2 Gbps'."""
    if bps == 0:
        return "0"
    if not math.isfinite(bps) or bps < 0:
        return ""
    for unit, scale in _BPS_UNITS:
        if bps >= scale:
            v = bps / scale
            if v >= 100:
                return f"{v:.0f} {unit}"
            if v >= 10:
                return f"{v:.1f} {unit}"
            return f"{v:.2f} {unit}"
    return f"{bps:.0f} bps"


def _nice_bps_ticks(max_val: float, n: int = 5) -> tuple[list[float], list[str]]:
    """Pick ~n nice round tick positions in [0, max_val] and label them."""
    if max_val <= 0 or not math.isfinite(max_val):
        positions = [0.0, 1000.0]
    else:
        raw_step = max_val / max(1, n)
        exp = math.floor(math.log10(raw_step)) if raw_step > 0 else 0
        base = 10 ** exp
        for m in (1, 2, 2.5, 5, 10):
            step = m * base
            if step >= raw_step:
                break
        upper = math.ceil(max_val / step) * step
        count = int(round(upper / step)) + 1
        positions = [i * step for i in range(count)]
    return positions, [_fmt_bps_short(p) for p in positions]


def _nice_ms_ticks(max_val: float, n: int = 4) -> tuple[list[float], list[str]]:
    if max_val <= 0 or not math.isfinite(max_val):
        positions = [0.0, 10.0]
    else:
        raw_step = max_val / max(1, n)
        exp = math.floor(math.log10(raw_step)) if raw_step > 0 else 0
        base = 10 ** exp
        for m in (1, 2, 2.5, 5, 10):
            step = m * base
            if step >= raw_step:
                break
        upper = math.ceil(max_val / step) * step
        count = int(round(upper / step)) + 1
        positions = [i * step for i in range(count)]
    labels = [(f"{p:.0f} ms" if p >= 10 else f"{p:.1f} ms") if p > 0 else "0" for p in positions]
    return positions, labels


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


@dataclass
class _SessionState:
    external_ip: Optional[str] = None
    wan_status: Optional[WanStatus] = None
    last_meta_refresh: float = 0.0
    ping_axis_max: float = 20.0  # sticky max for the overlaid ping line


def _refresh_meta(svc: IgdService, state: _SessionState, every: float = 30.0) -> None:
    now = time.time()
    if now - state.last_meta_refresh < every:
        return
    state.last_meta_refresh = now
    try:
        state.external_ip = get_external_ip(svc) or state.external_ip
    except Exception:
        pass
    try:
        s = get_wan_status(svc)
        if s is not None:
            state.wan_status = s
    except Exception:
        pass


def run(args: argparse.Namespace) -> int:
    print(f"gTraffic — discovering IGD ({args.host or 'broadcast'})...", flush=True)
    try:
        svc = _build_service(args)
    except Exception as e:
        print(f"discovery failed: {e}", file=sys.stderr)
        return 2
    print(f"  {svc.friendly_name}  ->  {svc.control_url}", flush=True)
    if svc.wan_connection:
        print(f"  WAN connection service: {svc.wan_connection.service_type}", flush=True)
    else:
        print("  (no WAN*Connection service — external IP / uptime will be hidden)", flush=True)
    time.sleep(0.5)

    history = args.history
    up_bps: Deque[float] = deque(maxlen=history)
    down_bps: Deque[float] = deque(maxlen=history)
    rtts: Deque[float] = deque(maxlen=history)

    from .upnp import get_throughput_counters

    prev_sent, prev_recv = get_throughput_counters(svc)
    prev_t = time.time()

    state = _SessionState()
    _refresh_meta(svc, state, every=0.0)  # force first fetch

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
                if args.swap_up_down:
                    up, down = down, up
                prev_sent, prev_recv, prev_t = cur_sent, cur_recv, now
            except Exception:
                up = float("nan")
                down = float("nan")

            rtt = ping_once(ping_target, timeout_ms=args.ping_timeout) if ping_target else None

            up_bps.append(up)
            down_bps.append(down)
            rtts.append(rtt if rtt is not None else float("nan"))

            _refresh_meta(svc, state, every=args.meta_interval)
            # Bump uptime by elapsed wall time so it ticks live between refreshes
            if state.wan_status:
                state.wan_status.uptime_seconds += int(args.interval)

            _render(args, svc, state, up_bps, down_bps, rtts)

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


def _build_title(svc: IgdService, state: _SessionState) -> str:
    parts = [f"gTraffic — {svc.friendly_name or svc.location}"]
    if state.external_ip:
        parts.append(f"WAN {state.external_ip}")
    if state.wan_status:
        s = state.wan_status
        parts.append(s.connection_status)
        parts.append(f"up {_fmt_uptime(s.uptime_seconds)}")
        if s.last_error and s.last_error != "ERROR_NONE":
            parts.append(f"err {s.last_error}")
    return "  |  ".join(parts)


def _render(
    args: argparse.Namespace,
    svc: IgdService,
    state: _SessionState,
    up: Deque[float],
    down: Deque[float],
    rtts: Deque[float],
) -> None:
    plt.clt()
    plt.clf()

    x = list(range(-len(up) + 1, 1))  # negative seconds = age

    bps_max = max(
        (v for v in list(up) + list(down) if v == v and math.isfinite(v)),
        default=0.0,
    )
    bps_pos, bps_labels = _nice_bps_ticks(bps_max)
    bps_axis_max = bps_pos[-1] if bps_pos[-1] > 0 else 1000.0

    # Sticky ping axis: grow but never shrink within a session.
    rtt_observed_max = max(
        (v for v in rtts if v == v and math.isfinite(v)),
        default=0.0,
    )
    if rtt_observed_max * 1.2 > state.ping_axis_max:
        state.ping_axis_max = max(20.0, rtt_observed_max * 1.5)
    ping_axis_max = state.ping_axis_max

    # Overlay ping by mapping [0, ping_axis_max] ms -> [0, bps_axis_max] bps.
    scaled_rtts = [
        (v / ping_axis_max) * bps_axis_max if (v == v and math.isfinite(v)) else float("nan")
        for v in rtts
    ]

    last_down = _last_finite(down)
    last_up = _last_finite(up)
    last_rtt = _last_finite(rtts)
    label_down = f"down {_fmt_bps(last_down)}"
    label_up = f"up   {_fmt_bps(last_up)}"
    rtt_now = _fmt_ms(last_rtt if last_rtt == last_rtt else None)
    label_ping = f"ping {rtt_now}  (scale 0–{int(ping_axis_max)} ms)"

    plt.title(_build_title(svc, state))
    plt.plot(x, list(down), label=label_down, color="green", marker="braille")
    plt.plot(x, list(up), label=label_up, color="cyan", marker="braille")
    plt.plot(x, scaled_rtts, label=label_ping, color="magenta", marker="braille")
    plt.xlabel(f"seconds (now = 0)   target {args.host or _hostname_from_url(svc.control_url)}")
    plt.theme("pro")
    plt.ylim(0, bps_axis_max)
    plt.yticks(bps_pos, bps_labels)

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
    p.add_argument(
        "--meta-interval",
        type=float,
        default=30.0,
        help="How often (seconds) to refresh WAN status / external IP (default: 30).",
    )
    p.add_argument(
        "--swap-up-down",
        action="store_true",
        help="Swap upload/download labels. Use if your router reports the "
             "GetTotalBytesSent / GetTotalBytesReceived counters reversed.",
    )
    args = p.parse_args(argv)

    if args.list_interfaces:
        return _list_interfaces_and_exit()

    if not args.host and not args.description_url:
        p.error("host is required (or pass --description-url)")

    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
