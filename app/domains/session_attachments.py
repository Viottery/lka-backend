"""Validated local image and document attachments, scoped to active sessions."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import warnings
from pathlib import Path
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from app.domains.audio import (
    AUDIO_TYPES,
    MAX_NORMALIZED_AUDIO_BYTES,
    AudioValidationError,
    _validate_mp4_audio_container,
    canonical_audio_mime,
    prepare_audio,
    validate_audio,
    validate_normalized_m4a,
)
from app.domains.file_processing import (
    MAX_EXTRACTED_CHARS,
    FileProcessingError,
    FileProcessor,
    get_file_type_policy,
)
from app.platform.safe_files import (
    is_link_or_reparse,
    open_file_no_follow,
    open_posix_path_no_follow,
)

MAX_ATTACHMENT_BYTES = 256 * 1024 * 1024
MAX_AUDIO_ATTACHMENT_BYTES = MAX_ATTACHMENT_BYTES
MAX_DOCUMENT_BYTES = MAX_ATTACHMENT_BYTES
MAX_TEXT_BYTES = MAX_EXTRACTED_CHARS * 4
MAX_METADATA_BYTES = 64 * 1024
MAX_IMAGE_PIXELS = 4_000_000
IMAGE_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


class SessionAttachmentError(ValueError):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


def _filename(value: str) -> str:
    # The supplied name is display metadata only, never a storage path.
    if not isinstance(value, str):
        raise SessionAttachmentError("Invalid attachment filename.")
    name = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or name in {".", ".."} or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise SessionAttachmentError("Invalid attachment filename.")
    if len(name.encode("utf-8")) > 240:
        raise SessionAttachmentError("Attachment filename is too long.")
    return name


def attachment_type(filename: str, media_type: str) -> tuple[str, str, int]:
    """Select a bounded upload route without reading arbitrary files."""
    filename = _filename(filename)
    suffix = Path(filename).suffix.lower()
    if suffix in AUDIO_TYPES:
        if media_type in {"", "application/octet-stream"}:
            media_type = AUDIO_TYPES[suffix]
        try:
            canonical = canonical_audio_mime(media_type)
        except AudioValidationError as exc:
            raise SessionAttachmentError("Unsupported audio media type.", 415) from exc
        if canonical != AUDIO_TYPES[suffix]:
            raise SessionAttachmentError("Audio media type does not match its filename.", 415)
        return "audio", canonical, MAX_AUDIO_ATTACHMENT_BYTES
    if media_type.lower().startswith("audio/"):
        raise SessionAttachmentError("Unsupported audio file type.", 415)
    if media_type in IMAGE_TYPES.values():
        return "image", media_type, MAX_ATTACHMENT_BYTES
    policy = get_file_type_policy(filename)
    if policy is None or not policy.index_supported:
        raise SessionAttachmentError("Unsupported attachment file type.", 415)
    if media_type not in {"", "application/octet-stream", policy.source_mime_type}:
        raise SessionAttachmentError("Document media type does not match its filename.", 415)
    return "document", policy.source_mime_type, MAX_DOCUMENT_BYTES


def _validate_native_text(raw: bytes) -> None:
    # Match the converter's binary-content conventions for native UTF-8 routes.
    signatures = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"\xd0\xcf\x11\xe0",
                  b"%PDF-", b"\x89PNG", b"\xff\xd8\xff", b"RIFF", b"\x7fELF", b"\x1f\x8b")
    if raw.removeprefix(b"\xef\xbb\xbf").startswith(signatures) or b"\x00" in raw:
        raise SessionAttachmentError("Binary source does not match declared text type.")


def _validate_image(raw: bytes, media_type: str) -> tuple[int, int]:
    if len(raw) > MAX_ATTACHMENT_BYTES:
        raise SessionAttachmentError("Attachment exceeds 256 MiB.", 413)
    if media_type not in IMAGE_TYPES.values():
        raise SessionAttachmentError("Only PNG, JPEG and WebP images are supported.", 415)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if IMAGE_TYPES.get(image.format) != media_type:
                    raise SessionAttachmentError(
                        "Image content does not match its media type.", 415
                    )
                width, height = image.size
                if width * height > MAX_IMAGE_PIXELS:
                    raise SessionAttachmentError("Image exceeds four million pixels.", 413)
                if getattr(image, "n_frames", 1) != 1:
                    raise SessionAttachmentError(
                        "Animated or multi-frame images are not supported."
                    )
                image.verify()
            # verify checks the container; load also checks the decoded pixel data.
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
        return width, height
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise SessionAttachmentError("Image exceeds four million pixels.", 413) from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        if isinstance(exc, SessionAttachmentError):
            raise
        raise SessionAttachmentError("Invalid or corrupt image.") from exc


class SessionAttachmentService:
    def __init__(self, data_dir: Path, session_service):
        self.root = Path(os.path.abspath(data_dir)) / "session_attachments"
        self.session_service = session_service
        self.file_processor = FileProcessor()

    def _require_active_session(self, session_id: str) -> None:
        if self.session_service.get_session_or_none(session_id=session_id) is None:
            raise KeyError("Active session not found.")

    def _session_directory(self, session_id: str) -> Path:
        self._require_active_session(session_id)
        # Session identifiers are never interpreted as paths.
        return self.root / hashlib.sha256(session_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _check_directories(directory: Path) -> None:
        for current in (directory, *directory.parents):
            metadata = current.lstat()
            if is_link_or_reparse(metadata) or not current.is_dir():
                raise OSError("Attachment storage directory is unsafe.")

    @staticmethod
    def _write_new(path: Path, raw: bytes) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        if os.name == "posix":
            descriptor = open_posix_path_no_follow(path, flags)
            os.fchmod(descriptor, 0o600)
        else:
            SessionAttachmentService._check_directories(path.parent)
            descriptor = os.open(path, flags | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)

    @staticmethod
    def _read(path: Path, limit: int) -> bytes:
        with os.fdopen(open_file_no_follow(path), "rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise OSError("Attachment storage content exceeds its limit.")
        return raw

    def upload(self, *, session_id: str, filename: str, media_type: str, raw: bytes) -> dict:
        directory = self._session_directory(session_id)
        filename = _filename(filename)
        kind, media_type, limit = attachment_type(filename, media_type)
        if len(raw) > limit:
            raise SessionAttachmentError(f"Attachment exceeds {limit // (1024 * 1024)} MiB.", 413)
        metadata = {
            "attachment_id": "att_" + uuid4().hex,
            "kind": kind,
            "filename": filename,
            "media_type": media_type,
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        text_raw = None
        prepared_audio = None
        if kind == "image":
            width, height = _validate_image(raw, media_type)
            metadata.update(width=width, height=height)
        elif kind == "audio":
            try:
                prepared_audio, audio_metadata = prepare_audio(
                    raw, media_type, source_max_bytes=MAX_AUDIO_ATTACHMENT_BYTES,
                )
            except AudioValidationError as exc:
                status = 413 if len(raw) > MAX_AUDIO_ATTACHMENT_BYTES else 422
                raise SessionAttachmentError(str(exc), status) from exc
            metadata.update(
                audio_metadata,
                input_size_bytes=len(prepared_audio),
                normal_size_bytes=len(prepared_audio),
            )
            if prepared_audio != raw or audio_metadata.get("source_format") == "m4a":
                metadata.update(
                    prepared_sha256=hashlib.sha256(prepared_audio).hexdigest(),
                    prepared_size_bytes=len(prepared_audio),
                )
        else:
            policy = get_file_type_policy(filename)
            if policy.preferred_processor == "native_text":
                _validate_native_text(raw)
            try:
                processed = self.file_processor.process_bytes(
                    filename, raw, source_max_bytes=MAX_DOCUMENT_BYTES,
                )
            except FileProcessingError as exc:
                notice = "Invalid, encrypted or unreadable document, or no extractable text."
                if policy.preferred_processor == "native_pdf":
                    notice += " PDF text-layer extraction only; scanned PDFs require separate OCR."
                raise SessionAttachmentError(notice) from exc
            extraction = dict(processed.metadata["extraction"])
            for key in ("page_spans", "pages_without_text", "pages_needing_visual"):
                items = extraction.get(key)
                if isinstance(items, list):
                    extraction[key + "_count"] = len(items)
                    extraction[key] = items[:20]
                    extraction[key + "_truncated"] = len(items) > 20
            text_raw = processed.text.encode("utf-8")
            metadata.update(
                processor=processed.metadata["extraction"]["processor"],
                total_chars=len(processed.text),
                text_size_bytes=len(text_raw),
                text_sha256=hashlib.sha256(text_raw).hexdigest(),
                extraction=extraction,
            )
        encoded_metadata = json.dumps(metadata).encode("utf-8")
        if len(encoded_metadata) > MAX_METADATA_BYTES:
            raise SessionAttachmentError("Document extraction metadata exceeds its limit.")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._check_directories(directory)
        path = directory / metadata["attachment_id"]
        storage_suffix = {"image": ".image", "audio": ".audio", "document": ".document"}[kind]
        self._write_new(path.with_suffix(storage_suffix), raw)
        if prepared_audio is not None and metadata.get("prepared_sha256"):
            self._write_new(path.with_suffix(".prepared_audio"), prepared_audio)
        if text_raw is not None:
            self._write_new(path.with_suffix(".text"), text_raw)
        self._write_new(path.with_suffix(".json"), encoded_metadata)
        return metadata

    def get(self, *, session_id: str, attachment_id: str) -> dict:
        metadata, _raw, _text = self._read_attachment(session_id, attachment_id)
        return metadata

    def _read_attachment(
        self, session_id: str, attachment_id: str, *, normalize_audio_input: bool = False,
    ) -> tuple[dict, bytes, str | None]:
        directory = self._session_directory(session_id)
        if not re.fullmatch(r"att_[0-9a-f]{32}", attachment_id):
            raise KeyError("Attachment not found.")
        path = directory / attachment_id
        try:
            metadata = json.loads(self._read(path.with_suffix(".json"), MAX_METADATA_BYTES))
            if not isinstance(metadata, dict) or metadata.get("attachment_id") != attachment_id:
                raise ValueError("Invalid metadata.")
            kind = metadata.get("kind", "image")  # Existing image metadata remains readable.
            text = None
            source_raw = None
            if kind == "image":
                raw = self._read(path.with_suffix(".image"), MAX_ATTACHMENT_BYTES)
                width, height = _validate_image(raw, metadata["media_type"])
                if metadata["width"] != width or metadata["height"] != height:
                    raise ValueError("Image dimensions changed.")
            elif kind == "document":
                selected_kind, media_type, _limit = attachment_type(metadata["filename"], metadata["media_type"])
                if selected_kind != "document" or media_type != metadata["media_type"]:
                    raise ValueError("Invalid document type.")
                raw = self._read(path.with_suffix(".document"), MAX_DOCUMENT_BYTES)
                text_raw = self._read(path.with_suffix(".text"), MAX_TEXT_BYTES)
                text = text_raw.decode("utf-8")
                policy = get_file_type_policy(metadata["filename"])
                if (
                    not text.strip() or len(text) > MAX_EXTRACTED_CHARS
                    or metadata["text_sha256"] != hashlib.sha256(text_raw).hexdigest()
                    or metadata["text_size_bytes"] != len(text_raw)
                    or metadata["total_chars"] != len(text)
                    or metadata["processor"] != policy.preferred_processor
                    or not isinstance(metadata["extraction"], dict)
                    or metadata["extraction"].get("processor") != metadata["processor"]
                    or metadata["extraction"].get("source_sha256") != metadata["sha256"]
                ):
                    raise ValueError("Document integrity check failed.")
            elif kind == "audio":
                selected_kind, media_type, _limit = attachment_type(
                    metadata["filename"], metadata["media_type"]
                )
                if selected_kind != "audio" or media_type != metadata["media_type"]:
                    raise ValueError("Invalid audio type.")
                raw = self._read(path.with_suffix(".audio"), MAX_AUDIO_ATTACHMENT_BYTES)
                source_raw = raw
                if (
                    metadata["sha256"] != hashlib.sha256(source_raw).hexdigest()
                    or metadata["size_bytes"] != len(source_raw)
                    or _filename(metadata["filename"]) != metadata["filename"]
                ):
                    raise ValueError("Audio source integrity check failed.")
                if metadata.get("prepared_sha256"):
                    if metadata.get("source_format") == "m4a":
                        _validate_mp4_audio_container(raw)
                    prepared = self._read(
                        path.with_suffix(".prepared_audio"), MAX_NORMALIZED_AUDIO_BYTES,
                    )
                    if (
                        metadata.get("prepared_size_bytes") != len(prepared)
                        or metadata.get("prepared_sha256") != hashlib.sha256(prepared).hexdigest()
                    ):
                        raise ValueError("Prepared audio integrity check failed.")
                    if metadata.get("source_format") == "m4a" and metadata.get("audio_format") == "wav":
                        # Existing M4A attachments keep their legacy WAV cache readable.
                        audio_metadata = validate_normalized_m4a(prepared)
                    else:
                        audio_metadata = validate_audio(
                            prepared,
                            "audio/wav" if metadata.get("audio_format") == "wav" else "audio/mpeg",
                            source_max_bytes=MAX_NORMALIZED_AUDIO_BYTES,
                        )
                        audio_metadata.update(
                            source_format=metadata.get("source_format"),
                            duration_ms=metadata.get("duration_ms"),
                        )
                        source_metadata = validate_audio(
                            raw, media_type, source_max_bytes=MAX_AUDIO_ATTACHMENT_BYTES,
                        )
                        if abs(source_metadata["duration_ms"] - metadata.get("duration_ms", -100000)) > 2000:
                            raise ValueError("Audio source duration changed.")
                    if normalize_audio_input:
                        raw = prepared
                else:
                    audio_metadata = validate_audio(
                        raw, media_type, source_max_bytes=MAX_AUDIO_ATTACHMENT_BYTES,
                    )
                if (
                    metadata.get("input_size_bytes") != (
                        metadata.get("prepared_size_bytes") if metadata.get("prepared_sha256")
                        else len(raw)
                    )
                    or metadata.get("normal_size_bytes") != metadata.get("input_size_bytes")
                    or any(metadata.get(key) != value for key, value in audio_metadata.items())
                ):
                    raise ValueError("Audio metadata integrity check failed.")
            else:
                raise ValueError("Invalid attachment kind.")
            if (
                metadata["sha256"] != hashlib.sha256(source_raw if source_raw is not None else raw).hexdigest()
                or metadata["size_bytes"] != len(source_raw if source_raw is not None else raw)
                or _filename(metadata["filename"]) != metadata["filename"]
            ):
                raise ValueError("Attachment integrity check failed.")
            self._require_active_session(session_id)
            return metadata, raw, text
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise KeyError("Attachment not found or unavailable.") from exc

    def read_file(self, *, session_id: str, attachment_id: str) -> tuple[dict, bytes]:
        metadata, raw, _text = self._read_attachment(session_id, attachment_id)
        return metadata, raw

    def read_image(self, *, session_id: str, attachment_id: str) -> tuple[dict, bytes]:
        metadata, raw, _text = self._read_attachment(session_id, attachment_id)
        if metadata.get("kind", "image") != "image":
            raise KeyError("Image attachment not found.")
        return metadata, raw

    @staticmethod
    def _validate_read_bounds(start_char: int, max_chars: int) -> None:
        if type(start_char) is not int or type(max_chars) is not int or not 1 <= max_chars <= 64000:
            raise SessionAttachmentError("Invalid document reading bounds.")

    @staticmethod
    def _document_preview(metadata: dict, text: str, start_char: int, max_chars: int) -> dict:
        if not 0 <= start_char <= len(text):
            raise SessionAttachmentError("Document offset is outside its text range.")
        end = min(len(text), start_char + max_chars)
        return {
            "attachment_id": metadata["attachment_id"], "filename": metadata["filename"],
            "sha256": metadata["sha256"], "processor": metadata["processor"],
            "text": text[start_char:end], "total_chars": len(text),
            "start_char": start_char, "end_char": end,
            "next_start_char": end if end < len(text) else None,
            "truncated": end < len(text), "extraction": metadata["extraction"],
        }

    def read_input(self, *, session_id: str, attachment_id: str,
                   max_chars: int = 12000) -> tuple[dict, bytes, dict | None]:
        """Return original bytes and an optional preview from one validated snapshot."""
        self._validate_read_bounds(0, max_chars)
        metadata, raw, text = self._read_attachment(
            session_id, attachment_id, normalize_audio_input=True,
        )
        preview = None if text is None else self._document_preview(metadata, text, 0, max_chars)
        return metadata, raw, preview

    def read_document(self, *, session_id: str, attachment_id: str,
                      start_char: int = 0, max_chars: int = 12000) -> dict:
        self._validate_read_bounds(start_char, max_chars)
        metadata, _raw, text = self._read_attachment(session_id, attachment_id)
        if text is None:
            raise KeyError("Document attachment not found.")
        return self._document_preview(metadata, text, start_char, max_chars)

    def find_document(self, *, session_id: str, attachment_id: str, query: str,
                      start_char: int = 0, max_matches: int = 20) -> dict:
        if (not isinstance(query, str) or not query or len(query) > 256
                or type(start_char) is not int or type(max_matches) is not int
                or not 1 <= max_matches <= 100):
            raise SessionAttachmentError("Invalid document search bounds or query.")
        metadata, _raw, text = self._read_attachment(session_id, attachment_id)
        if text is None:
            raise KeyError("Document attachment not found.")
        if not 0 <= start_char <= len(text):
            raise SessionAttachmentError("Document offset is outside its text range.")
        matches = []
        offset = start_char
        while len(matches) < max_matches:
            index = text.find(query, offset)
            if index < 0:
                break
            end = index + len(query)
            snippet_start = max(0, index - 160)
            snippet_end = min(len(text), end + 160)
            matches.append({"start_char": index, "end_char": end,
                            "snippet_start_char": snippet_start, "snippet_end_char": snippet_end,
                            "text": text[snippet_start:snippet_end]})
            offset = end
        more = text.find(query, offset) >= 0
        return {"attachment_id": attachment_id, "filename": metadata["filename"],
                "sha256": metadata["sha256"], "query": query, "matches": matches,
                "total_chars": len(text), "start_char": start_char,
                "next_start_char": offset if more else None, "truncated": more}
