from __future__ import annotations

import hashlib
import io
import json
import struct
import wave

import pytest

from app.domains.session_attachments import SessionAttachmentError, SessionAttachmentService


class Sessions:
    def __init__(self):
        self.active = {"one", "two"}

    def get_session_or_none(self, *, session_id):
        return object() if session_id in self.active else None


@pytest.fixture
def service(tmp_path):
    return SessionAttachmentService(tmp_path, Sessions())


def wav_bytes(*, frames: int = 8000, sample_rate: int = 8000, channels: int = 1) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(b"\0\0" * frames * channels)
    return output.getvalue()


def mp3_bytes(*, frames: int = 3) -> bytes:
    # MPEG-1 Layer III, 128 kbps, 44.1 kHz, stereo. Frame length is 417 bytes.
    frame = b"\xff\xfb\x90\x00" + bytes(413)
    return frame * frames


def upload(service, raw: bytes, filename: str, media_type: str, *, session_id="one"):
    return service.upload(
        session_id=session_id,
        filename=filename,
        media_type=media_type,
        raw=raw,
    )


@pytest.mark.parametrize(
    ("raw", "filename", "media_type", "audio_format", "sample_rate", "channels", "duration_ms"),
    [
        (wav_bytes(), "voice.wav", "audio/wav", "wav", 8000, 1, 1000),
        (mp3_bytes(), "song.mp3", "audio/mpeg", "mp3", 44100, 2, 78),
    ],
)
def test_audio_round_trip(service, raw, filename, media_type, audio_format, sample_rate, channels, duration_ms):
    metadata = upload(service, raw, filename, media_type)
    assert metadata["kind"] == "audio"
    assert metadata["media_type"] == media_type
    assert metadata["audio_format"] == audio_format
    assert metadata["sample_rate"] == sample_rate
    assert metadata["channels"] == channels
    assert metadata["duration_ms"] == duration_ms
    assert metadata["size_bytes"] == len(raw)
    assert metadata["sha256"] == hashlib.sha256(raw).hexdigest()
    assert service.get(session_id="one", attachment_id=metadata["attachment_id"]) == metadata
    assert service.read_file(session_id="one", attachment_id=metadata["attachment_id"]) == (metadata, raw)


@pytest.mark.parametrize(
    ("raw", "filename", "media_type", "status"),
    [
        (b"", "empty.wav", "audio/wav", 422),
        (b"not audio", "broken.wav", "audio/wav", 422),
        (wav_bytes(), "disguised.mp3", "audio/mpeg", 422),
        (mp3_bytes()[:-1], "truncated.mp3", "audio/mpeg", 422),
        (b"\xff\xfb\x00\x00", "fake.mp3", "audio/mpeg", 422),
        (wav_bytes(), "wrong.wav", "audio/mpeg", 415),
        (mp3_bytes(), "wrong.mp3", "audio/wav", 415),
        (b"not audio", "broken.m4a", "audio/mp4", 422),
        (b"not audio", "unsupported.aac", "audio/mp4", 415),
    ],
)
def test_invalid_audio_is_rejected(service, raw, filename, media_type, status):
    with pytest.raises(SessionAttachmentError) as error:
        upload(service, raw, filename, media_type)
    assert error.value.status_code == status
    assert not service.root.exists()


def test_audio_attachment_source_cap_is_256_mib(service):
    from app.domains.session_attachments import MAX_AUDIO_ATTACHMENT_BYTES, attachment_type
    assert MAX_AUDIO_ATTACHMENT_BYTES == 256 * 1024 * 1024
    assert attachment_type("voice.wav", "audio/wav")[2] == MAX_AUDIO_ATTACHMENT_BYTES


def test_audio_upload_size_check_uses_attachment_cap(service, monkeypatch):
    monkeypatch.setattr("app.domains.session_attachments.MAX_AUDIO_ATTACHMENT_BYTES", 16)
    with pytest.raises(SessionAttachmentError) as error:
        upload(service, b"x" * 17, "large.wav", "audio/wav")
    assert error.value.status_code == 413


def test_large_wav_is_compressed_without_losing_its_duration_or_original(service, tmp_path):
    raw_path = tmp_path / "long.wav"
    with wave.open(str(raw_path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        silence = bytes(1024 * 1024)
        remaining = 16000 * 2 * 510
        while remaining >= len(silence):
            stream.writeframesraw(silence)
            remaining -= len(silence)
        if remaining:
            stream.writeframesraw(bytes(remaining))
    raw = raw_path.read_bytes()
    assert len(raw) > 12 * 1024 * 1024

    metadata = upload(service, raw, "long.wav", "audio/wav")
    assert metadata["source_format"] == "wav"
    assert metadata["audio_format"] == "mp3"
    assert metadata["duration_ms"] == 510000
    assert metadata["input_size_bytes"] <= 12 * 1024 * 1024
    assert service.read_file(session_id="one", attachment_id=metadata["attachment_id"])[1] == raw
    read_metadata, model_audio, _preview = service.read_input(
        session_id="one", attachment_id=metadata["attachment_id"],
    )
    assert read_metadata == metadata
    assert len(model_audio) == metadata["input_size_bytes"]


def test_audio_attachment_is_session_scoped_and_raw_tampering_is_detected(service):
    raw = wav_bytes()
    metadata = upload(service, raw, "voice.wav", "audio/wav")
    attachment_id = metadata["attachment_id"]
    with pytest.raises(KeyError):
        service.read_file(session_id="two", attachment_id=attachment_id)

    storage_path = service.root / hashlib.sha256(b"one").hexdigest() / f"{attachment_id}.audio"
    tampered = bytearray(raw)
    tampered[-1] ^= 1
    storage_path.write_bytes(tampered)
    with pytest.raises(KeyError):
        service.read_file(session_id="one", attachment_id=attachment_id)


def test_audio_metadata_tampering_is_detected(service):
    metadata = upload(service, mp3_bytes(), "song.mp3", "audio/mpeg")
    attachment_id = metadata["attachment_id"]
    directory = service.root / hashlib.sha256(b"one").hexdigest()
    metadata_path = directory / f"{attachment_id}.json"
    altered = json.loads(metadata_path.read_text())
    altered["duration_ms"] += 1
    metadata_path.write_text(json.dumps(altered))
    with pytest.raises(KeyError):
        service.get(session_id="one", attachment_id=attachment_id)


@pytest.mark.parametrize("media_type", ["", "application/octet-stream", "audio/x-wav"])
def test_audio_upload_normalizes_generic_and_alias_mime(service, media_type):
    metadata = upload(service, wav_bytes(), "voice.wav", media_type)
    assert metadata["media_type"] == "audio/wav"
    assert service.get(session_id="one", attachment_id=metadata["attachment_id"]) == metadata


def test_wav_rejects_inconsistent_byte_rate(service):
    raw = bytearray(wav_bytes())
    struct.pack_into("<I", raw, 28, 1)
    with pytest.raises(SessionAttachmentError, match="encoding"):
        upload(service, bytes(raw), "voice.wav", "audio/wav")
