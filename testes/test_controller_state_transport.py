from __future__ import annotations

import struct

import controller_state_transport as transport


class _MemoryMapping:
    def __init__(self, size: int) -> None:
        self.buffer = bytearray(size)
        self.position = 0
        self.closed = False

    def seek(self, position: int) -> None:
        self.position = int(position)

    def write(self, payload: bytes) -> int:
        end = self.position + len(payload)
        if end > len(self.buffer):
            raise ValueError("mapping is too small")
        self.buffer[self.position : end] = payload
        self.position = end
        return len(payload)

    def read(self, size: int) -> bytes:
        end = min(len(self.buffer), self.position + int(size))
        payload = bytes(self.buffer[self.position : end])
        self.position = end
        return payload

    def close(self) -> None:
        self.closed = True


def _rows(control_count: int):
    return (
        tuple(0.0 for _ in range(control_count)),
        tuple(0.25 for _ in range(control_count)),
        tuple(1.0 for _ in range(control_count)),
    )


def test_channel_transports_writer_identity_presence_and_generations():
    writer = transport.ControllerStateChannel(2)
    reader = transport.ControllerStateChannel(2)
    mapping = _MemoryMapping(writer.size)
    writer._mapping = mapping
    reader._mapping = mapping

    assert writer.publish(
        _rows(2),
        connected=(True, False, True),
        generations=(11, 0, 29),
    )
    assert reader.read() == _rows(2)
    assert reader.sample_writer_id == writer._writer_instance_id
    assert reader.sample_connected_mask == 0b101
    assert reader.sample_generations == (11, 0, 29)


def test_channel_distinguishes_a_writer_restart_even_when_sequence_repeats():
    first_writer = transport.ControllerStateChannel(1)
    second_writer = transport.ControllerStateChannel(1)
    reader = transport.ControllerStateChannel(1)
    mapping = _MemoryMapping(first_writer.size)
    first_writer._mapping = mapping
    second_writer._mapping = mapping
    reader._mapping = mapping

    assert first_writer.publish(_rows(1), generations=(1, 1, 1))
    assert reader.read() == _rows(1)
    first_id = reader.sample_writer_id
    assert first_writer._sequence == 2

    assert second_writer.publish(_rows(1), generations=(1, 1, 1))
    assert second_writer._sequence == 2
    assert reader.read() == _rows(1)
    assert reader.sample_writer_id == second_writer._writer_instance_id
    assert reader.sample_writer_id != first_id


def test_channel_drops_a_structurally_incompatible_named_mapping():
    reader = transport.ControllerStateChannel(1)
    mapping = _MemoryMapping(reader.size)
    mapping.write(
        struct.pack(
            "<III",
            transport.MAGIC,
            transport.FORMAT_VERSION + 1,
            2,
        )
    )
    reader._mapping = mapping

    assert reader.read() is None
    assert reader._mapping is None
    assert mapping.closed
    assert reader.sample_writer_id is None
    assert reader.sample_connected_mask == 0
    assert reader.sample_generations == (0, 0, 0)


def test_channel_crc_rejects_a_torn_payload_with_a_stable_sequence():
    writer = transport.ControllerStateChannel(2)
    reader = transport.ControllerStateChannel(2)
    mapping = _MemoryMapping(writer.size)
    writer._mapping = mapping
    reader._mapping = mapping
    assert writer.publish(_rows(2), connected=(True, True, True))

    mapping.buffer[-1] ^= 0x01

    assert reader.read() is None
    assert reader._mapping is None
    assert mapping.closed
    assert reader.sample_writer_id is None


def test_channel_never_exposes_an_in_progress_odd_sequence():
    writer = transport.ControllerStateChannel(1)
    reader = transport.ControllerStateChannel(1)
    mapping = _MemoryMapping(writer.size)
    writer._mapping = mapping
    reader._mapping = mapping
    assert writer.publish(_rows(1))
    committed_sequence = writer._sequence
    struct.pack_into("<I", mapping.buffer, transport._SEQUENCE_OFFSET, 3)

    assert reader.read() is None
    assert reader._mapping is mapping

    struct.pack_into(
        "<I",
        mapping.buffer,
        transport._SEQUENCE_OFFSET,
        committed_sequence,
    )
    assert reader.read() == _rows(1)


def test_channel_rejects_invalid_connection_and_generation_shapes():
    channel = transport.ControllerStateChannel(1)
    channel._mapping = _MemoryMapping(channel.size)

    try:
        channel.publish(_rows(1), connected=(True, False))
    except ValueError as error:
        assert "connection shape" in str(error)
    else:
        raise AssertionError("invalid connection shape was accepted")

    try:
        channel.publish(_rows(1), generations=(1, -1, 2))
    except ValueError as error:
        assert "generation shape" in str(error)
    else:
        raise AssertionError("invalid generation shape was accepted")
