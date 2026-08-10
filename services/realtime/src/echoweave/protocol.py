from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

MAGIC = b"EW"
VERSION = 1
HEADER = struct.Struct("<2sBBII")
UINT32_MAX = (1 << 32) - 1
# Keeps malformed peers from turning a single decode into an unbounded allocation.
# Application-level limits may be stricter (microphone frames normally are).
MAX_MEDIA_PAYLOAD_BYTES = 32 * 1024 * 1024
MAX_MEDIA_PACKET_BYTES = HEADER.size + MAX_MEDIA_PAYLOAD_BYTES


class PacketKind(IntEnum):
    MIC_PCM16 = 1
    TTS_PCM16 = 2
    VIDEO_FRAGMENT = 3


@dataclass(frozen=True, slots=True)
class MediaPacket:
    kind: PacketKind
    turn_id: int
    pts_ms: int
    payload: bytes


def pack_packet(
    kind: PacketKind,
    turn_id: int,
    pts_ms: int,
    payload: bytes | bytearray | memoryview,
) -> bytes:
    if not isinstance(kind, PacketKind):
        raise TypeError("kind must be a PacketKind")
    if isinstance(turn_id, bool) or not isinstance(turn_id, int):
        raise TypeError("turn_id must be an integer")
    if isinstance(pts_ms, bool) or not isinstance(pts_ms, int):
        raise TypeError("pts_ms must be an integer")
    if not 0 <= turn_id <= UINT32_MAX or not 0 <= pts_ms <= UINT32_MAX:
        raise ValueError("turn_id and pts_ms must fit in unsigned 32-bit fields")
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("payload must be bytes-like")
    payload_bytes = bytes(payload)
    if len(payload_bytes) > MAX_MEDIA_PAYLOAD_BYTES:
        raise ValueError("media packet payload exceeds the protocol limit")
    return HEADER.pack(MAGIC, VERSION, int(kind), turn_id, pts_ms) + payload_bytes


def unpack_packet(data: bytes | bytearray | memoryview) -> MediaPacket:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("media packet must be bytes-like")
    view = memoryview(data)
    if view.nbytes < HEADER.size:
        raise ValueError("media packet is shorter than the 12-byte header")
    if view.nbytes > MAX_MEDIA_PACKET_BYTES:
        raise ValueError("media packet exceeds the protocol limit")
    packet_bytes = data if isinstance(data, bytes) else bytes(view)
    magic, version, raw_kind, turn_id, pts_ms = HEADER.unpack_from(packet_bytes)
    if magic != MAGIC:
        raise ValueError("invalid EchoWeave media packet magic")
    if version != VERSION:
        raise ValueError(f"unsupported EchoWeave media protocol version: {version}")
    try:
        kind = PacketKind(raw_kind)
    except ValueError as exc:
        raise ValueError(f"unknown media packet kind: {raw_kind}") from exc
    return MediaPacket(kind, turn_id, pts_ms, packet_bytes[HEADER.size :])
