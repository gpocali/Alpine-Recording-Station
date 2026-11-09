"""SayoDevice 1x4P (and compatible VID 0x8089 macro keypad) LED status controller.

Synchronizes the 4 hardware button LEDs on a SayoDevice 1x4P keypad with the live
studio transport and microphone state of the Alpine Recording Station:

  - Button 0 [R / Space] : Record / Stop
      * Green (0, 210, 60)       -> Ready to record (audio interface connected)
      * Bright Red (255, 20, 20) -> Actively recording
      * Amber-Red (160, 50, 0)   -> Recording paused
      * Off (0, 0, 0)            -> No audio interface connected
  - Button 1 [P / Pause] : Pause / Resume Recording (or Play/Pause take when idle)
      * Flashing Gold (255, 190, 0) <-> Dim (36, 24, 0) -> Recording paused
      * Armed Warm Dim (64, 42, 0)  -> Recording active (ready to pause)
      * Studio Blue (0, 140, 255)   -> Playing reference take (when idle)
      * Off (0, 0, 0)               -> Idle
  - Button 2 [T]         : Talkback (Momentary Push-to-Talk or Idle Latch)
      * Bright Orange (255, 110, 0) -> Talkback active (local talent or remote producer)
      * Dim Teal (0, 40, 64)        -> Talkback armed / available
      * Off (0, 0, 0)               -> Disabled (paused or no audio interface)
  - Button 3 [B]         : Cough / Privacy Mute
      * Flashing Red (255, 0, 32) <-> Dim (42, 0, 6) -> Microphone Muted
      * Cyan (0, 210, 255)          -> Talkback temporary unmute override active
      * Studio Green (0, 180, 50)   -> Microphone Live (unmuted)
      * Off (0, 0, 0)               -> No audio interface connected

Hardware Communication Methods (Dual-Mode):
  1. Direct USB HID Vendor Reports via `/dev/hidraw*` (VID `0x8089`):
     Supports both Sayo HID Protocol v1 (`Report ID 0x02`, Command `0x07` RGB)
     and Sayo HID Protocol v2 (`Report ID 0x21` / `0x22`, `hid_packet_v2_t`
     with `0x10` Light and `0x11` Palette commands). Updates are written strictly
     to volatile RAM (never sending the `0x04` flash save command) so the flash
     memory is never worn out by real-time indicator updates.
  2. Standard Linux Input `EV_LED` (`0x11`) Events via `/dev/input/event*`:
     Simultaneously updates the keypad's isolated hardware LED indicators
     (`LED_NUML` = Record, `LED_CAPSL` = Pause, `LED_SCROLLL` = Talkback,
     `LED_COMPOSE` / `LED_KANA` = Mute) on the SayoDevice's input node without
     altering X11 Caps Lock or Num Lock state on the primary keyboard.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

SAYODEVICE_VENDOR_ID: int = 0x8089

# Linux <linux/input-event-codes.h> constants
EV_SYN: int = 0x00
SYN_REPORT: int = 0x00
EV_LED: int = 0x11

LED_NUML: int = 0x00      # Button 0: Record
LED_CAPSL: int = 0x01     # Button 1: Pause
LED_SCROLLL: int = 0x02   # Button 2: Talkback
LED_COMPOSE: int = 0x03   # Button 3: Mute
LED_KANA: int = 0x04      # Button 3: Mute (secondary indicator)

# Native Linux struct input_event format: (tv_sec: long, tv_usec: long, type: uint16, code: uint16, value: int32)
INPUT_EVENT_STRUCT = struct.Struct("@llHHi")

RGBColor = Tuple[int, int, int]

# Studio RGB palette for the 4 buttons on SayoDevice 1x4P
COLOR_OFF: RGBColor = (0, 0, 0)
COLOR_RECORD_READY: RGBColor = (0, 210, 60)
COLOR_RECORD_ACTIVE: RGBColor = (255, 20, 20)
COLOR_RECORD_PAUSED: RGBColor = (160, 50, 0)

COLOR_PAUSE_ARMED: RGBColor = (64, 42, 0)
COLOR_PAUSE_BLINK_ON: RGBColor = (255, 190, 0)
COLOR_PAUSE_BLINK_OFF: RGBColor = (36, 24, 0)
COLOR_PLAYBACK_ACTIVE: RGBColor = (0, 140, 255)
COLOR_PLAYBACK_PAUSED: RGBColor = (255, 180, 0)

COLOR_TALKBACK_READY: RGBColor = (0, 40, 64)
COLOR_TALKBACK_ACTIVE: RGBColor = (255, 110, 0)

COLOR_MIC_LIVE: RGBColor = (0, 180, 50)
COLOR_MUTE_BLINK_ON: RGBColor = (255, 0, 32)
COLOR_MUTE_BLINK_OFF: RGBColor = (42, 0, 6)
COLOR_MUTE_TB_OVERRIDE: RGBColor = (0, 210, 255)


@dataclass(frozen=True)
class SayoStudioState:
    """High-level studio transport and microphone state to reflect on the 1x4P."""

    has_device: bool = False
    is_recording: bool = False
    is_record_paused: bool = False
    talkback_active: bool = False
    is_mic_muted: bool = False
    is_talkback_mute_override: bool = False
    is_playing: bool = False
    is_playback_paused: bool = False
    blink_on: bool = True

    def compute_button_colors(self) -> Tuple[RGBColor, RGBColor, RGBColor, RGBColor]:
        """Compute the 4 RGB button colors (Record, Pause, Talkback, Mute)."""
        # Button 0: Record / Stop [R or Space]
        if not self.has_device:
            btn0 = COLOR_OFF
        elif self.is_recording:
            btn0 = COLOR_RECORD_PAUSED if self.is_record_paused else COLOR_RECORD_ACTIVE
        else:
            btn0 = COLOR_RECORD_READY

        # Button 1: Pause / Resume [P or Pause]
        if self.is_recording:
            if self.is_record_paused:
                btn1 = COLOR_PAUSE_BLINK_ON if self.blink_on else COLOR_PAUSE_BLINK_OFF
            else:
                btn1 = COLOR_PAUSE_ARMED
        elif self.is_playing:
            btn1 = COLOR_PLAYBACK_PAUSED if self.is_playback_paused else COLOR_PLAYBACK_ACTIVE
        else:
            btn1 = COLOR_OFF

        # Button 2: Talkback [T]
        if not self.has_device or self.is_record_paused:
            btn2 = COLOR_OFF
        elif self.talkback_active:
            btn2 = COLOR_TALKBACK_ACTIVE
        else:
            btn2 = COLOR_TALKBACK_READY

        # Button 3: Cough / Privacy Mute [B]
        if not self.has_device:
            btn3 = COLOR_OFF
        elif self.is_talkback_mute_override:
            btn3 = COLOR_MUTE_TB_OVERRIDE
        elif self.is_mic_muted:
            btn3 = COLOR_MUTE_BLINK_ON if self.blink_on else COLOR_MUTE_BLINK_OFF
        else:
            btn3 = COLOR_MIC_LIVE

        return (btn0, btn1, btn2, btn3)

    def compute_indicator_bits(self) -> Tuple[bool, bool, bool, bool]:
        """Compute 4 boolean indicator states (Record, Pause, Talkback, Mute) for EV_LED."""
        rec_led = bool(self.has_device and self.is_recording and not self.is_record_paused)
        pause_led = bool(
            (self.is_recording and self.is_record_paused and self.blink_on)
            or (not self.is_recording and self.is_playing and not self.is_playback_paused)
        )
        tb_led = bool(self.has_device and not self.is_record_paused and self.talkback_active)
        mute_led = bool(
            self.has_device
            and self.is_mic_muted
            and not self.is_talkback_mute_override
            and self.blink_on
        )
        return (rec_led, pause_led, tb_led, mute_led)


@dataclass
class SayoHidrawNode:
    """Metadata for a discovered SayoDevice `/dev/hidraw*` interface."""

    dev_path: Path
    vendor_id: int
    product_id: int
    product_name: str = "SayoDevice"
    usage_pages: Tuple[int, ...] = field(default_factory=tuple)
    report_ids: Tuple[int, ...] = field(default_factory=tuple)

    @property
    def is_vendor_config_node(self) -> bool:
        """Return True if this hidraw interface carries Sayo vendor configuration reports."""
        for up in self.usage_pages:
            if up in (0xFF00, 0xFF11, 0xFF12) or (up & 0xFF00) == 0xFF00:
                return True
        for rid in self.report_ids:
            if rid in (0x02, 0x21, 0x22):
                return True
        # If descriptor parsing yielded no usage pages (e.g. unreadable sysfs), allow trying it
        return len(self.usage_pages) == 0

    @property
    def prefers_v2_high_speed(self) -> bool:
        return 0xFF12 in self.usage_pages or 0x22 in self.report_ids

    @property
    def prefers_v2_normal(self) -> bool:
        return 0xFF11 in self.usage_pages or 0x21 in self.report_ids

    @property
    def prefers_v1(self) -> bool:
        return 0xFF00 in self.usage_pages or 0x02 in self.report_ids


@dataclass
class SayoInputEventNode:
    """Metadata for a discovered SayoDevice `/dev/input/event*` node supporting EV_LED."""

    dev_path: Path
    vendor_id: int
    product_id: int
    product_name: str = "SayoDevice"


def parse_hid_report_descriptor(desc: bytes) -> Tuple[Tuple[int, ...], Tuple[int, ...]]:
    """Extract Usage Pages and Report IDs from a raw USB HID Report Descriptor."""
    usage_pages: List[int] = []
    report_ids: List[int] = []
    idx = 0
    n = len(desc)
    while idx < n:
        prefix = desc[idx]
        if prefix == 0xFE:
            # Long item: [0xFE, data_size, long_item_tag, ...data...]
            if idx + 2 >= n:
                break
            data_len = desc[idx + 1]
            idx += 3 + data_len
            continue

        size_code = prefix & 0x03
        data_len = 4 if size_code == 3 else size_code
        tag_and_type = prefix & 0xFC
        payload = desc[idx + 1 : idx + 1 + data_len]
        if len(payload) < data_len:
            break

        # Usage Page (Global item: type=1, tag=0 -> 0x04)
        if tag_and_type == 0x04 and data_len in (1, 2):
            up = payload[0] if data_len == 1 else (payload[0] | (payload[1] << 8))
            if up not in usage_pages:
                usage_pages.append(up)
        # Report ID (Global item: type=1, tag=8 -> 0x84)
        elif tag_and_type == 0x84 and data_len == 1:
            rid = payload[0]
            if rid not in report_ids:
                report_ids.append(rid)

        idx += 1 + data_len

    return (tuple(usage_pages), tuple(report_ids))


def build_sayo_v1_led_key_packet(
    key_index: int,
    r: int,
    g: int,
    b: int,
    sub_cmd: int = 0x01,
) -> bytes:
    """Build a 64-byte Sayo Protocol v1 (`Report ID 0x02`, `cmd 0x07`) per-key RGB packet.

    Frame layout:
      [0x02, 0x07, data_len, sub_cmd, key_index, R, G, B, checksum, 0x00...]
    where checksum = sum(frame[:3 + data_len]) & 0xFF.
    """
    payload = bytes([
        sub_cmd & 0xFF,
        key_index & 0xFF,
        max(0, min(255, int(r))),
        max(0, min(255, int(g))),
        max(0, min(255, int(b))),
    ])
    header = bytes([0x02, 0x07, len(payload)]) + payload
    checksum = sum(header) & 0xFF
    frame = header + bytes([checksum])
    return frame.ljust(64, b"\x00")


def build_sayo_v1_led_batch_packet(colors: Tuple[RGBColor, ...]) -> bytes:
    """Build a 64-byte Sayo Protocol v1 (`Report ID 0x02`, `cmd 0x07`) multi-key RGB packet."""
    payload_bytes = bytearray([0x00])
    for r, g, b in colors[:16]:
        payload_bytes.extend([
            max(0, min(255, int(r))),
            max(0, min(255, int(g))),
            max(0, min(255, int(b))),
        ])
    header = bytes([0x02, 0x07, len(payload_bytes)]) + bytes(payload_bytes)
    checksum = sum(header) & 0xFF
    frame = header + bytes([checksum])
    return frame[:64].ljust(64, b"\x00")


def compute_sayo_v2_checksum(packet_with_zero_checksum: bytes) -> int:
    """Compute the 16-bit little-endian word sum checksum for a `hid_packet_v2_t` frame."""
    buf = bytearray(packet_with_zero_checksum)
    if len(buf) % 2 != 0:
        buf.append(0)
    buf[2] = 0
    buf[3] = 0
    word_count = len(buf) // 2
    words = struct.unpack(f"<{word_count}H", buf)
    return sum(words) & 0xFFFF


def build_sayo_v2_led_packet(
    colors: Tuple[RGBColor, ...],
    report_id: int = 0x21,
    cmd_id: int = 0x10,
    echo: int = 0x03,
) -> bytes:
    """Build a Sayo Protocol v2 (`hid_packet_v2_t`) RGB lighting packet.

    Structure:
      struct hid_packet_v2_t {
          uint8_t  report_id;  // 0x21 (64-byte) or 0x22 (1024-byte)
          uint8_t  echo;       // 0x03
          uint16_t checksum;   // 16-bit little-endian word sum of packet
          hid_cmd_v2_t cmds[]; // Starting at offset 0x04
      };
      struct hid_cmd_v2_t {
          uint16_t length;     // Total command length including 4-byte header (multiple of 4)
          uint8_t  id;         // 0x10 (Light) or 0x11 (Palette)
          uint8_t  index;      // Key / zone index
          uint8_t  data[...];  // Payload padded to 4-byte boundary
      };
    """
    packet_size = 1024 if report_id == 0x22 else 64
    cmds_buf = bytearray()

    for idx, (r, g, b) in enumerate(colors[:4]):
        # Each per-key command has 4-byte header + 4-byte payload [mode=0x01 (static), R, G, B] = 8 bytes
        cmd_len = 8
        cmd_bytes = struct.pack(
            "<HBBBBBB",
            cmd_len,
            cmd_id & 0xFF,
            idx & 0xFF,
            0x01,  # static color mode
            max(0, min(255, int(r))),
            max(0, min(255, int(g))),
            max(0, min(255, int(b))),
        )
        cmds_buf.extend(cmd_bytes)

    raw_packet = bytearray(packet_size)
    raw_packet[0] = report_id & 0xFF
    raw_packet[1] = echo & 0xFF
    raw_packet[2] = 0x00
    raw_packet[3] = 0x00
    end_idx = min(packet_size, 4 + len(cmds_buf))
    raw_packet[4:end_idx] = cmds_buf[: end_idx - 4]

    checksum = compute_sayo_v2_checksum(raw_packet)
    struct.pack_into("<H", raw_packet, 2, checksum)
    return bytes(raw_packet)


def build_linux_ev_led_events(
    indicators: Tuple[bool, bool, bool, bool],
    timestamp: Optional[float] = None,
) -> bytes:
    """Pack Linux `struct input_event` records for the 4 button states plus `SYN_REPORT`."""
    now = time.time() if timestamp is None else float(timestamp)
    tv_sec = int(now)
    tv_usec = int((now - tv_sec) * 1_000_000)

    rec_on, pause_on, tb_on, mute_on = indicators
    events = [
        (EV_LED, LED_NUML, 1 if rec_on else 0),
        (EV_LED, LED_CAPSL, 1 if pause_on else 0),
        (EV_LED, LED_SCROLLL, 1 if tb_on else 0),
        (EV_LED, LED_COMPOSE, 1 if mute_on else 0),
        (EV_LED, LED_KANA, 1 if mute_on else 0),
        (EV_SYN, SYN_REPORT, 0),
    ]
    buf = bytearray()
    for ev_type, ev_code, ev_val in events:
        buf.extend(INPUT_EVENT_STRUCT.pack(tv_sec, tv_usec, ev_type, ev_code, ev_val))
    return bytes(buf)


def discover_sayo_hidraw_nodes(
    sysfs_root: Path = Path("/sys/class/hidraw"),
    dev_root: Path = Path("/dev"),
    vendor_id: int = SAYODEVICE_VENDOR_ID,
) -> List[SayoHidrawNode]:
    """Scan `/sys/class/hidraw` for connected SayoDevice (`VID 0x8089`) interfaces."""
    nodes: List[SayoHidrawNode] = []
    if not sysfs_root.is_dir():
        return nodes

    hid_id_pattern = re.compile(
        r"^HID_ID=[0-9A-Fa-f]{4}:([0-9A-Fa-f]{8}):([0-9A-Fa-f]{8})\s*$",
        re.MULTILINE,
    )
    hid_name_pattern = re.compile(r"^HID_NAME=(.+)$", re.MULTILINE)

    for entry in sorted(sysfs_root.iterdir(), key=lambda p: p.name):
        uevent_path = entry / "device" / "uevent"
        if not uevent_path.is_file():
            continue
        try:
            uevent_text = uevent_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        m_id = hid_id_pattern.search(uevent_text)
        if not m_id:
            continue
        vid = int(m_id.group(1), 16)
        pid = int(m_id.group(2), 16)
        if vid != vendor_id:
            continue

        m_name = hid_name_pattern.search(uevent_text)
        prod_name = m_name.group(1).strip() if m_name else "SayoDevice"

        desc_path = entry / "device" / "report_descriptor"
        usage_pages: Tuple[int, ...] = ()
        report_ids: Tuple[int, ...] = ()
        if desc_path.is_file():
            try:
                raw_desc = desc_path.read_bytes()
                usage_pages, report_ids = parse_hid_report_descriptor(raw_desc)
            except Exception:
                pass

        dev_node = dev_root / entry.name
        nodes.append(
            SayoHidrawNode(
                dev_path=dev_node,
                vendor_id=vid,
                product_id=pid,
                product_name=prod_name,
                usage_pages=usage_pages,
                report_ids=report_ids,
            )
        )

    return nodes


def discover_sayo_input_event_nodes(
    sysfs_root: Path = Path("/sys/class/input"),
    dev_root: Path = Path("/dev/input"),
    vendor_id: int = SAYODEVICE_VENDOR_ID,
) -> List[SayoInputEventNode]:
    """Scan `/sys/class/input/event*` for SayoDevice nodes that support `EV_LED`."""
    nodes: List[SayoInputEventNode] = []
    if not sysfs_root.is_dir():
        return nodes

    for entry in sorted(sysfs_root.iterdir(), key=lambda p: p.name):
        if not entry.name.startswith("event"):
            continue
        dev_dir = entry / "device"
        vendor_file = dev_dir / "id" / "vendor"
        product_file = dev_dir / "id" / "product"
        if not vendor_file.is_file():
            continue
        try:
            vid = int(vendor_file.read_text(encoding="utf-8").strip(), 16)
        except Exception:
            continue
        if vid != vendor_id:
            continue

        pid = 0
        if product_file.is_file():
            try:
                pid = int(product_file.read_text(encoding="utf-8").strip(), 16)
            except Exception:
                pass

        # Check if capabilities/ev has EV_LED (bit 0x11) enabled if present
        ev_cap_file = dev_dir / "capabilities" / "ev"
        if ev_cap_file.is_file():
            try:
                ev_hex = ev_cap_file.read_text(encoding="utf-8").strip()
                # Capabilities may be space-separated hex words (most significant first)
                ev_mask = int(ev_hex.replace(" ", ""), 16)
                if not (ev_mask & (1 << EV_LED)):
                    continue
            except Exception:
                pass

        name_file = dev_dir / "name"
        prod_name = "SayoDevice"
        if name_file.is_file():
            try:
                prod_name = name_file.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                pass

        nodes.append(
            SayoInputEventNode(
                dev_path=dev_root / entry.name,
                vendor_id=vid,
                product_id=pid,
                product_name=prod_name,
            )
        )

    return nodes


class SayoDeviceLedController:
    """Asynchronous, hotplug-aware LED status controller for SayoDevice 1x4P."""

    def __init__(
        self,
        sysfs_hidraw_root: Path = Path("/sys/class/hidraw"),
        dev_hidraw_root: Path = Path("/dev"),
        sysfs_input_root: Path = Path("/sys/class/input"),
        dev_input_root: Path = Path("/dev/input"),
        vendor_id: int = SAYODEVICE_VENDOR_ID,
        background_worker: bool = True,
        write_fn: Optional[Callable[[Path, bytes], None]] = None,
    ) -> None:
        self.sysfs_hidraw_root = Path(sysfs_hidraw_root)
        self.dev_hidraw_root = Path(dev_hidraw_root)
        self.sysfs_input_root = Path(sysfs_input_root)
        self.dev_input_root = Path(dev_input_root)
        self.vendor_id = int(vendor_id)
        self._write_fn = write_fn or self._default_write_bytes

        self._lock = threading.Lock()
        self._hidraw_nodes: List[SayoHidrawNode] = []
        self._input_nodes: List[SayoInputEventNode] = []
        self._last_probe_mono: float = 0.0
        self._desired_state: Optional[SayoStudioState] = None
        self._last_pushed_colors: Optional[Tuple[RGBColor, RGBColor, RGBColor, RGBColor]] = None
        self._last_pushed_indicators: Optional[Tuple[bool, bool, bool, bool]] = None
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

        self.rescan_devices()

        if background_worker:
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                name="SayoDeviceLedWorker",
                daemon=True,
            )
            self._worker_thread.start()

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return bool(self._hidraw_nodes or self._input_nodes)

    @property
    def device_summary(self) -> str:
        with self._lock:
            if self._hidraw_nodes:
                n = self._hidraw_nodes[0]
                return f"{n.product_name} ({n.vendor_id:04x}:{n.product_id:04x})"
            if self._input_nodes:
                n = self._input_nodes[0]
                return f"{n.product_name} ({n.vendor_id:04x}:{n.product_id:04x})"
            return ""

    def rescan_devices(self) -> bool:
        """Scan sysfs for SayoDevice hidraw and input event nodes. Returns True if connected."""
        hidraw = discover_sayo_hidraw_nodes(
            sysfs_root=self.sysfs_hidraw_root,
            dev_root=self.dev_hidraw_root,
            vendor_id=self.vendor_id,
        )
        inputs = discover_sayo_input_event_nodes(
            sysfs_root=self.sysfs_input_root,
            dev_root=self.dev_input_root,
            vendor_id=self.vendor_id,
        )
        with self._lock:
            prev_paths = (
                tuple(n.dev_path for n in self._hidraw_nodes),
                tuple(n.dev_path for n in self._input_nodes),
            )
            new_paths = (
                tuple(n.dev_path for n in hidraw),
                tuple(n.dev_path for n in inputs),
            )
            self._hidraw_nodes = hidraw
            self._input_nodes = inputs
            self._last_probe_mono = time.monotonic()
            if new_paths != prev_paths:
                # Force re-pushing current LED state on newly connected device
                self._last_pushed_colors = None
                self._last_pushed_indicators = None
                if hidraw or inputs:
                    logger.info(
                        "SayoDevice detected: hidraw=%s, input=%s",
                        [str(n.dev_path) for n in hidraw],
                        [str(n.dev_path) for n in inputs],
                    )
                    self._wake_event.set()
            return bool(hidraw or inputs)

    def update_state(
        self,
        *,
        has_device: bool,
        is_recording: bool,
        is_record_paused: bool,
        talkback_active: bool,
        is_mic_muted: bool,
        is_talkback_mute_override: bool = False,
        is_playing: bool = False,
        is_playback_paused: bool = False,
        blink_on: bool = True,
    ) -> None:
        """Update the target studio state and wake the LED writer only if state changed."""
        new_state = SayoStudioState(
            has_device=bool(has_device),
            is_recording=bool(is_recording),
            is_record_paused=bool(is_record_paused),
            talkback_active=bool(talkback_active),
            is_mic_muted=bool(is_mic_muted),
            is_talkback_mute_override=bool(is_talkback_mute_override),
            is_playing=bool(is_playing),
            is_playback_paused=bool(is_playback_paused),
            blink_on=bool(blink_on),
        )
        new_colors = new_state.compute_button_colors()
        new_indicators = new_state.compute_indicator_bits()

        with self._lock:
            self._desired_state = new_state
            if (
                new_colors == self._last_pushed_colors
                and new_indicators == self._last_pushed_indicators
            ):
                return
            if not self._hidraw_nodes and not self._input_nodes:
                return

        if self._worker_thread is not None:
            self._wake_event.set()
        else:
            self.flush_now()

    def flush_now(self) -> bool:
        """Synchronously push the latest desired state to all discovered SayoDevice nodes."""
        with self._lock:
            state = self._desired_state
            hidraw_nodes = list(self._hidraw_nodes)
            input_nodes = list(self._input_nodes)

        if state is None or (not hidraw_nodes and not input_nodes):
            return False

        colors = state.compute_button_colors()
        indicators = state.compute_indicator_bits()
        any_ok = False
        had_error = False

        # 1. Push per-key RGB packets over vendor /dev/hidraw* nodes
        vendor_nodes = [n for n in hidraw_nodes if n.is_vendor_config_node]
        for node in vendor_nodes:
            try:
                self._write_rgb_to_hidraw_node(node, colors)
                any_ok = True
            except Exception as exc:
                logger.debug("SayoDevice hidraw write failed on %s: %s", node.dev_path, exc)
                had_error = True

        # 2. Push standard EV_LED indicator events over /dev/input/event* nodes
        if input_nodes:
            ev_payload = build_linux_ev_led_events(indicators)
            for inode in input_nodes:
                try:
                    self._write_fn(inode.dev_path, ev_payload)
                    any_ok = True
                except Exception as exc:
                    logger.debug("SayoDevice EV_LED write failed on %s: %s", inode.dev_path, exc)
                    had_error = True

        with self._lock:
            if any_ok and not had_error:
                self._last_pushed_colors = colors
                self._last_pushed_indicators = indicators
            elif had_error:
                # Trigger a device rescan on next opportunity if a node disappeared
                self._last_pushed_colors = None
                self._last_pushed_indicators = None

        return any_ok

    def turn_off_all(self) -> None:
        """Set all 4 button LEDs and lock indicators to OFF (used on clean shutdown)."""
        off_state = SayoStudioState(has_device=False)
        with self._lock:
            self._desired_state = off_state
            self._last_pushed_colors = None
            self._last_pushed_indicators = None
        self.flush_now()

    def close(self) -> None:
        """Stop the background worker thread and turn off keypad LEDs."""
        self._stop_event.set()
        self._wake_event.set()
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=0.5)
        try:
            self.turn_off_all()
        except Exception:
            pass

    def _write_rgb_to_hidraw_node(
        self,
        node: SayoHidrawNode,
        colors: Tuple[RGBColor, RGBColor, RGBColor, RGBColor],
    ) -> None:
        """Transmit Sayo v1 and/or v2 RGB LED packets matching the node's report descriptor."""
        sent = False
        if node.prefers_v2_high_speed:
            pkt_light = build_sayo_v2_led_packet(colors, report_id=0x22, cmd_id=0x10)
            pkt_pal = build_sayo_v2_led_packet(colors, report_id=0x22, cmd_id=0x11)
            self._write_fn(node.dev_path, pkt_light)
            self._write_fn(node.dev_path, pkt_pal)
            sent = True

        if node.prefers_v2_normal:
            pkt_light = build_sayo_v2_led_packet(colors, report_id=0x21, cmd_id=0x10)
            pkt_pal = build_sayo_v2_led_packet(colors, report_id=0x21, cmd_id=0x11)
            self._write_fn(node.dev_path, pkt_light)
            self._write_fn(node.dev_path, pkt_pal)
            sent = True

        if node.prefers_v1 or not sent:
            for idx, (r, g, b) in enumerate(colors):
                pkt = build_sayo_v1_led_key_packet(idx, r, g, b)
                self._write_fn(node.dev_path, pkt)
            batch_pkt = build_sayo_v1_led_batch_packet(colors)
            self._write_fn(node.dev_path, batch_pkt)

    @staticmethod
    def _default_write_bytes(dev_path: Path, data: bytes) -> None:
        flags = os.O_WRONLY | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(str(dev_path), flags)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=1.0)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            try:
                self.flush_now()
            except Exception as exc:
                logger.debug("SayoDevice worker error: %s", exc)


def main(argv: Optional[List[str]] = None) -> int:
    """CLI diagnostic and interactive LED test tool for SayoDevice 1x4P."""
    parser = argparse.ArgumentParser(
        description="SayoDevice 1x4P USB HID & LED Diagnostic Utility"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Cycle through Record, Pause, Talkback, and Mute LED states on the connected SayoDevice.",
    )
    args = parser.parse_args(argv)

    ctrl = SayoDeviceLedController(background_worker=False)
    hidraw_nodes = discover_sayo_hidraw_nodes()
    input_nodes = discover_sayo_input_event_nodes()

    if not hidraw_nodes and not input_nodes:
        print("No SayoDevice (VID 0x8089) currently detected.")
        return 1

    print(f"Detected SayoDevice: {ctrl.device_summary}")
    for hn in hidraw_nodes:
        ups = ", ".join(f"0x{u:04X}" for u in hn.usage_pages) or "none"
        rids = ", ".join(f"0x{r:02X}" for r in hn.report_ids) or "none"
        print(
            f"  [hidraw] {hn.dev_path} | PID=0x{hn.product_id:04X} | "
            f"UsagePages=[{ups}] | ReportIDs=[{rids}] | VendorConfig={hn.is_vendor_config_node}"
        )
    for en in input_nodes:
        print(f"  [input]  {en.dev_path} | PID=0x{en.product_id:04X} | Name='{en.product_name}' (EV_LED)")

    if args.test:
        print("Running 4-button studio LED state test cycle...")
        test_steps = [
            ("1/4 Standby Ready (Record=Green, Mic=Green)", dict(
                has_device=True, is_recording=False, is_record_paused=False,
                talkback_active=False, is_mic_muted=False, blink_on=True,
            )),
            ("2/4 Recording Active (Record=Red, Pause=Armed)", dict(
                has_device=True, is_recording=True, is_record_paused=False,
                talkback_active=False, is_mic_muted=False, blink_on=True,
            )),
            ("3/4 Recording Paused + Talkback (Pause=Gold, Talkback=Orange)", dict(
                has_device=True, is_recording=True, is_record_paused=True,
                talkback_active=False, is_mic_muted=False, blink_on=True,
            )),
            ("4/4 Talkback Active + Mic Muted (Talkback=Orange, Mute=Red)", dict(
                has_device=True, is_recording=False, is_record_paused=False,
                talkback_active=True, is_mic_muted=True, blink_on=True,
            )),
        ]
        for label, kwargs in test_steps:
            print(f"  -> {label}")
            ctrl.update_state(**kwargs)  # type: ignore[arg-type]
            time.sleep(0.8)
        ctrl.update_state(
            has_device=True,
            is_recording=False,
            is_record_paused=False,
            talkback_active=False,
            is_mic_muted=False,
            blink_on=True,
        )
        print("Test complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
