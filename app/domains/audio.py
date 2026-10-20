"""Bounded structural validation for audio uploads without decoding dependencies."""

from __future__ import annotations

import re
import struct
import subprocess
import tempfile
from pathlib import Path

MAX_AUDIO_BYTES = 10 * 1024 * 1024
AUDIO_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
}

_MIME_ALIASES = {
    "audio/wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/x-wav": "audio/wav",
    "audio/mpeg": "audio/mpeg",
    "audio/mp3": "audio/mpeg",
    "audio/x-mpeg": "audio/mpeg",
    "audio/mp4": "audio/mp4",
    "audio/m4a": "audio/mp4",
    "audio/x-m4a": "audio/mp4",
}
MAX_NORMALIZED_AUDIO_BYTES = 12 * 1024 * 1024
MAX_AUDIO_DURATION_MS = 6 * 60 * 1000
_AUDIO_CONVERSION_TIMEOUT_SECONDS = 180


class AudioValidationError(ValueError):
    """Audio bytes are empty, malformed, unsupported, or mislabeled."""


def canonical_audio_mime(media_type: str) -> str:
    try:
        return _MIME_ALIASES[media_type.lower().split(";", 1)[0].strip()]
    except (AttributeError, KeyError) as exc:
        raise AudioValidationError("Unsupported audio media type.") from exc


def _validate_wav(raw: bytes) -> dict:
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise AudioValidationError("Invalid WAV audio.")
    riff_end = struct.unpack_from("<I", raw, 4)[0] + 8
    if riff_end != len(raw):
        raise AudioValidationError("Invalid WAV container length.")

    offset = 12
    fmt = None
    data_size = None
    while offset < riff_end:
        if riff_end - offset < 8:
            raise AudioValidationError("Invalid WAV chunk header.")
        chunk_id = raw[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", raw, offset + 4)[0]
        start = offset + 8
        end = start + chunk_size
        padded_end = end + (chunk_size & 1)
        if end > riff_end or padded_end > riff_end:
            raise AudioValidationError("Invalid WAV chunk size.")
        if chunk_id == b"fmt ":
            if fmt is not None or chunk_size < 16:
                raise AudioValidationError("Invalid WAV format chunk.")
            fmt = list(struct.unpack_from("<HHIIHH", raw, start))
            if fmt[0] == 0xFFFE:
                if chunk_size < 40:
                    raise AudioValidationError("Invalid extensible WAV format chunk.")
                extension_size, valid_bits = struct.unpack_from("<HH", raw, start + 16)
                subformat = raw[start + 24 : start + 40]
                pcm_guid = bytes.fromhex("0100000000001000800000aa00389b71")
                float_guid = bytes.fromhex("0300000000001000800000aa00389b71")
                if (extension_size < 22 or extension_size + 18 > chunk_size
                        or valid_bits < 1 or valid_bits > fmt[5]):
                    raise AudioValidationError("Invalid extensible WAV sample format.")
                if subformat == pcm_guid:
                    fmt[0] = 1
                elif subformat == float_guid:
                    fmt[0] = 3
                else:
                    raise AudioValidationError("Unsupported extensible WAV subformat.")
            fmt = tuple(fmt)
        elif chunk_id == b"data":
            if data_size is not None:
                raise AudioValidationError("Multiple WAV data chunks are unsupported.")
            data_size = chunk_size
        offset = padded_end

    if fmt is None or data_size is None:
        raise AudioValidationError("WAV format or audio data is missing.")
    audio_format, channels, sample_rate, byte_rate, block_align, bits_per_sample = fmt
    expected_align = channels * ((bits_per_sample + 7) // 8)
    valid_pcm_depth = bits_per_sample in {8, 16, 24, 32}
    valid_float_depth = bits_per_sample in {32, 64}
    if (
        audio_format not in {1, 3}
        or channels < 1 or channels > 8
        or sample_rate < 1
        or byte_rate < 1
        or block_align < 1
        or bits_per_sample < 1
        or (audio_format == 1 and not valid_pcm_depth)
        or (audio_format == 3 and not valid_float_depth)
        or block_align != expected_align
        or byte_rate != sample_rate * block_align
        or data_size == 0
        or data_size % block_align
    ):
        raise AudioValidationError("Unsupported or invalid WAV encoding.")
    duration_ms = round(data_size * 1000 / byte_rate)
    if duration_ms < 1:
        raise AudioValidationError("WAV audio is too short.")
    return {
        "audio_format": "wav",
        "duration_ms": duration_ms,
        "sample_rate": sample_rate,
        "channels": channels,
    }


_MPEG_BITRATES = {
    # MPEG-1 Layer III, indexed by bitrate_index.
    3: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    # MPEG-2/2.5 Layer III.
    2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    0: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
}
_MPEG_SAMPLE_RATES = {
    3: (44100, 48000, 32000),
    2: (22050, 24000, 16000),
    0: (11025, 12000, 8000),
}


def _parse_mp3_header(raw: bytes, offset: int):
    if len(raw) - offset < 4:
        return None
    header = int.from_bytes(raw[offset : offset + 4], "big")
    if header >> 21 != 0x7FF:
        return None
    version = (header >> 19) & 0b11
    layer = (header >> 17) & 0b11
    bitrate_index = (header >> 12) & 0b1111
    sample_index = (header >> 10) & 0b11
    if version == 1 or layer != 1 or bitrate_index in {0, 15} or sample_index == 3:
        return None
    rates = _MPEG_BITRATES[version]
    sample_rate = _MPEG_SAMPLE_RATES[version][sample_index]
    bitrate = rates[bitrate_index] * 1000
    padding = (header >> 9) & 1
    frame_length = (144 if version == 3 else 72) * bitrate // sample_rate + padding
    samples_per_frame = 1152 if version == 3 else 576
    channel_mode = (header >> 6) & 0b11
    channels = 1 if channel_mode == 3 else 2
    return frame_length, sample_rate, channels, samples_per_frame


def _id3_audio_offset(raw: bytes) -> int:
    if not raw.startswith(b"ID3"):
        return 0
    if len(raw) < 10:
        raise AudioValidationError("Invalid MP3 ID3 header.")
    size_bytes = raw[6:10]
    if any(byte & 0x80 for byte in size_bytes):
        raise AudioValidationError("Invalid MP3 ID3 size.")
    size = (size_bytes[0] << 21) | (size_bytes[1] << 14) | (size_bytes[2] << 7) | size_bytes[3]
    end = 10 + size + (10 if raw[5] & 0x10 else 0)
    if end > len(raw):
        raise AudioValidationError("Truncated MP3 ID3 tag.")
    return end


def _validate_mp3(raw: bytes) -> dict:
    offset = _id3_audio_offset(raw)
    end_limit = len(raw) - 128 if len(raw) >= 128 and raw[-128:-125] == b"TAG" else len(raw)
    frames = 0
    samples = 0
    first_rate = first_channels = None
    expected_rate = None
    while offset < end_limit:
        parsed = _parse_mp3_header(raw, offset)
        if parsed is None:
            raise AudioValidationError("Invalid or unsupported MP3 frame.")
        frame_length, sample_rate, channels, frame_samples = parsed
        if frame_length < 4 or offset + frame_length > end_limit:
            raise AudioValidationError("Truncated MP3 frame.")
        if expected_rate is not None and sample_rate != expected_rate:
            raise AudioValidationError("MP3 sample rate changes within the stream.")
        first_rate = first_rate or sample_rate
        first_channels = first_channels or channels
        expected_rate = sample_rate
        offset += frame_length
        samples += frame_samples
        frames += 1
    if frames == 0:
        raise AudioValidationError("MP3 audio frames are missing.")
    return {
        "audio_format": "mp3",
        "duration_ms": round(samples * 1000 / first_rate),
        "sample_rate": first_rate,
        "channels": first_channels,
    }


def _validate_mp4_audio_container(raw: bytes) -> None:
    # Require a complete ISO BMFF ftyp box at byte zero; a suffix alone is not
    # sufficient evidence that ffmpeg should parse the input as an MP4 audio file.
    if len(raw) < 16 or raw[4:8] != b"ftyp":
        raise AudioValidationError("Invalid M4A/MP4 audio container header.")
    box_size = int.from_bytes(raw[:4], "big")
    if box_size == 1:
        if len(raw) < 24:
            raise AudioValidationError("Invalid M4A/MP4 file type box.")
        box_size = int.from_bytes(raw[8:16], "big")
        if box_size < 24:
            raise AudioValidationError("Invalid M4A/MP4 file type box.")
    elif box_size < 16:
        raise AudioValidationError("Invalid M4A/MP4 file type box.")
    if box_size > len(raw):
        raise AudioValidationError("Truncated M4A/MP4 file type box.")
    brand_start = 16 if int.from_bytes(raw[:4], "big") != 1 else 24
    if brand_start >= box_size:
        raise AudioValidationError("Invalid M4A/MP4 compatible brands.")


def _convert_audio(
    raw: bytes, *, source_max_bytes: int = MAX_AUDIO_BYTES,
    source_format: str,
) -> tuple[bytes, dict]:
    if len(raw) > source_max_bytes:
        raise AudioValidationError("Audio file exceeds its source size limit.")
    if source_format == "m4a":
        _validate_mp4_audio_container(raw)
    try:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise AudioValidationError("M4A audio conversion is unavailable.") from exc

    try:
        with tempfile.TemporaryDirectory(prefix="lka-audio-") as temp_dir:
            suffix = {"m4a": ".m4a", "wav": ".wav", "mp3": ".mp3"}[source_format]
            source = Path(temp_dir) / ("source" + suffix)
            target = Path(temp_dir) / "normalized.mp3"
            source.write_bytes(raw)
            try:
                subprocess.run(
                    [
                        ffmpeg, "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
                        "-i", str(source), "-map", "0:a:0", "-vn", "-sn", "-dn",
                        "-ac", "2", "-ar", "44100", "-c:a", "libmp3lame",
                        "-b:a", "128k", "-fs", str(MAX_NORMALIZED_AUDIO_BYTES + 1),
                        "-f", "mp3", str(target),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd=temp_dir,
                    timeout=_AUDIO_CONVERSION_TIMEOUT_SECONDS,
                    check=True,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise AudioValidationError("Invalid or unconvertible M4A audio.") from exc
            if not target.is_file():
                raise AudioValidationError("Audio conversion did not produce output.")
            if target.stat().st_size > MAX_NORMALIZED_AUDIO_BYTES:
                raise AudioValidationError("Compressed audio exceeds the 12 MiB model input limit.")
            normalized = target.read_bytes()
    except AudioValidationError:
        raise
    except OSError as exc:
        raise AudioValidationError("M4A audio conversion failed.") from exc

    normalized_metadata = _validate_mp3(normalized)
    source_metadata = (
        _probe_m4a(raw, source_max_bytes=source_max_bytes)
        if source_format == "m4a" else
        (_validate_wav(raw) if source_format == "wav" else _validate_mp3(raw))
    )
    if abs(normalized_metadata["duration_ms"] - source_metadata["duration_ms"]) > 2000:
        raise AudioValidationError("Audio conversion did not preserve the complete recording.")
    return normalized, {
        **normalized_metadata,
        "source_format": source_format,
        "duration_ms": source_metadata["duration_ms"],
    }


def _probe_m4a(raw: bytes, *, source_max_bytes: int) -> dict:
    """Read source duration without decoding or imposing an output format."""
    _validate_mp4_audio_container(raw)
    try:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise AudioValidationError("M4A audio conversion is unavailable.") from exc
    try:
        with tempfile.TemporaryDirectory(prefix="lka-audio-probe-") as temp_dir:
            source = Path(temp_dir) / "source.m4a"
            source.write_bytes(raw)
            result = subprocess.run(
                [ffmpeg, "-nostdin", "-i", str(source)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, cwd=temp_dir, timeout=20, text=True, check=False,
            )
            match = re.search(r"Duration: (\d{2}):(\d{2}):(\d{2}(?:\.\d+)?)", result.stderr)
            if not match:
                raise ValueError("M4A duration not found")
            duration = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise AudioValidationError("Invalid or unconvertible M4A audio.") from exc
    if not 0 < duration < 24 * 60 * 60:
        raise AudioValidationError("M4A audio duration is invalid or too long.")
    return {"audio_format": "m4a", "duration_ms": round(duration * 1000)}


def _convert_short_m4a_to_wav(
    raw: bytes, *, duration_ms: int, source_max_bytes: int,
) -> tuple[bytes, dict]:
    """Keep the established 16 kHz mono WAV input for short M4A uploads."""
    if len(raw) > source_max_bytes:
        raise AudioValidationError("Audio file exceeds its source size limit.")
    try:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        with tempfile.TemporaryDirectory(prefix="lka-audio-") as temp_dir:
            source = Path(temp_dir) / "source.m4a"
            target = Path(temp_dir) / "normalized.wav"
            source.write_bytes(raw)
            subprocess.run(
                [ffmpeg, "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
                 "-i", str(source), "-map", "0:a:0", "-vn", "-sn", "-dn",
                 "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-fs",
                 str(MAX_NORMALIZED_AUDIO_BYTES + 1), "-f", "wav", str(target)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, cwd=temp_dir,
                timeout=_AUDIO_CONVERSION_TIMEOUT_SECONDS, check=True,
            )
            if not target.is_file() or target.stat().st_size > MAX_NORMALIZED_AUDIO_BYTES:
                raise AudioValidationError("Normalized audio exceeds 12 MiB.")
            normalized = target.read_bytes()
    except AudioValidationError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise AudioValidationError("Invalid or unconvertible M4A audio.") from exc
    metadata = validate_normalized_m4a(normalized)
    if abs(metadata["duration_ms"] - duration_ms) > 2000:
        raise AudioValidationError("Audio conversion did not preserve the complete recording.")
    return normalized, metadata


def validate_audio(
    raw: bytes, media_type: str, *, source_max_bytes: int = MAX_AUDIO_BYTES,
) -> dict:
    """Validate a supported WAV/MP3 byte stream and return bounded audio metadata."""
    if not raw:
        raise AudioValidationError("Audio file is empty.")
    if len(raw) > source_max_bytes:
        raise AudioValidationError("Audio file exceeds its source size limit.")
    canonical_mime = canonical_audio_mime(media_type)
    if canonical_mime == "audio/mp4":
        return _probe_m4a(raw, source_max_bytes=source_max_bytes)
    if raw.startswith(b"RIFF") and raw[8:12] == b"WAVE":
        actual_mime = "audio/wav"
        metadata = _validate_wav(raw)
    else:
        actual_mime = "audio/mpeg"
        metadata = _validate_mp3(raw)
    if canonical_mime != actual_mime:
        raise AudioValidationError("Audio content does not match its media type.")
    return metadata


def validate_normalized_m4a(raw: bytes) -> dict:
    """Validate the legacy provider-ready WAV derived from an uploaded M4A."""
    if not raw or len(raw) > MAX_NORMALIZED_AUDIO_BYTES:
        raise AudioValidationError("Normalized audio exceeds 12 MiB or is empty.")
    metadata = _validate_wav(raw)
    if metadata["sample_rate"] != 16000 or metadata["channels"] != 1:
        raise AudioValidationError("M4A conversion produced an invalid audio format.")
    if metadata["duration_ms"] > MAX_AUDIO_DURATION_MS:
        raise AudioValidationError("Audio exceeds six minutes.")
    return {**metadata, "source_format": "m4a"}


def prepare_audio(
    raw: bytes, media_type: str, *, source_max_bytes: int = MAX_AUDIO_BYTES,
) -> tuple[bytes, dict]:
    """Return provider-ready audio bytes with validated format metadata."""
    if not raw:
        raise AudioValidationError("Audio file is empty.")
    if len(raw) > source_max_bytes:
        raise AudioValidationError("Audio file exceeds its source size limit.")
    canonical_mime = canonical_audio_mime(media_type)
    if canonical_mime == "audio/mp4":
        source_metadata = _probe_m4a(raw, source_max_bytes=source_max_bytes)
        if source_metadata["duration_ms"] <= MAX_AUDIO_DURATION_MS:
            try:
                return _convert_short_m4a_to_wav(
                    raw, duration_ms=source_metadata["duration_ms"],
                    source_max_bytes=source_max_bytes,
                )
            except AudioValidationError as exc:
                if "exceeds 12 MiB" not in str(exc):
                    raise
        return _convert_audio(raw, source_max_bytes=source_max_bytes, source_format="m4a")
    source_format = "wav" if canonical_mime == "audio/wav" else "mp3"
    source_metadata = validate_audio(raw, canonical_mime, source_max_bytes=source_max_bytes)
    if len(raw) <= MAX_NORMALIZED_AUDIO_BYTES:
        return raw, source_metadata
    return _convert_audio(raw, source_max_bytes=source_max_bytes, source_format=source_format)
