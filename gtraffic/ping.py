"""Single-shot ping wrapper using the system `ping` binary (no admin/root needed)."""
from __future__ import annotations

import re
import subprocess
import sys
from typing import Optional

_TIME_RE = re.compile(r"time[=<]\s*([\d.]+)\s*ms", re.IGNORECASE)


def ping_once(host: str, timeout_ms: int = 1000) -> Optional[float]:
    """Send a single ICMP echo and return RTT in ms, or None on timeout/error."""
    if sys.platform.startswith("win"):
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), host]
    else:
        # -W on Linux is seconds; -t on macOS is total deadline (also seconds).
        secs = max(1, (timeout_ms + 999) // 1000)
        cmd = ["ping", "-c", "1", "-W", str(secs), host]
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=(timeout_ms / 1000) + 1.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if out.returncode != 0:
        return None
    m = _TIME_RE.search(out.stdout)
    return float(m.group(1)) if m else None


if __name__ == "__main__":
    import time
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.15.1"
    for _ in range(3):
        print(f"{host}: {ping_once(host)} ms")
        time.sleep(0.5)
