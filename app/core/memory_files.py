"""Conservative, editable Markdown views over the deterministic memory store.

The generated file is a bounded sidecar. It never edits instruction files and
only imports content changes to records that were present in its last snapshot.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.domains.memory import (
    MemoryConflictError,
    MemoryRecord,
    MemoryService,
    MemorySourceInput,
)

_HEADER = "<!-- lka-memory-view:v1 scope={scope} project_id={project_id} -->"
_MAX_VIEW_BYTES = 8 * 1024 * 1024
_RECORD = re.compile(
    r"^<!-- memory id=(?P<id>[A-Za-z0-9_-]+) version=(?P<version>[0-9]+) -->\n"
    r"<!-- source_ids=(?P<sources>[^\n]*) -->\n"
    r"<!-- updated_at=(?P<updated>[^\n]*) -->\n"
    r"```memory-content\n(?P<content>.*?)\n```\n<!-- /memory -->$",
    re.MULTILINE | re.DOTALL,
)


class MemoryFileError(ValueError):
    """Malformed, ambiguous, or manually altered memory view."""


class MemoryFileConflictError(MemoryConflictError):
    """The view changed since generation or its records changed concurrently."""


@dataclass(frozen=True)
class MemoryFileEdit:
    memory_id: str
    expected_version: int
    content: str


@dataclass(frozen=True)
class MemoryFilePreview:
    path: Path
    generated_hash: str
    current_hash: str
    edits: tuple[MemoryFileEdit, ...]


class MemoryFiles:
    """Generate and safely import global/project MEMORY.md sidecars."""

    def __init__(self, memory: MemoryService, data_dir: str | Path) -> None:
        self.memory = memory
        self.data_dir = Path(data_dir)

    def path_for(self, *, scope: str, project_id: str | None = None) -> Path:
        if scope == "global":
            if project_id is not None:
                raise ValueError("global memory cannot have project_id")
            return self.data_dir / "memory" / "global" / "MEMORY.md"
        if scope != "project" or not project_id:
            raise ValueError("project scope requires project_id")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", project_id):
            raise ValueError("invalid stable project_id")
        return self.data_dir / "memory" / "projects" / project_id / "MEMORY.md"

    def render(self, *, scope: str, project_id: str | None = None) -> str:
        self.path_for(scope=scope, project_id=project_id)
        records = self._view_records(scope=scope, project_id=project_id)
        records.sort(key=lambda item: item.memory_id)
        header = _HEADER.format(scope=scope, project_id=project_id or "-")
        blocks = [header, "", "<!-- Edit only memory-content blocks. Keep every record and its metadata. -->", ""]
        for item in records:
            content = item.content.replace("\r\n", "\n").replace("\r", "\n")
            if ("```" in content or "<!--" in content or "-->" in content
                    or len(content) > 4_000):
                raise MemoryFileError(f"Memory {item.memory_id} cannot be represented safely")
            blocks.extend((
                f"<!-- memory id={item.memory_id} version={item.version} -->",
                f"<!-- source_ids={','.join(item.source_ids)} -->",
                f"<!-- updated_at={item.updated_at} -->",
                "```memory-content",
                content,
                "```",
                "<!-- /memory -->",
                "",
            ))
        return "\n".join(blocks).rstrip() + "\n"

    def _view_records(self, *, scope: str, project_id: str | None) -> list[MemoryRecord]:
        records = self.memory.list(scope=scope, project_id=project_id, statuses=("active",), limit=1000)
        if len(records) == 1000 and self.memory.list(
            scope=scope, project_id=project_id, statuses=("active",), limit=1, offset=1000,
        ):
            raise MemoryFileError("Memory view exceeds 1000 records; use the paginated memory export API")
        return records

    @staticmethod
    def _read_view(path: Path) -> bytes:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_VIEW_BYTES + 1)
        if len(raw) > _MAX_VIEW_BYTES:
            raise MemoryFileError("Memory view exceeds 8 MiB; use the paginated memory export API")
        return raw

    def generate(self, *, scope: str, project_id: str | None = None) -> Path:
        """Write a view only when absent or still equal to our prior generation."""
        path = self.path_for(scope=scope, project_id=project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.memory.db_path, timeout=10) as conn:
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("CREATE TABLE IF NOT EXISTS memory_file_hashes "
                         "(path TEXT PRIMARY KEY, digest TEXT NOT NULL)")
            conn.execute("BEGIN IMMEDIATE")
            key = str(path.resolve(strict=False))
            row = conn.execute("SELECT digest FROM memory_file_hashes WHERE path=?",
                               (key,)).fetchone()
            previous = str(row[0]) if row else None
            if path.exists():
                try:
                    existing = self._read_view(path)
                except OSError as exc:
                    raise MemoryFileError(f"Cannot read memory view: {path}") from exc
                if previous is None or _hash(existing) != previous:
                    raise MemoryFileConflictError(f"Memory view has manual edits: {path}")
            encoded = self.render(scope=scope, project_id=project_id).encode("utf-8")
            digest = _hash(encoded)
            self._atomic_write(path, encoded)
            conn.execute("INSERT INTO memory_file_hashes(path,digest) VALUES(?,?) "
                         "ON CONFLICT(path) DO UPDATE SET digest=excluded.digest",
                         (key, digest))
            conn.commit()
        return path

    def preview_import(self, *, scope: str, project_id: str | None = None) -> MemoryFilePreview:
        path = self.path_for(scope=scope, project_id=project_id)
        if not path.exists():
            raise FileNotFoundError(path)
        try:
            raw = self._read_view(path)
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise MemoryFileError("Memory view is not valid UTF-8") from exc
        base_hash = self._last_hash(path)
        if base_hash is None:
            raise MemoryFileConflictError("No generated baseline exists for this file")
        edits = self._parse_edits(text, scope=scope, project_id=project_id)
        return MemoryFilePreview(path, base_hash, _hash(raw), tuple(edits))

    def import_edits(self, *, scope: str, project_id: str | None = None,
                     preview: MemoryFilePreview | None = None) -> list[MemoryRecord]:
        """Validate the complete file, then apply content-only edits using CAS."""
        preview = preview or self.preview_import(scope=scope, project_id=project_id)
        if preview.path != self.path_for(scope=scope, project_id=project_id):
            raise MemoryFileConflictError("Preview belongs to a different memory view")
        try:
            raw = self._read_view(preview.path)
        except OSError as exc:
            raise MemoryFileConflictError("Memory view disappeared after preview") from exc
        if _hash(raw) != preview.current_hash:
            raise MemoryFileConflictError("Memory view changed after preview")
        if len(preview.edits) > 1:
            raise MemoryFileError("Import one changed memory block at a time")
        # Check all versions before making any correction. MemoryService.correct
        # then provides a second, transactional CAS at each individual write.
        checked: list[tuple[MemoryFileEdit, MemoryRecord]] = []
        for edit in preview.edits:
            try:
                record = self.memory.get(edit.memory_id)
            except KeyError as exc:
                raise MemoryFileConflictError(f"Memory disappeared: {edit.memory_id}") from exc
            if record.scope != scope or record.project_id != project_id:
                raise MemoryFileError(f"Memory is outside this view: {edit.memory_id}")
            if record.status != "active" or record.version != edit.expected_version:
                raise MemoryFileConflictError(f"Memory changed since generation: {edit.memory_id}")
            checked.append((edit, record))
        updated: list[MemoryRecord] = []
        for edit, _record in checked:
            try:
                source_id = self.memory.register_source(MemorySourceInput(
                    source_type="user_memory_file",
                    source_ref=f"{edit.memory_id}:{preview.current_hash}",
                    checksum=_hash(edit.content.encode("utf-8")),
                    trusted_source=True,
                ))
                updated.append(self.memory.correct(edit.memory_id, content=edit.content,
                                                   expected_version=edit.expected_version,
                                                   source_id=source_id))
            except MemoryConflictError as exc:
                raise MemoryFileConflictError(str(exc)) from exc
        # Imported content is preserved verbatim for user review. Record the
        # current file as an owned baseline only after the full import succeeds.
        self._set_last_hash(preview.path, preview.current_hash)
        return updated

    def _parse_edits(self, text: str, *, scope: str, project_id: str | None) -> list[MemoryFileEdit]:
        expected_header = _HEADER.format(scope=scope, project_id=project_id or "-")
        if not text.startswith(expected_header + "\n"):
            raise MemoryFileError("Memory view header does not match requested scope")
        matches = list(_RECORD.finditer(text))
        residue = _RECORD.sub("", text)
        # Apart from the exact file header and fixed explanatory line, no text
        # outside a record is accepted: it could otherwise be silently lost.
        prefix = (expected_header + "\n\n<!-- Edit only memory-content blocks. "
                  "Keep every record and its metadata. -->\n")
        if not residue.startswith(prefix) or residue[len(prefix):].strip():
            raise MemoryFileError("Unknown or ambiguous content outside memory records")
        if not matches:
            active = self._view_records(scope=scope, project_id=project_id)
            if active:
                raise MemoryFileError("Memory view contains no records")
            if residue != prefix:
                raise MemoryFileError("Unknown content in empty memory view")
            return []
        ids = [match.group("id") for match in matches]
        if len(ids) != len(set(ids)):
            raise MemoryFileError("Duplicate memory ID")
        edits: list[MemoryFileEdit] = []
        for match in matches:
            memory_id, version = match.group("id"), int(match.group("version"))
            try:
                record = self.memory.get(memory_id)
            except KeyError as exc:
                raise MemoryFileError(f"Unknown memory ID: {memory_id}") from exc
            if record.scope != scope or record.project_id != project_id:
                raise MemoryFileError(f"Memory ID is outside this view: {memory_id}")
            if match.group("sources") != ",".join(record.source_ids):
                raise MemoryFileError(f"Source metadata changed for {memory_id}")
            if match.group("updated") != record.updated_at:
                raise MemoryFileError(f"Timestamp metadata changed for {memory_id}")
            if version != record.version:
                raise MemoryFileConflictError(f"Memory version changed: {memory_id}")
            content = match.group("content")
            if not content.strip():
                raise MemoryFileError(f"Memory content is blank: {memory_id}")
            if (len(content) > 4_000 or "```" in content or "<!--" in content
                    or "-->" in content):
                raise MemoryFileError(f"Memory content has unsafe Markdown structure: {memory_id}")
            if content != record.content:
                edits.append(MemoryFileEdit(memory_id, version, content))
        # Removing an existing ID is never interpreted as deletion.
        active = self._view_records(scope=scope, project_id=project_id)
        if set(ids) != {record.memory_id for record in active}:
            raise MemoryFileError("Missing or extra memory IDs; deletion is not supported")
        return edits

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def _last_hash(self, path: Path) -> str | None:
        with sqlite3.connect(self.memory.db_path, timeout=10) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS memory_file_hashes "
                         "(path TEXT PRIMARY KEY, digest TEXT NOT NULL)")
            row = conn.execute("SELECT digest FROM memory_file_hashes WHERE path=?",
                               (str(path.resolve(strict=False)),)).fetchone()
            return str(row[0]) if row else None

    def _set_last_hash(self, path: Path, digest: str) -> None:
        with sqlite3.connect(self.memory.db_path, timeout=10) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS memory_file_hashes "
                         "(path TEXT PRIMARY KEY, digest TEXT NOT NULL)")
            conn.execute("INSERT INTO memory_file_hashes(path,digest) VALUES(?,?) "
                         "ON CONFLICT(path) DO UPDATE SET digest=excluded.digest",
                         (str(path.resolve(strict=False)), digest))
            conn.commit()


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
