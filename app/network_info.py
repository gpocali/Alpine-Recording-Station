"""Network interface, IP address discovery, and system CPU/RAM telemetry for the status bar."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import socket
import subprocess
from typing import Callable, List, Optional, Tuple


CPU_WARN_PERCENT = 75.0
CPU_CRITICAL_PERCENT = 90.0
RAM_WARN_PERCENT = 80.0
RAM_CRITICAL_PERCENT = 90.0


def _format_ram_bytes_compact(num_bytes: int) -> str:
    """Format byte count into compact MB or GB string for the bottom bar."""
    val = max(0, int(num_bytes))
    gb = 1024 ** 3
    mb = 1024 ** 2
    if val >= gb:
        return f"{val / gb:.1f} GB"
    return f"{val / mb:.0f} MB"


def parse_proc_stat_cpu(proc_stat_text: str) -> Optional[Tuple[int, int]]:
    """Parse aggregate 'cpu' line from /proc/stat and return (total_jiffies, idle_jiffies)."""
    for line in proc_stat_text.splitlines():
        if line.startswith("cpu ") or line.startswith("cpu\t"):
            parts = line.split()
            if len(parts) >= 5:
                try:
                    values = [int(x) for x in parts[1:]]
                except ValueError:
                    return None
                total = sum(values)
                # idle is column 3 (0-based), iowait is column 4 if present
                idle = values[3] + (values[4] if len(values) > 4 else 0)
                return total, idle
    return None


def parse_proc_meminfo(proc_meminfo_text: str) -> Optional[Tuple[int, int]]:
    """Parse /proc/meminfo and return (total_bytes, used_bytes)."""
    info: dict[str, int] = {}
    for line in proc_meminfo_text.splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        parts = rest.strip().split()
        if not parts:
            continue
        try:
            val_kb = int(parts[0])
        except ValueError:
            continue
        info[key.strip()] = val_kb * 1024

    total = info.get("MemTotal", 0)
    if total <= 0:
        return None

    if "MemAvailable" in info:
        avail = info["MemAvailable"]
    else:
        free = info.get("MemFree", 0)
        buffers = info.get("Buffers", 0)
        cached = info.get("Cached", 0)
        sreclaimable = info.get("SReclaimable", 0)
        shmem = info.get("Shmem", 0)
        avail = max(0, free + buffers + cached + sreclaimable - shmem)

    used = max(0, min(total, total - avail))
    return total, used


@dataclass(frozen=True)
class SystemResourceStatus:
    """Snapshot of system CPU and RAM utilization for overload monitoring."""

    cpu_percent: float = 0.0
    ram_used_bytes: int = 0
    ram_total_bytes: int = 0
    ram_percent: float = 0.0

    @property
    def ram_free_bytes(self) -> int:
        return max(0, self.ram_total_bytes - self.ram_used_bytes)

    @property
    def is_critical(self) -> bool:
        return (
            self.cpu_percent >= CPU_CRITICAL_PERCENT
            or self.ram_percent >= RAM_CRITICAL_PERCENT
        )

    @property
    def is_warning(self) -> bool:
        return not self.is_critical and (
            self.cpu_percent >= CPU_WARN_PERCENT
            or self.ram_percent >= RAM_WARN_PERCENT
        )

    @property
    def level(self) -> str:
        if self.is_critical:
            return "critical"
        if self.is_warning:
            return "warning"
        return "ok"

    @property
    def formatted_ram_used(self) -> str:
        return _format_ram_bytes_compact(self.ram_used_bytes)

    @property
    def formatted_ram_total(self) -> str:
        return _format_ram_bytes_compact(self.ram_total_bytes)

    def format_bar_text(self) -> str:
        """Format concise telemetry string for the bottom status bar."""
        if self.is_critical:
            prefix = "⚠ OVERLOAD  "
        elif self.is_warning:
            prefix = "▲ HIGH LOAD  "
        else:
            prefix = "● "

        if self.ram_total_bytes > 0:
            ram_str = (
                f"RAM: {self.ram_percent:3.0f}% "
                f"({self.formatted_ram_used}/{self.formatted_ram_total})"
            )
        else:
            ram_str = f"RAM: {self.ram_percent:3.0f}%"

        return f"{prefix}CPU: {self.cpu_percent:3.0f}%   •   {ram_str}"


class SystemResourceMonitor:
    """Lightweight, dependency-free CPU and RAM usage sampler."""

    def __init__(
        self,
        proc_stat_path: Path = Path("/proc/stat"),
        proc_meminfo_path: Path = Path("/proc/meminfo"),
        cpu_times_fn: Optional[Callable[[], Optional[Tuple[int, int]]]] = None,
        mem_info_fn: Optional[Callable[[], Optional[Tuple[int, int]]]] = None,
    ) -> None:
        self._proc_stat_path = proc_stat_path
        self._proc_meminfo_path = proc_meminfo_path
        self._cpu_times_fn = cpu_times_fn
        self._mem_info_fn = mem_info_fn
        self._prev_total: Optional[int] = None
        self._prev_idle: Optional[int] = None
        self._last_cpu_percent: float = 0.0

        # Prime initial CPU counter baseline so the first UI sample has a delta
        init_times = self._read_cpu_times()
        if init_times is not None:
            self._prev_total, self._prev_idle = init_times

    def _read_cpu_times(self) -> Optional[Tuple[int, int]]:
        if self._cpu_times_fn is not None:
            return self._cpu_times_fn()

        # 1. Linux / Alpine /proc/stat
        try:
            if self._proc_stat_path.is_file():
                text = self._proc_stat_path.read_text(encoding="utf-8")
                parsed = parse_proc_stat_cpu(text)
                if parsed is not None:
                    return parsed
        except Exception:
            pass

        # 2. Windows fallback via GetSystemTimes (for cross-platform dev/tests)
        if os.name == "nt":
            try:
                class _FILETIME(ctypes.Structure):
                    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]

                idle_ft = _FILETIME()
                kernel_ft = _FILETIME()
                user_ft = _FILETIME()
                if ctypes.windll.kernel32.GetSystemTimes(  # type: ignore[attr-defined]
                    ctypes.byref(idle_ft),
                    ctypes.byref(kernel_ft),
                    ctypes.byref(user_ft),
                ):
                    idle = (idle_ft.dwHighDateTime << 32) | idle_ft.dwLowDateTime
                    kernel = (kernel_ft.dwHighDateTime << 32) | kernel_ft.dwLowDateTime
                    user = (user_ft.dwHighDateTime << 32) | user_ft.dwLowDateTime
                    total = kernel + user
                    return total, idle
            except Exception:
                pass

        return None

    def _read_mem_info(self) -> Optional[Tuple[int, int]]:
        if self._mem_info_fn is not None:
            return self._mem_info_fn()

        # 1. Linux / Alpine /proc/meminfo
        try:
            if self._proc_meminfo_path.is_file():
                text = self._proc_meminfo_path.read_text(encoding="utf-8")
                parsed = parse_proc_meminfo(text)
                if parsed is not None:
                    return parsed
        except Exception:
            pass

        # 2. Windows fallback via GlobalMemoryStatusEx
        if os.name == "nt":
            try:
                class _MEMORYSTATUSEX(ctypes.Structure):
                    _fields_ = [
                        ("dwLength", ctypes.c_uint32),
                        ("dwMemoryLoad", ctypes.c_uint32),
                        ("ullTotalPhys", ctypes.c_uint64),
                        ("ullAvailPhys", ctypes.c_uint64),
                        ("ullTotalPageFile", ctypes.c_uint64),
                        ("ullAvailPageFile", ctypes.c_uint64),
                        ("ullTotalVirtual", ctypes.c_uint64),
                        ("ullAvailVirtual", ctypes.c_uint64),
                        ("ullAvailExtendedVirtual", ctypes.c_uint64),
                    ]

                stat = _MEMORYSTATUSEX()
                stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
                if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):  # type: ignore[attr-defined]
                    total = int(stat.ullTotalPhys)
                    avail = int(stat.ullAvailPhys)
                    used = max(0, min(total, total - avail))
                    return total, used
            except Exception:
                pass

        return None

    def sample(self) -> SystemResourceStatus:
        """Read current CPU and RAM utilization."""
        cpu_pct = self._last_cpu_percent
        times = self._read_cpu_times()
        if times is not None:
            total, idle = times
            if self._prev_total is not None and self._prev_idle is not None:
                total_delta = total - self._prev_total
                idle_delta = idle - self._prev_idle
                if total_delta > 0:
                    busy_delta = max(0, total_delta - idle_delta)
                    cpu_pct = min(100.0, max(0.0, (busy_delta / total_delta) * 100.0))
                    self._last_cpu_percent = cpu_pct
            self._prev_total = total
            self._prev_idle = idle

        mem = self._read_mem_info()
        if mem is not None and mem[0] > 0:
            total_ram, used_ram = mem
            ram_pct = min(100.0, max(0.0, (used_ram / total_ram) * 100.0))
        else:
            total_ram, used_ram, ram_pct = 0, 0, 0.0

        return SystemResourceStatus(
            cpu_percent=cpu_pct,
            ram_used_bytes=used_ram,
            ram_total_bytes=total_ram,
            ram_percent=ram_pct,
        )


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
