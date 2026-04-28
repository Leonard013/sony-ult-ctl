#!/usr/bin/env python3
"""
sony_ult_ctl.py — Control Sony ULT WEAR headphones from macOS via Bluetooth RFCOMM.

Protocol reverse-engineered via live packet capture from the headphones.

Transport: macOS IOBluetooth RFCOMM (not BLE — Sony uses Bluetooth Classic).

Requirements:
    pip install pyobjc-framework-IOBluetooth pyobjc-framework-Cocoa

Usage:
    python sony_ult_ctl.py anc                # Noise Cancelling ON
    python sony_ult_ctl.py ambient             # Ambient Sound ON
    python sony_ult_ctl.py ambient voice       # Ambient Sound + voice focus
    python sony_ult_ctl.py off                 # NC/Ambient OFF (Normal mode)
    python sony_ult_ctl.py ult1                # ULT bass boost 1
    python sony_ult_ctl.py ult2                # ULT bass boost 2 (deep bass)
    python sony_ult_ctl.py ultoff              # ULT bass OFF
    python sony_ult_ctl.py status              # Query all modes + battery
    python sony_ult_ctl.py battery             # Battery level
    python sony_ult_ctl.py monitor             # Sniff packets (Ctrl-C to stop)
    python sony_ult_ctl.py raw <hex>           # Send arbitrary payload

Note: The ULT WEAR firmware does not support graduated NC or Ambient levels.
NC and Ambient are binary on/off. The only sub-toggle within Ambient is voice focus.
The protocol SET command includes a level byte for compatibility with other Sony
models (e.g. WH-1000XM5), but the ULT WEAR ignores it.
"""

import sys
import struct
import time
import argparse

import objc
from Foundation import NSObject, NSRunLoop, NSDate
import IOBluetooth

# ── Device config ────────────────────────────────────────────────────

DEVICE_MAC = "XX-XX-XX-XX-XX-XX"  # Replace with your headphones' Bluetooth MAC
RFCOMM_CHANNEL = 18

# ── Sony V2 protocol constants ──────────────────────────────────────

SOF = 0x3E
EOF = 0x3C
ESC = 0x3D

DATA_MDR = 0x0C
DATA_ACK = 0x01

# ── Packet framing ──────────────────────────────────────────────────


def _checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def _escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b in (SOF, EOF, ESC):
            out.extend([ESC, b & 0xEF])
        else:
            out.append(b)
    return bytes(out)


def _unescape(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        if data[i] == ESC and i + 1 < len(data):
            out.append(data[i + 1] | 0x10)
            i += 2
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def _build_packet(dtype: int, seq: int, payload: bytes) -> bytes:
    length = struct.pack(">I", len(payload))
    inner = bytes([dtype, seq]) + length + payload
    inner += bytes([_checksum(inner)])
    return bytes([SOF]) + _escape(inner) + bytes([EOF])


def _extract_packets(buf: bytearray) -> list[dict]:
    packets = []
    while True:
        s = buf.find(bytes([SOF]))
        if s == -1:
            buf.clear()
            break
        e = buf.find(bytes([EOF]), s + 1)
        if e == -1:
            del buf[:s]
            break
        inner = _unescape(bytes(buf[s + 1 : e]))
        del buf[: e + 1]
        if len(inner) < 7:
            continue
        dtype = inner[0]
        seq = inner[1]
        plen = struct.unpack(">I", inner[2:6])[0]
        if 6 + plen > len(inner):
            continue
        payload = inner[6 : 6 + plen]
        chk = inner[6 + plen] if 6 + plen < len(inner) else -1
        if _checksum(inner[: 6 + plen]) == chk:
            packets.append({"type": dtype, "seq": seq, "payload": payload})
    return packets


# ── Command payloads ────────────────────────────────────────────────
#
# ANC/Ambient uses AmbientSoundControl2 (inquired type 0x17):
#   SET: [0x68, 0x17, 0x01, enabled, ambient, 0x02, voice_focus, level*]
#   GET: [0x66, 0x17]
#   RET: [0x67, 0x17, 0x01, enabled, ambient, reserved(0x01), voice_focus]
#   * level byte is accepted but ignored by ULT WEAR firmware
#
# ULT uses Equalizer (inquired type 0x03):
#   SET: [0x58, 0x03, eq_preset, ult_mode, num_bands=6, band1..6]
#   GET: [0x56, 0x03]
#   RET: [0x57, 0x03, eq_preset, ult_mode, num_bands=6, band1..6]


def cmd_init():
    return bytes([0x00, 0x00])


def cmd_anc_on():
    return bytes([0x68, 0x17, 0x01, 0x01, 0x00, 0x02, 0x00, 0x00])


def cmd_ambient(voice: bool = False):
    return bytes([0x68, 0x17, 0x01, 0x01, 0x01, 0x02, 0x01 if voice else 0x00, 0x00])


def cmd_off():
    return bytes([0x68, 0x17, 0x01, 0x00, 0x00, 0x02, 0x00, 0x00])


def cmd_anc_get():
    return bytes([0x66, 0x17])


def cmd_ult_set(mode: int):
    return bytes([0x58, 0x03, 0x00, mode, 0x06, 0x0A, 0x0A, 0x0A, 0x0A, 0x0A, 0x0A])


def cmd_ult_get():
    return bytes([0x56, 0x03])


def cmd_battery_get():
    return bytes([0x22, 0x00])


# ── RFCOMM delegate ─────────────────────────────────────────────────


class _RFCOMMDelegate(NSObject):
    def init(self):
        self = objc.super(_RFCOMMDelegate, self).init()
        self.rx = bytearray()
        self.packets: list[dict] = []
        self.connected = False
        self.channel = None
        self.open_error = None
        return self

    def rfcommChannelOpenComplete_status_(self, ch, status):
        if status == 0:
            self.connected = True
            self.channel = ch
        else:
            self.open_error = status

    def rfcommChannelData_data_length_(self, ch, data, length):
        try:
            raw = bytes(data)[:length]
        except Exception:
            try:
                import ctypes
                raw = ctypes.string_at(data, length)
            except Exception:
                return
        self.rx.extend(raw)
        self.packets.extend(_extract_packets(self.rx))

    def rfcommChannelClosed_(self, ch):
        self.connected = False
        self.channel = None


# ── Controller ──────────────────────────────────────────────────────


class SonyController:
    def __init__(self, mac: str = DEVICE_MAC, channel: int = RFCOMM_CHANNEL):
        self.mac = mac
        self.rfcomm_id = channel
        self._dev = None
        self._dlg: _RFCOMMDelegate | None = None
        self._seq = 0

    def connect(self):
        self._dev = IOBluetooth.IOBluetoothDevice.deviceWithAddressString_(self.mac)
        if not self._dev:
            raise RuntimeError(f"Device {self.mac} not found")
        if not self._dev.isConnected():
            raise RuntimeError(
                "Headphones not connected — pair in System Settings > Bluetooth first"
            )

        self._dlg = _RFCOMMDelegate.alloc().init()
        ret = self._dev.openRFCOMMChannelSync_withChannelID_delegate_(
            None, self.rfcomm_id & 0xFF, self._dlg
        )
        io_return = ret[0] if isinstance(ret, tuple) else ret
        if isinstance(ret, tuple) and ret[1] is not None:
            self._dlg.channel = ret[1]

        if io_return != 0:
            raise RuntimeError(f"RFCOMM open failed (IOReturn 0x{io_return:08X})")

        self._pump(3.0, until=lambda: self._dlg.connected or self._dlg.open_error)
        if not self._dlg.connected:
            raise RuntimeError("RFCOMM connection timed out")

    def close(self):
        if self._dlg and self._dlg.channel:
            try:
                self._dlg.channel.closeChannel()
            except Exception:
                pass

    def _pump(self, seconds: float, until=None):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if until and until():
                return
            NSRunLoop.currentRunLoop().runUntilDate_(
                NSDate.dateWithTimeIntervalSinceNow_(0.05)
            )

    def _send_ack(self, received_seq: int):
        ack = _build_packet(DATA_ACK, 1 - received_seq, b"")
        self._dlg.channel.writeSync_length_(ack, len(ack))

    def send(self, payload: bytes, timeout: float = 2.0) -> list[dict]:
        pkt = _build_packet(DATA_MDR, self._seq, payload)
        self._seq = 1 - self._seq
        self._dlg.packets.clear()

        io_ret = self._dlg.channel.writeSync_length_(pkt, len(pkt))
        if io_ret != 0:
            raise RuntimeError(f"Write failed (IOReturn 0x{io_ret:08X})")

        self._pump(timeout, until=lambda: any(
            p["type"] == DATA_MDR for p in self._dlg.packets
        ))
        self._pump(0.15)

        responses = []
        for p in self._dlg.packets:
            if p["type"] == DATA_MDR:
                self._send_ack(p["seq"])
                responses.append(p)
        self._dlg.packets.clear()
        return responses

    def initialize(self) -> str:
        resps = self.send(cmd_init())
        for r in resps:
            if r["type"] == DATA_MDR and r["payload"][:1] == b"\x01":
                return "V2" if len(r["payload"]) >= 8 else "V1"
        return "unknown"

    def set_anc(self):
        self.send(cmd_anc_on())

    def set_ambient(self, voice: bool = False):
        self.send(cmd_ambient(voice))

    def set_off(self):
        self.send(cmd_off())

    def set_ult(self, mode: int):
        self.send(cmd_ult_set(mode))

    def get_anc_status(self) -> dict:
        resps = self.send(cmd_anc_get())
        for r in resps:
            p = r["payload"]
            if len(p) >= 7 and p[0] == 0x67:
                return {
                    "enabled": bool(p[3]),
                    "ambient": bool(p[4]),
                    "voice_focus": bool(p[6]),
                }
        return {}

    def get_ult_status(self) -> dict:
        resps = self.send(cmd_ult_get())
        for r in resps:
            p = r["payload"]
            if len(p) >= 5 and p[0] == 0x57:
                return {"ult_mode": p[3], "eq_preset": p[2]}
        return {}

    def get_battery(self) -> dict:
        resps = self.send(cmd_battery_get())
        for r in resps:
            p = r["payload"]
            if len(p) >= 4 and p[0] == 0x23:
                return {"level": p[2], "charging": bool(p[3])}
        return {}

    def get_full_status(self) -> str:
        lines = []
        anc = self.get_anc_status()
        if anc:
            if not anc["enabled"]:
                lines.append("Mode:    Normal (OFF)")
            elif anc["ambient"]:
                vf = " + voice focus" if anc["voice_focus"] else ""
                lines.append(f"Mode:    Ambient Sound{vf}")
            else:
                lines.append("Mode:    Noise Cancelling")
        else:
            lines.append("Mode:    (unknown)")

        ult = self.get_ult_status()
        if ult:
            ult_name = {0: "OFF", 1: "ULT 1", 2: "ULT 2"}.get(
                ult["ult_mode"], f"unknown ({ult['ult_mode']})"
            )
            lines.append(f"ULT:     {ult_name}")

        bat = self.get_battery()
        if bat:
            c = " (charging)" if bat["charging"] else ""
            lines.append(f"Battery: {bat['level']}%{c}")

        return "\n".join(lines)

    def monitor(self):
        print("Monitoring — press buttons on headphones to see packets.")
        print("Ctrl-C to stop.\n")
        while True:
            self._pump(0.3)
            while self._dlg.packets:
                pkt = self._dlg.packets.pop(0)
                if pkt["type"] == DATA_MDR:
                    self._send_ack(pkt["seq"])
                _print_packet(pkt)

    def send_raw(self, hex_payload: str) -> list[dict]:
        return self.send(bytes.fromhex(hex_payload.replace(" ", "").replace(":", "")))


# ── Display helpers ─────────────────────────────────────────────────

CMD_NAMES = {
    0x00: "INIT_REQ", 0x01: "INIT_RET",
    0x05: "FW_RET",
    0x23: "BAT_RET", 0x25: "BAT_NOTIFY",
    0x57: "EQ_RET", 0x59: "EQ/ULT_NOTIFY",
    0x67: "ANC_RET", 0x69: "ANC_NOTIFY",
}


def _print_packet(pkt: dict):
    t = pkt["type"]
    p = pkt["payload"]
    dtype = {0x01: "ACK", 0x0C: "DATA", 0x0E: "DATA2"}.get(t, f"0x{t:02X}")
    cmd = CMD_NAMES.get(p[0], "") if p else ""
    label = f"{dtype}/{cmd}" if cmd else dtype
    print(f"  [{label:20s}] seq={pkt['seq']} ({len(p):2d}B): {p.hex(' ')}")


# ── CLI ─────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Control Sony ULT headphones via Bluetooth RFCOMM",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s anc              Noise Cancelling ON
  %(prog)s ambient          Ambient Sound ON
  %(prog)s ambient voice    Ambient Sound + voice focus
  %(prog)s off              Normal mode (NC/Ambient OFF)
  %(prog)s ult1             ULT bass boost 1
  %(prog)s ult2             ULT deep bass 2
  %(prog)s ultoff           ULT bass OFF
  %(prog)s status           Show all modes + battery
""",
    )
    parser.add_argument(
        "command",
        choices=[
            "anc", "ambient", "off",
            "ult1", "ult2", "ultoff",
            "status", "battery", "monitor", "raw",
        ],
    )
    parser.add_argument("args", nargs="*")
    parser.add_argument("--mac", default=DEVICE_MAC)
    parser.add_argument("--channel", type=int, default=RFCOMM_CHANNEL)
    args = parser.parse_args()

    ctl = SonyController(mac=args.mac, channel=args.channel)
    try:
        print(f"Connecting to {ctl.mac} ch{ctl.rfcomm_id}...")
        ctl.connect()
        version = ctl.initialize()
        print(f"Connected ({version})\n")

        match args.command:
            case "anc":
                ctl.set_anc()
                print("Noise Cancelling ON")
            case "ambient":
                voice = any(
                    a.lower() in ("voice", "v", "1", "true", "yes")
                    for a in args.args
                )
                ctl.set_ambient(voice)
                vf = " + voice focus" if voice else ""
                print(f"Ambient Sound{vf}")
            case "off":
                ctl.set_off()
                print("Normal mode (NC/Ambient OFF)")
            case "ult1":
                ctl.set_ult(1)
                print("ULT 1 (bass boost)")
            case "ult2":
                ctl.set_ult(2)
                print("ULT 2 (deep bass)")
            case "ultoff":
                ctl.set_ult(0)
                print("ULT OFF")
            case "status":
                print(ctl.get_full_status())
            case "battery":
                bat = ctl.get_battery()
                if bat:
                    c = " (charging)" if bat["charging"] else ""
                    print(f"Battery: {bat['level']}%{c}")
                else:
                    print("No battery response")
            case "monitor":
                ctl.monitor()
            case "raw":
                if not args.args:
                    print("Usage: raw <hex>")
                    return
                resps = ctl.send_raw(args.args[0])
                for r in resps:
                    _print_packet(r)

    except KeyboardInterrupt:
        print("\nInterrupted.")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
    finally:
        ctl.close()


if __name__ == "__main__":
    main()
