# sony-ult-ctl

Control Sony ULT WEAR (WH-ULT900N) headphones from macOS via command line.

Reverse-engineered the Sony V2 Bluetooth RFCOMM protocol to toggle Noise Cancelling, Ambient Sound, Voice Focus, and ULT bass boost modes — no Sony Headphones Connect app needed.

## Features

| Command | Description |
|---------|-------------|
| `sony_ult_ctl.py anc` | Noise Cancelling ON |
| `sony_ult_ctl.py ambient` | Ambient Sound ON |
| `sony_ult_ctl.py ambient voice` | Ambient Sound + Voice Focus |
| `sony_ult_ctl.py off` | Normal mode (NC/Ambient OFF) |
| `sony_ult_ctl.py ult1` | ULT bass boost 1 |
| `sony_ult_ctl.py ult2` | ULT bass boost 2 (deep bass) |
| `sony_ult_ctl.py ultoff` | ULT bass OFF |
| `sony_ult_ctl.py status` | Show current mode + ULT + battery |
| `sony_ult_ctl.py battery` | Battery level |
| `sony_ult_ctl.py monitor` | Live packet sniffer (Ctrl-C to stop) |
| `sony_ult_ctl.py raw <hex>` | Send arbitrary payload bytes |

## Requirements

- macOS (uses IOBluetooth RFCOMM — not BLE)
- Python 3.10+
- Sony ULT WEAR paired and connected via System Settings > Bluetooth

```bash
pip install pyobjc-framework-IOBluetooth pyobjc-framework-Cocoa
```

## Setup

Edit `DEVICE_MAC` in `sony_ult_ctl.py` to match your headphones' Bluetooth address. Find it in System Settings > Bluetooth > hover over device, or:

```bash
system_profiler SPBluetoothDataType | grep -A2 "ULT WEAR"
```

## Usage

```bash
# Check current state
python sony_ult_ctl.py status
# Mode:    Noise Cancelling
# ULT:     ULT 1
# Battery: 60%

# Switch modes
python sony_ult_ctl.py ambient voice
python sony_ult_ctl.py anc
python sony_ult_ctl.py off

# ULT bass
python sony_ult_ctl.py ult2
python sony_ult_ctl.py ultoff

# Monitor live packets (press buttons on headphones to see what they send)
python sony_ult_ctl.py monitor
```

## Protocol

Sony ULT WEAR uses **Bluetooth Classic RFCOMM** (channel 18) with the Sony V2 protocol (UUID `956C7B26-D49A-4BA8-B03F-B17D393CB6E2`).

### Packet framing

```
[0x3E start] [escaped inner data] [0x3C end]

Inner: [data_type:1B] [seq:1B] [length:4B BE] [payload:NB] [checksum:1B]

Escape: bytes 0x3C/0x3D/0x3E → [0x3D, byte & 0xEF]
Checksum: sum(all inner bytes before checksum) & 0xFF
```

Data types: `0x0C` = DATA_MDR (commands/responses), `0x01` = ACK.

Every DATA_MDR from the device **must be ACK'd** with `type=0x01, seq=1-received_seq`, or the device endlessly retransmits.

### ANC / Ambient Sound (AmbientSoundControl2, inquired type 0x17)

```
SET: [0x68, 0x17, 0x01, enabled, ambient, 0x02, voice_focus, 0x00]
GET: [0x66, 0x17]
RET: [0x67, 0x17, 0x01, enabled, ambient, reserved, voice_focus]
```

| Mode | enabled | ambient | voice_focus |
|------|---------|---------|-------------|
| Noise Cancelling | 1 | 0 | 0 |
| Ambient Sound | 1 | 1 | 0 |
| Ambient + Voice Focus | 1 | 1 | 1 |
| Normal (OFF) | 0 | 0 | 0 |

The protocol includes a level byte (last byte of SET) for models with graduated NC/Ambient levels (e.g. WH-1000XM5), but the **ULT WEAR ignores it** — NC and Ambient are binary on/off.

### ULT Bass Boost (Equalizer system, inquired type 0x03)

ULT mode is multiplexed into the Equalizer system — not at a dedicated feature ID.

```
SET: [0x58, 0x03, 0x00, ult_mode, 0x06, 0x0A, 0x0A, 0x0A, 0x0A, 0x0A, 0x0A]
GET: [0x56, 0x03]
RET: [0x57, 0x03, eq_preset, ult_mode, 0x06, band1..6]
```

| ult_mode | Effect |
|----------|--------|
| 0 | OFF |
| 1 | ULT 1 (bass boost) |
| 2 | ULT 2 (deep bass) |

### Battery

```
GET: [0x22, 0x00]
RET: [0x23, 0x00, level_percent, charging_bool]
```

### Init handshake

```
SEND: [0x00, 0x00]
RET:  [0x01, 0x00, 0x03, ...] → V2 protocol confirmed
```

## Compatibility

Tested on Sony ULT WEAR (WH-ULT900N) with Airoha chipset. The V2 protocol is shared across many Sony headphones — other models may work with minor adjustments to command payloads. Models with graduated levels (WH-1000XM4/XM5) likely respond to the level byte that the ULT WEAR ignores.

## License

MIT
