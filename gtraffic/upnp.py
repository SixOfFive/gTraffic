"""UPnP/IGD discovery via SSDP + SOAP throughput counter queries.

We find an InternetGatewayDevice and walk into its embedded WANDevice
to locate the WANCommonInterfaceConfig:1 service, which exposes:
  - GetTotalBytesSent      -> NewTotalBytesSent      (uint32, octets, wraps at 4 GiB)
  - GetTotalBytesReceived  -> NewTotalBytesReceived  (uint32, octets, wraps at 4 GiB)
  - GetCommonLinkProperties -> NewLayer1UpstreamMaxBitRate / NewLayer1DownstreamMaxBitRate (bps)
"""
from __future__ import annotations

import re
import socket
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
DEVICE_NS = "urn:schemas-upnp-org:device-1-0"

# Search targets to try, in order. Some routers only respond to upnp:rootdevice
# (not the service-specific ST), so we walk the description tree ourselves.
SEARCH_TARGETS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "upnp:rootdevice",
    "ssdp:all",
)

DiscoveryMethod = str  # "multicast" | "unicast" | "auto"


@dataclass
class IgdService:
    location: str         # URL of the device description we discovered
    base_url: str         # base URL for resolving relative controlURLs
    control_url: str
    service_type: str
    friendly_name: str = ""


def list_local_ipv4() -> list[str]:
    """Return a sorted list of local IPv4 addresses (best-effort, stdlib-only)."""
    ips: set[str] = set()
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    # Drop link-local autoconf if anything else exists
    real = {ip for ip in ips if not ip.startswith("169.254.")}
    return sorted(real or ips)


def _local_ip_for(target: str) -> Optional[str]:
    """Return the local IP that the OS would use to reach `target`, or None."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((target, 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def discover_igd(
    target: Optional[str] = None,
    *,
    interface_ip: Optional[str] = None,
    method: DiscoveryMethod = "auto",
    ssdp_port: int = SSDP_PORT,
    timeout: float = 3.0,
) -> IgdService:
    """Discover an IGD WANCommonInterfaceConfig service via SSDP M-SEARCH.

    Args:
      target: gateway IP. If given, responses must come from / point to it.
      interface_ip: local IPv4 to bind the SSDP socket to. If None and a target
        is given, the OS-chosen route for the target is used.
      method: "multicast" sends to 239.255.255.250; "unicast" sends directly to
        `target`; "auto" tries multicast first then unicast (requires `target`).
      ssdp_port: SSDP destination port (default 1900).
      timeout: per-attempt timeout in seconds.
    """
    bind_ip = interface_ip or (_local_ip_for(target) if target else None)

    if method == "unicast" and not target:
        raise ValueError("--method unicast requires a target host")

    methods: list[DiscoveryMethod]
    if method == "auto":
        methods = ["multicast", "unicast"] if target else ["multicast"]
    else:
        methods = [method]

    last_err: Optional[Exception] = None
    for m in methods:
        for st in SEARCH_TARGETS:
            try:
                location = _ssdp_search(
                    st,
                    target=target,
                    bind_ip=bind_ip,
                    ssdp_port=ssdp_port,
                    method=m,
                    timeout=timeout,
                )
            except OSError as e:
                last_err = e
                continue
            if location:
                return _parse_device_description(location)
    msg = "No UPnP IGD WANCommonInterfaceConfig found"
    if target:
        msg += f" matching host {target}"
    if last_err:
        msg += f" (last error: {last_err})"
    raise RuntimeError(msg)


def from_description_url(url: str) -> IgdService:
    """Skip SSDP and build an IgdService directly from a known device-description URL."""
    return _parse_device_description(url)


def _ssdp_search(
    st: str,
    *,
    target: Optional[str],
    bind_ip: Optional[str],
    ssdp_port: int,
    method: DiscoveryMethod,
    timeout: float,
) -> Optional[str]:
    if method == "unicast":
        if not target:
            return None
        dest_host = target
    else:
        dest_host = SSDP_ADDR

    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {dest_host}:{ssdp_port}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {st}\r\n\r\n"
    ).encode("ascii")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    if method == "multicast":
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        if bind_ip:
            sock.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(bind_ip)
            )
    if bind_ip:
        try:
            sock.bind((bind_ip, 0))
        except OSError:
            sock.bind(("", 0))
    else:
        sock.bind(("", 0))

    try:
        sock.sendto(msg, (dest_host, ssdp_port))
        deadline = time.time() + timeout
        while time.time() < deadline:
            sock.settimeout(max(0.05, deadline - time.time()))
            try:
                data, addr = sock.recvfrom(8192)
            except socket.timeout:
                break
            if target and addr[0] != target:
                continue
            text = data.decode("utf-8", errors="replace")
            m = re.search(r"^LOCATION:\s*(\S+)", text, re.IGNORECASE | re.MULTILINE)
            if not m:
                continue
            location = m.group(1).strip()
            host = urllib.parse.urlparse(location).hostname
            if target and host != target:
                continue
            return location
    finally:
        sock.close()
    return None


def _parse_device_description(location: str) -> IgdService:
    with urllib.request.urlopen(location, timeout=3) as resp:
        body = resp.read()
    root = ET.fromstring(body)
    ns = {"d": DEVICE_NS}

    url_base = root.findtext("d:URLBase", default="", namespaces=ns).strip() or location
    friendly = root.findtext(".//d:friendlyName", default="", namespaces=ns)

    for svc in root.iter(f"{{{DEVICE_NS}}}service"):
        st = (svc.findtext("d:serviceType", default="", namespaces=ns) or "").strip()
        if "WANCommonInterfaceConfig" in st:
            ctrl = (svc.findtext("d:controlURL", default="", namespaces=ns) or "").strip()
            full = urllib.parse.urljoin(url_base, ctrl)
            return IgdService(
                location=location,
                base_url=url_base,
                control_url=full,
                service_type=st,
                friendly_name=friendly,
            )
    raise RuntimeError("WANCommonInterfaceConfig service not found in device description")


def _soap_call(svc: IgdService, action: str) -> str:
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:{action} xmlns:u="{svc.service_type}"></u:{action}>'
        "</s:Body></s:Envelope>"
    ).encode("utf-8")
    req = urllib.request.Request(
        svc.control_url,
        data=body,
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{svc.service_type}#{action}"',
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2) as resp:
        return resp.read().decode("utf-8", errors="replace")


def get_throughput_counters(svc: IgdService) -> tuple[int, int]:
    """Return (bytes_sent, bytes_received). Counters are uint32 and wrap at 4 GiB."""
    sent_xml = _soap_call(svc, "GetTotalBytesSent")
    recv_xml = _soap_call(svc, "GetTotalBytesReceived")
    sent_m = re.search(r"<NewTotalBytesSent>(\d+)</NewTotalBytesSent>", sent_xml)
    recv_m = re.search(r"<NewTotalBytesReceived>(\d+)</NewTotalBytesReceived>", recv_xml)
    if not sent_m or not recv_m:
        raise RuntimeError("Unexpected SOAP response from IGD")
    return int(sent_m.group(1)), int(recv_m.group(1))


def diff_counter(prev: int, cur: int, mod: int = 1 << 32) -> int:
    """Return delta accounting for uint32 wrap-around."""
    if cur >= prev:
        return cur - prev
    return (mod - prev) + cur
