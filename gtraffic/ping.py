"""Continuous ping wrapper.

Spawns a single long-running `ping` process and reads RTT from each
response line in a background thread. This avoids creating a fresh
subprocess every second, which on Windows can exhaust handles / paged
pool over a long session and trip `[WinError 1450] Insufficient system
resources exist to complete the requested service`.
"""
from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from typing import Optional

_TIME_RE = re.compile(r"time[=<]\s*([\d.]+)\s*ms", re.IGNORECASE)
_FAIL_RE = re.compile(
    r"(timed out|unreachable|100% packet loss|no route)", re.IGNORECASE
)


class ContinuousPing:
    """Background thread that runs `ping <host>` continuously and tracks the latest RTT."""

    def __init__(self, host: str, interval: float = 1.0) -> None:
        self.host = host
        self.interval = interval
        self._latest: Optional[float] = None
        self._latest_at: float = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if sys.platform.startswith("win"):
            cmd = ["ping", "-t", self.host]
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            cmd = ["ping", "-i", str(max(0.2, self.interval)), self.host]
            creationflags = 0

        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            creationflags=creationflags,
        )
        self._thread = threading.Thread(
            target=self._reader, name="gtraffic-ping", daemon=True
        )
        self._thread.start()

    def _reader(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            for line in proc.stdout:
                if self._stop.is_set():
                    break
                m = _TIME_RE.search(line)
                now = time.time()
                if m:
                    try:
                        rtt = float(m.group(1))
                    except ValueError:
                        continue
                    with self._lock:
                        self._latest = rtt
                        self._latest_at = now
                elif _FAIL_RE.search(line):
                    with self._lock:
                        self._latest = None
                        self._latest_at = now
        except (OSError, ValueError):
            pass

    def latest(self, max_age: float = 5.0) -> Optional[float]:
        """Return the most recent RTT in ms, or None on timeout / stale data."""
        with self._lock:
            if self._latest_at == 0.0:
                return None
            if time.time() - self._latest_at > max_age:
                return None
            return self._latest

    def close(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        except Exception:
            pass


if __name__ == "__main__":
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.15.1"
    p = ContinuousPing(host)
    p.start()
    try:
        for _ in range(5):
            time.sleep(1.2)
            print(f"{host}: {p.latest()} ms")
    finally:
        p.close()
