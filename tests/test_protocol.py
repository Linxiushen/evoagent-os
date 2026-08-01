import pytest

from echoweave import protocol
from echoweave.protocol import (
    HEADER,
    UINT32_MAX,
    PacketKind,
    pack_packet,
    unpack_packet,
)


def test_media_packet_round_trip():
    encoded = pack_packet(PacketKind.MIC_PCM16, 7, 1234, b"\x01\x02")
    assert len(encoded) == HEADER.size + 2
    packet = unpack_packet(encoded)
    assert packet.kind is PacketKind.MIC_PCM16
    assert packet.turn_id == 7
    assert packet.pts_ms == 1234
    assert packet.payload == b"\x01\x02"


@pytest.mark.parametrize("bad", [b"", b"bad packet", b"XX\x01\x01" + b"\0" * 8])
def test_media_packet_rejects_invalid_data(bad):
    with pytest.raises(ValueError):
        unpack_packet(bad)


def test_media_packet_accepts_bytes_like_and_uint32_boundaries():
    encoded = pack_packet(
        PacketKind.VIDEO_FRAGMENT,
        UINT32_MAX,
        UINT32_MAX,
        bytearray(b"data"),
    )
    packet = unpack_packet(memoryview(encoded).cast("I"))
    assert packet.turn_id == UINT32_MAX
    assert packet.pts_ms == UINT32_MAX
    assert packet.payload == b"data"


@pytest.mark.parametrize("field", ["turn_id", "pts_ms"])
def test_media_packet_rejects_out_of_range_header_fields(field):
    values = {"turn_id": 1, "pts_ms": 1}
    values[field] = UINT32_MAX + 1
    with pytest.raises(ValueError, match="unsigned 32-bit"):
        pack_packet(PacketKind.MIC_PCM16, payload=b"x", **values)


def test_media_packet_enforces_payload_limit_before_encoding(monkeypatch):
    monkeypatch.setattr(protocol, "MAX_MEDIA_PAYLOAD_BYTES", 2)
    with pytest.raises(ValueError, match="payload exceeds"):
        pack_packet(PacketKind.MIC_PCM16, 1, 1, b"abc")


@pytest.mark.parametrize(
    ("kind", "turn_id", "pts_ms", "payload", "error"),
    [
        (1, 1, 1, b"x", TypeError),
        (PacketKind.MIC_PCM16, True, 1, b"x", TypeError),
        (PacketKind.MIC_PCM16, 1, False, b"x", TypeError),
        (PacketKind.MIC_PCM16, 1, 1, "x", TypeError),
    ],
)
def test_media_packet_rejects_ambiguous_types(kind, turn_id, pts_ms, payload, error):
    with pytest.raises(error):
        pack_packet(kind, turn_id, pts_ms, payload)
