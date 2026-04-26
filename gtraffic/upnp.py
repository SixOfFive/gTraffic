"""UPnP/IGD discovery via SSDP + SOAP throughput counter queries.

We find an InternetGatewayDevice and walk into its embedded WANDevice
to locate the WANCommonInterfaceConfig:1 service, which exposes:
  - GetTotalBytesSent      -> NewTotalBytesSent      (uint32, octets, wraps at 4 GiB)
  - GetTotalBytesReceived  -> NewTotalBytesReceived  (uint32, octets, wraps at 4 GiB)
  - GetCommonLinkProperties -> NewLayer1UpstreamMaxBitRate / NewLayer1DownstreamMaxBitRate (bps)
"""
from __future__ import annotations

import http.client
import re
import socket
import threading
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
class _Endpoint:
    control_url: str
    service_type: str


@dataclass
class IgdService:
    location: str         # URL of the device description we discovered
    base_url: str         # base URL for resolving relative controlURLs
    control_url: str      # WANCommonInterfaceConfig:1 control URL (back-compat)
    service_type: str     # WANCommonInterfaceConfig:1 service type
    friendly_name: str = ""
    wan_connection: Optional[_Endpoint] = None  # WANIPConnection or WANPPPConnection


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

    common_iface: Optional[_Endpoint] = None
    wan_conn: Optional[_Endpoint] = None

    for svc in root.iter(f"{{{DEVICE_NS}}}service"):
        st = (svc.findtext("d:serviceType", default="", namespaces=ns) or "").strip()
        ctrl = (svc.findtext("d:controlURL", default="", namespaces=ns) or "").strip()
        if not ctrl:
            continue
        full = urllib.parse.urljoin(url_base, ctrl)
        if "WANCommonInterfaceConfig" in st and common_iface is None:
            common_iface = _Endpoint(full, st)
        elif ("WANIPConnection" in st or "WANPPPConnection" in st) and wan_conn is None:
            wan_conn = _Endpoint(full, st)

    if not common_iface:
        raise RuntimeError("WANCommonInterfaceConfig service not found in device description")

    return IgdService(
        location=location,
        base_url=url_base,
        control_url=common_iface.control_url,
        service_type=common_iface.service_type,
        friendly_name=friendly,
        wan_connection=wan_conn,
    )


_conn_cache: dict[tuple[str, int], http.client.HTTPConnection] = {}
_conn_lock = threading.Lock()


def _get_conn(host: str, port: int) -> http.client.HTTPConnection:
    key = (host, port)
    with _conn_lock:
        conn = _conn_cache.get(key)
        if conn is None:
            conn = http.client.HTTPConnection(host, port, timeout=3)
            _conn_cache[key] = conn
        return conn


def _drop_conn(host: str, port: int) -> None:
    with _conn_lock:
        conn = _conn_cache.pop((host, port), None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def _soap_call_endpoint(ep: _Endpoint, action: str) -> str:
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:{action} xmlns:u="{ep.service_type}"></u:{action}>'
        "</s:Body></s:Envelope>"
    ).encode("utf-8")
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": f'"{ep.service_type}#{action}"',
        "Connection": "keep-alive",
    }
    parsed = urllib.parse.urlparse(ep.control_url)
    host = parsed.hostname or ""
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query

    last_err: Optional[Exception] = None
    for attempt in range(2):
        conn = _get_conn(host, port)
        try:
            conn.request("POST", path, body, headers)
            resp = conn.getresponse()
            data = resp.read()  # must fully drain before next request on this conn
            return data.decode("utf-8", errors="replace")
        except (http.client.HTTPException, ConnectionError, OSError, TimeoutError) as e:
            last_err = e
            _drop_conn(host, port)
            continue
    raise RuntimeError(f"SOAP call {action} failed: {last_err}")


def _soap_call(svc: IgdService, action: str) -> str:
    return _soap_call_endpoint(_Endpoint(svc.control_url, svc.service_type), action)


def get_throughput_counters(svc: IgdService) -> tuple[int, int]:
    """Return (bytes_sent, bytes_received). Counters are uint32 and wrap at 4 GiB."""
    sent_xml = _soap_call(svc, "GetTotalBytesSent")
    recv_xml = _soap_call(svc, "GetTotalBytesReceived")
    sent_m = re.search(r"<NewTotalBytesSent>(\d+)</NewTotalBytesSent>", sent_xml)
    recv_m = re.search(r"<NewTotalBytesReceived>(\d+)</NewTotalBytesReceived>", recv_xml)
    if not sent_m or not recv_m:
        raise RuntimeError("Unexpected SOAP response from IGD")
    return int(sent_m.group(1)), int(recv_m.group(1))


@dataclass
class WanStatus:
    connection_status: str  # "Connected", "Disconnected", "Connecting", ...
    last_error: str         # "ERROR_NONE" when healthy
    uptime_seconds: int     # seconds connection has been up


def get_wan_status(svc: IgdService) -> Optional[WanStatus]:
    """Call GetStatusInfo on the WAN*Connection service. None if unavailable."""
    if not svc.wan_connection:
        return None
    xml = _soap_call_endpoint(svc.wan_connection, "GetStatusInfo")
    cs = re.search(r"<NewConnectionStatus>([^<]*)</NewConnectionStatus>", xml)
    le = re.search(r"<NewLastConnectionError>([^<]*)</NewLastConnectionError>", xml)
    ut = re.search(r"<NewUptime>(\d+)</NewUptime>", xml)
    if not cs:
        return None
    return WanStatus(
        connection_status=cs.group(1).strip(),
        last_error=(le.group(1).strip() if le else ""),
        uptime_seconds=int(ut.group(1)) if ut else 0,
    )


def get_external_ip(svc: IgdService) -> Optional[str]:
    """Call GetExternalIPAddress on the WAN*Connection service. None if unavailable."""
    if not svc.wan_connection:
        return None
    xml = _soap_call_endpoint(svc.wan_connection, "GetExternalIPAddress")
    m = re.search(r"<NewExternalIPAddress>([^<]*)</NewExternalIPAddress>", xml)
    return m.group(1).strip() if m and m.group(1).strip() else None


def diff_counter(prev: int, cur: int, mod: int = 1 << 32) -> int:
    """Return delta accounting for uint32 wrap-around."""
    if cur >= prev:
        return cur - prev
    return (mod - prev) + cur
