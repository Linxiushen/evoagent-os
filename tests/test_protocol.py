import pytest

from echoweave.protocol import HEADER, PacketKind, pack_packet, unpack_packet


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
