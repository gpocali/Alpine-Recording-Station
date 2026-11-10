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
     Implements the official `Sayobot/Sayo_CLI` `O2Protocol::Lighting` (`o2_hid_data`
     `Report ID 0x02`, `cmd 0x07`, `data_len 0x09`, `data.lamp` struct) protocol as
     well as Sayo HID Protocol v2 (`Report ID 0x21` / `0x22`, `hid_packet_v2_t`
     with `0x10` Light and `0x11` Palette commands). By writing `lamp.type = 1`
     (Static RGB) and `lamp.event = 0` (Always Active) directly to each LED slot,
     button colors reflect live software state without relying on the keypad's
     internal two-state lock trigger. Updates are written strictly to volatile RAM
     (never sending the `cmd 0x04` `0x72 0x96` flash save command) so flash memory
     is never worn out by real-time indicator updates.
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
import select
import struct
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

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


@dataclass(frozen=True)
class SayoO2DeviceInfo:
    """Device metadata returned by `O2Protocol::SearchDrivers` (`cmd 0x00`)."""

    mode: str
    version: int
    model_code: int
    support_list: Tuple[int, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SayoO2LampSlot:
    """Per-LED configuration entry returned by `O2Protocol::Lighting` (`cmd 0x07`)."""

    number: int
    code: int
    sub_flags: int
    event: int
    lamp_cmd_len: int
    col_r: int
    col_g: int
    col_b: int
    interval_time: int

    @property
    def lamp_type(self) -> int:
        return (self.code & 0x3F) | (self.sub_flags & 0xC0)

    @property
    def hex_color(self) -> str:
        return f"#{self.col_r:02X}{self.col_g:02X}{self.col_b:02X}"


def build_sayo_o2_handshake_packet(timestamp: Optional[float] = None) -> bytes:
    """Build the 64-byte `O2Protocol::SearchDrivers` (`Report ID 0x02`, `cmd 0x00`) info packet.

    Matches `o2_protocol.cpp` lines 80-104:
      - Byte 0 (`id`):       0x02
      - Byte 1 (`cmd`):      0x00
      - Byte 2 (`data_len`): 0x04
      - Bytes 3..6 (`data`): [Day, Hour, Min, Second]
      - Byte 7 (`checksum`): sum(frame[:7]) & 0xFF
    """
    lt = time.localtime(time.time() if timestamp is None else float(timestamp))
    header = bytes([
        0x02,
        0x00,
        0x04,
        lt.tm_mday & 0xFF,
        lt.tm_hour & 0xFF,
        lt.tm_min & 0xFF,
        lt.tm_sec & 0xFF,
    ])
    checksum = sum(header) & 0xFF
    return (header + bytes([checksum])).ljust(64, b"\x00")


def parse_sayo_o2_handshake_response(frame: bytes) -> Optional[SayoO2DeviceInfo]:
    """Parse a 64-byte `EpIn` response from `O2Protocol::SearchDrivers` (`cmd 0x00`)."""
    if len(frame) < 7 or frame[0] != 0x02:
        return None
    resp_cmd = frame[1]
    data_len = frame[2]
    if resp_cmd == 0x00:
        version = (frame[3] << 8) | frame[4]
        model_code = ((frame[5] << 8) | frame[6]) if data_len > 2 else 0
        support: List[int] = []
        if data_len > 8:
            end_idx = min(len(frame), 3 + data_len)
            support = list(frame[11:end_idx])
        return SayoO2DeviceInfo(
            mode="app",
            version=version,
            model_code=model_code,
            support_list=tuple(support),
        )
    if resp_cmd == 0x02:
        model_code = (frame[5] << 8) | frame[6]
        return SayoO2DeviceInfo(
            mode="bootloader",
            version=0,
            model_code=model_code,
        )
    return None


def build_sayo_v1_led_read_packet(key_index: int) -> bytes:
    """Build a 64-byte `O2Protocol::Lighting("read")` (`Report ID 0x02`, `cmd 0x07`) packet.

    Matches `o2_protocol.cpp` lines 1132-1145:
      - Byte 0 (`id`):          0x02
      - Byte 1 (`cmd`):         0x07 (Lighting)
      - Byte 2 (`data_len`):    0x02
      - Byte 3 (`pattern`):     0x00 (0 = read)
      - Byte 4 (`number`):      key_index (0, 1, 2, 3...)
      - Byte 5 (`OUT_CHECKSUM`): sum(frame[:5]) & 0xFF
    """
    header = bytes([
        0x02,
        0x07,
        0x02,
        0x00,
        key_index & 0xFF,
    ])
    checksum = sum(header) & 0xFF
    return (header + bytes([checksum])).ljust(64, b"\x00")


def parse_sayo_v1_led_response(frame: bytes) -> Optional[SayoO2LampSlot]:
    """Parse a 64-byte `EpIn` response from `O2Protocol::Lighting` (`cmd 0x07`).

    Matches `o2_hid_data.format.data.lamp` in `o2_protocol.h` lines 121-150:
      - Byte 0 (`id`):            0x02
      - Byte 1 (`cmd`):           0x00 (0 = success)
      - Byte 2 (`data_len`):      payload length
      - Byte 3 (`lamp.pattern`):  0 (read) or 1 (write)
      - Byte 4 (`lamp.number`):   LED slot index
      - Byte 5 (`lamp.type`):     (code & 0x3F) | (sub_flags & 0xC0)
      - Byte 6 (`lamp.event`):    event trigger ID
      - Byte 7 (`lamp.lamp_cmd_len`): lamp command length
      - Byte 8 (`col.col_r`):     Red (0..255)
      - Byte 9 (`col.col_g`):     Green (0..255)
      - Byte 10 (`col.col_b`):    Blue (0..255)
      - Byte 11 (`col.interval_time`): interval / speed
    """
    if len(frame) < 12 or frame[1] != 0x00:
        return None
    lamp_type = frame[5]
    return SayoO2LampSlot(
        number=frame[4],
        code=lamp_type & 0x3F,
        sub_flags=lamp_type & 0xC0,
        event=frame[6],
        lamp_cmd_len=frame[7],
        col_r=frame[8],
        col_g=frame[9],
        col_b=frame[10],
        interval_time=frame[11],
    )


def build_sayo_v1_led_key_packet(
    key_index: int,
    r: int,
    g: int,
    b: int,
    pattern: int = 0x01,
    lamp_type: Optional[int] = None,
    event: int = 0x00,
    lamp_cmd_len: int = 0x00,
    interval_time: int = 0x01,
) -> bytes:
    """Build a 64-byte `O2Protocol::Lighting("write")` (`Report ID 0x02`, `cmd 0x07`) packet.

    Matches `o2_hid_data.format.data.lamp` in `Sayobot/Sayo_CLI` (`o2_protocol.h`
    lines 121-150 and `o2_protocol.cpp` lines 1191-1228):
      - Byte 0  (`id`):               0x02 (Report ID)
      - Byte 1  (`cmd`):              0x07 (Lighting command)
      - Byte 2  (`data_len`):         0x09 (9-byte `data.lamp` payload)
      - Byte 3  (`lamp.pattern`):     0x01 (1 = write, 0 = read)
      - Byte 4  (`lamp.number`):      key_index (0, 1, 2, 3...)
      - Byte 5  (`lamp.type`):        (code & 0x3F) | (sub_flags & 0xC0)
                                      Defaults to 0x01 (Static RGB) when color is
                                      non-zero, or 0x00 (Off) when (r, g, b) == (0, 0, 0).
      - Byte 6  (`lamp.event`):       0x00 (Always active / host-driven, bypassing
                                      firmware two-state trigger bugs)
      - Byte 7  (`lamp.lamp_cmd_len`): 0x00
      - Byte 8  (`col.col_r`):        Red (0..255)
      - Byte 9  (`col.col_g`):        Green (0..255)
      - Byte 10 (`col.col_b`):        Blue (0..255)
      - Byte 11 (`col.interval_time`): 0x01 (interval / speed)
      - Byte 12 (`OUT_CHECKSUM`):     sum(frame[:12]) & 0xFF (`EpOut.udata[data_len + 3]`)
      - Bytes 13..63:                 0x00
    """
    clamped_r = max(0, min(255, int(r)))
    clamped_g = max(0, min(255, int(g)))
    clamped_b = max(0, min(255, int(b)))
    if lamp_type is None:
        resolved_type = 0x00 if (clamped_r == 0 and clamped_g == 0 and clamped_b == 0) else 0x01
    else:
        resolved_type = int(lamp_type) & 0xFF

    payload = bytes([
        pattern & 0xFF,
        key_index & 0xFF,
        resolved_type,
        event & 0xFF,
        lamp_cmd_len & 0xFF,
        clamped_r,
        clamped_g,
        clamped_b,
        interval_time & 0xFF,
    ])
    header = bytes([0x02, 0x07, len(payload)]) + payload
    checksum = sum(header) & 0xFF
    frame = header + bytes([checksum])
    return frame.ljust(64, b"\x00")


def build_sayo_v1_led_batch_packet(
    colors: Tuple[RGBColor, ...],
    slot_offset: int = 0,
) -> List[bytes]:
    """Build the 4 `O2Protocol::Lighting` 64-byte write frames for a tuple of button RGB colors."""
    return [
        build_sayo_v1_led_key_packet(slot_offset + idx, r, g, b)
        for idx, (r, g, b) in enumerate(colors[:4])
    ]


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


def query_o2_device_and_lighting(
    dev_path: Path,
    max_slots: int = 8,
    timeout_s: float = 0.05,
) -> Tuple[Optional[SayoO2DeviceInfo], List[SayoO2LampSlot]]:
    """Query `O2Protocol` device info (`cmd 0x00`) and `Lighting` table (`cmd 0x07`) from a hidraw node."""
    info: Optional[SayoO2DeviceInfo] = None
    slots: List[SayoO2LampSlot] = []
    flags = getattr(os, "O_RDWR", 2) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(str(dev_path), flags)
    except Exception:
        return (None, [])

    try:
        # Drain any stale input frames
        while True:
            rlist, _, _ = select.select([fd], [], [], 0.0)
            if not rlist:
                break
            try:
                if not os.read(fd, 64):
                    break
            except Exception:
                break

        # 1. Send O2Protocol::SearchDrivers handshake (cmd = 0x00, data_len = 4)
        os.write(fd, build_sayo_o2_handshake_packet())
        rlist, _, _ = select.select([fd], [], [], timeout_s)
        if rlist:
            resp = os.read(fd, 64)
            info = parse_sayo_o2_handshake_response(resp)

        # 2. Read LED slots sequentially (cmd = 0x07, data_len = 2, pattern = 0, number = 0..max_slots-1)
        for led_num in range(max_slots):
            os.write(fd, build_sayo_v1_led_read_packet(led_num))
            rlist, _, _ = select.select([fd], [], [], timeout_s)
            if not rlist:
                break
            resp = os.read(fd, 64)
            slot = parse_sayo_v1_led_response(resp)
            if slot is None:
                break
            slots.append(slot)
    except Exception as exc:
        logger.debug("SayoDevice O2 query failed on %s: %s", dev_path, exc)
    finally:
        try:
            os.close(fd)
        except Exception:
            pass

    return (info, slots)


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
        self._custom_write_fn = write_fn is not None
        self._write_fn = write_fn or self._default_write_bytes

        self._lock = threading.Lock()
        self._hidraw_nodes: List[SayoHidrawNode] = []
        self._input_nodes: List[SayoInputEventNode] = []
        self._node_slot_offsets: Dict[Path, int] = {}
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
                self._node_slot_offsets.clear()
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

    def _resolve_slot_offset(self, node: SayoHidrawNode) -> int:
        """Determine whether the 4 button LEDs start at `lamp.number = 0` or `1`."""
        env_offset = os.environ.get("ALPINE_RECORDER_SAYO_LED_OFFSET", "").strip()
        if env_offset:
            try:
                return max(0, int(env_offset))
            except ValueError:
                pass

        if self._custom_write_fn:
            return 0

        with self._lock:
            cached = self._node_slot_offsets.get(node.dev_path)
        if cached is not None:
            return cached

        offset = 0
        _, slots = query_o2_device_and_lighting(node.dev_path)
        if len(slots) == 5 and slots[0].event == 0 and any(s.event > 0 for s in slots[1:5]):
            # Slot 0 is global/master lamp and slots 1..4 are Keys 1..4
            offset = 1

        with self._lock:
            self._node_slot_offsets[node.dev_path] = offset
        return offset

    def _write_rgb_to_hidraw_node(
        self,
        node: SayoHidrawNode,
        colors: Tuple[RGBColor, RGBColor, RGBColor, RGBColor],
    ) -> None:
        """Transmit Sayo `O2Protocol::Lighting` v1 and/or v2 RGB LED packets."""
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
            slot_offset = self._resolve_slot_offset(node)
            for pkt in build_sayo_v1_led_batch_packet(colors, slot_offset=slot_offset):
                self._write_fn(node.dev_path, pkt)

    @staticmethod
    def _default_write_bytes(dev_path: Path, data: bytes) -> None:
        nonblock = getattr(os, "O_NONBLOCK", 0)
        is_hidraw = dev_path.name.startswith("hidraw")
        fd = -1
        opened_rw = False
        if is_hidraw:
            try:
                fd = os.open(str(dev_path), getattr(os, "O_RDWR", 2) | nonblock)
                opened_rw = True
            except OSError:
                fd = os.open(str(dev_path), os.O_WRONLY | nonblock)
        else:
            fd = os.open(str(dev_path), os.O_WRONLY | nonblock)

        try:
            os.write(fd, data)
            if opened_rw:
                # Drain the 64-byte EpIn response sent by Sayo O2 firmware (`O2Protocol::Lighting`)
                try:
                    rlist, _, _ = select.select([fd], [], [], 0.015)
                    if rlist:
                        os.read(fd, 64)
                except Exception:
                    pass
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
        if hn.is_vendor_config_node:
            info, slots = query_o2_device_and_lighting(hn.dev_path)
            if info is not None:
                print(
                    f"           O2Protocol Info: mode={info.mode}, "
                    f"firmware_version={info.version}, model_code={info.model_code}"
                )
            if slots:
                print(f"           O2Protocol Lighting Slots ({len(slots)} reported):")
                for s in slots:
                    print(
                        f"             Slot #{s.number}: code={s.code} (type=0x{s.lamp_type:02X}), "
                        f"event={s.event}, color={s.hex_color}, interval={s.interval_time}, "
                        f"cmd_len={s.lamp_cmd_len}"
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
