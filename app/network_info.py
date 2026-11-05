"""Network interface and IP address discovery for Samba share display."""

from __future__ import annotations

import shutil
import socket
import subprocess
from typing import List


def get_lan_ip_addresses() -> List[str]:
    """Return a list of non-loopback IPv4 addresses assigned to the machine."""
    ips: List[str] = []

    # 1. Try `ip -4 -o addr show` on Linux/Alpine
    ip_bin = shutil.which("ip")
    if ip_bin:
        try:
            res = subprocess.run(
                [ip_bin, "-4", "-o", "addr", "show"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if res.returncode == 0:
                for line in res.stdout.splitlines():
                    parts = line.split()
                    if len(parts) >= 4 and "inet" in parts:
                        idx = parts.index("inet")
                        if idx + 1 < len(parts):
                            cidr = parts[idx + 1]
                            addr = cidr.split("/")[0]
                            if not addr.startswith("127.") and addr not in ips:
                                ips.append(addr)
        except Exception:
            pass

    # 2. Try UDP routing socket probe (does not send packets on wire)
    if not ips:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(0.2)
                s.connect(("10.255.255.255", 1))
                addr = s.getsockname()[0]
                if addr and not addr.startswith("127.") and addr not in ips:
                    ips.append(addr)
        except Exception:
            pass

    # 3. Fallback to gethostbyname_ex
    if not ips:
        try:
            _, _, host_ips = socket.gethostbyname_ex(socket.gethostname())
            for addr in host_ips:
                if addr and not addr.startswith("127.") and addr not in ips:
                    ips.append(addr)
        except Exception:
            pass

    return ips


def get_primary_ip_display() -> str:
    """Format the primary LAN IP and Samba share path for the UI bar."""
    ips = get_lan_ip_addresses()
    if not ips:
        return "IP: Offline / No LAN  |  Share: \\\\<offline>\\storage"
    primary = ips[0]
    extra = f" (+{len(ips) - 1})" if len(ips) > 1 else ""
    return f"IP: {primary}{extra}   •   Samba Share: \\\\{primary}\\storage"
