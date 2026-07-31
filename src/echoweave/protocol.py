from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

MAGIC = b"EW"
VERSION = 1
HEADER = struct.Struct("<2sBBII")


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


def pack_packet(kind: PacketKind, turn_id: int, pts_ms: int, payload: bytes) -> bytes:
    if turn_id < 0 or pts_ms < 0:
        raise ValueError("turn_id and pts_ms must be non-negative")
    return HEADER.pack(MAGIC, VERSION, int(kind), turn_id, pts_ms) + payload


def unpack_packet(data: bytes) -> MediaPacket:
    if len(data) < HEADER.size:
        raise ValueError("media packet is shorter than the 12-byte header")
    magic, version, raw_kind, turn_id, pts_ms = HEADER.unpack_from(data)
    if magic != MAGIC:
        raise ValueError("invalid EchoWeave media packet magic")
    if version != VERSION:
        raise ValueError(f"unsupported EchoWeave media protocol version: {version}")
    try:
        kind = PacketKind(raw_kind)
    except ValueError as exc:
        raise ValueError(f"unknown media packet kind: {raw_kind}") from exc
    return MediaPacket(kind, turn_id, pts_ms, data[HEADER.size :])
