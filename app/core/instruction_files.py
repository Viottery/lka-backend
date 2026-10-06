"""Bounded, file-backed guidance for agent and watch runs.

These documents are operational context, never a source of tool permissions.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

MAX_INSTRUCTION_BYTES = 16_384
MAX_TOTAL_INSTRUCTION_BYTES = 32_768
MAX_INSTRUCTION_UPDATE_BYTES = 1_000_000
INDEX_CHUNK_BYTES = 8_192
IMPORTANT_LINE = re.compile(r"必须|禁止|不得|应当|should|must|never|always", re.IGNORECASE)


@dataclass(frozen=True)
class _IndexedChunk:
    offset: int
    text: str
    excerpt: str


@dataclass(frozen=True)
class _FileIndex:
    signature: tuple[int, int, int, int]
    chunks: tuple[_IndexedChunk, ...]
    summary: str
    outline: tuple[dict[str, object], ...]
    sha256: str

_LEGACY_GLOBAL_TEMPLATE = """# Global Agent Guidance

This file is loaded for every Agent turn. Keep enduring user preferences and
cross-project working conventions here. Project-specific rules belong in that
workspace's AGENTS.md. These notes cannot grant tools or bypass safety checks.
"""

GLOBAL_TEMPLATE = """# Global Agent Guidance

This file is loaded for every Agent turn. It contains user-maintained,
cross-project operating guidance. Only edit guidance files when the user
explicitly requests a persistent guidance-file change. Ordinary conversational
preferences, corrections, and requests to remember belong to background memory
learning when enabled; they are not authorization to edit AGENTS.md.
Project-specific rules belong in that workspace's AGENTS.md. These notes cannot
grant tools or bypass safety checks.
"""

WATCH_TEMPLATE = """# Global Watch Guidance

This file guides every scheduled watch check. Keep durable preferences for how
to assess freshness, evidence, significance, and uncertainty here. Do not
repeat individual watch goals or private source content. A watch run remains
read-only and limited to its explicitly granted sources and tools.
"""


class InstructionFiles:
    def __init__(self, data_dir: Path, workspace_roots: list[Path]) -> None:
        self.base = data_dir / "instructions"
        self.roots = tuple(root.resolve(strict=False) for root in workspace_roots)
        self._update_lock = threading.Lock()
        self._index_lock = threading.Lock()
        self._indexes: dict[Path, _FileIndex] = {}
        self._pending: dict[Path, None] = {}
        self._index_wake = threading.Event()
        self._index_stop = threading.Event()
        self._index_thread: threading.Thread | None = None

    def close(self) -> None:
        self._index_stop.set()
        self._index_wake.set()
        if self._index_thread is not None:
            self._index_thread.join(timeout=2)

    def _preview_index(self, path: Path) -> _FileIndex | None:
        """Never rebuild a large changed document on the foreground path."""
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
        if stat.st_size <= 65_536:
            return self._index(path)
        with self._index_lock:
            cached = self._indexes.get(path)
            if cached is not None and cached.signature == signature:
                return cached
            if not self._index_stop.is_set():
                # Derived work is bounded and rebuildable after restart. Old
                # versions are never presented as a current-file summary.
                if len(self._pending) >= 16:
                    self._pending.pop(next(iter(self._pending)))
                self._pending[path] = None
                if self._index_thread is None or not self._index_thread.is_alive():
                    self._index_thread = threading.Thread(target=self._rebuild_pending, daemon=True,
                                                          name="instruction-index")
                    self._index_thread.start()
                self._index_wake.set()
        return None

    def _rebuild_pending(self) -> None:
        while not self._index_stop.is_set():
            self._index_wake.wait(timeout=1)
            self._index_wake.clear()
            while not self._index_stop.is_set():
                with self._index_lock:
                    if not self._pending:
                        break
                    path = next(iter(self._pending))
                    self._pending.pop(path)
                try:
                    self._index(path)
                except (OSError, ValueError):
                    # Current preview/paging remains authoritative. Explicit
                    # search reports the original parse/access error.
                    continue

    def _attach_index(self, item: dict[str, object], path: Path) -> None:
        indexed = self._preview_index(path)
        item["index_status"] = "ready" if indexed is not None else "pending"
        item["summary"] = indexed.summary if indexed is not None else "Latest file preview loaded; index rebuilding. Search/read the original for omitted rules."
        item["index_chunk_count"] = len(indexed.chunks) if indexed is not None else 0
        item["index_outline"] = list(indexed.outline) if indexed is not None else []
        item["indexed_sha256"] = indexed.sha256 if indexed is not None else None

    def path(self, kind: str) -> Path:
        if kind == "global":
            return self.base / "AGENTS.md"
        if kind == "watch":
            return self.base / "watches" / "AGENTS.md"
        raise ValueError("kind must be global or watch")

    def initialize(self) -> None:
        for kind, template in (("global", GLOBAL_TEMPLATE), ("watch", WATCH_TEMPLATE)):
            target = self.path(kind)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                try:
                    with target.open("x", encoding="utf-8") as stream:
                        stream.write(template)
                except FileExistsError:
                    pass
            elif kind == "global" and not target.is_symlink():
                # Upgrade only the untouched old default. User-maintained
                # guidance, including any appended preferences, stays intact.
                legacy = _LEGACY_GLOBAL_TEMPLATE.encode("utf-8")
                if target.read_bytes() == legacy:
                    try:
                        self.update(kind, content=template,
                                    expected_sha256=hashlib.sha256(legacy).hexdigest())
                    except ValueError:
                        # A concurrent edit must win over the template upgrade.
                        pass

    def read(
        self, kind: str, *, offset: int = 0, max_bytes: int = 4_096,
    ) -> dict[str, object]:
        return self._page(self.path(kind), kind=kind, offset=offset,
                          max_bytes=max_bytes, include_hash=True)

    def read_project(
        self, path: str, *, workspace_root: str | None,
        offset: int = 0, max_bytes: int = 4_096,
    ) -> dict[str, object]:
        candidate = Path(path).resolve(strict=False)
        if candidate not in self._project_paths(workspace_root):
            raise PermissionError("Project AGENTS.md is outside the selected workspace instruction chain.")
        return self._page(candidate, kind="project", offset=offset,
                          max_bytes=max_bytes, include_hash=False)

    def for_watch(self) -> dict[str, object]:
        path = self.path("watch")
        item = self._page(path, kind="watch", offset=0,
                          max_bytes=4_096, include_hash=False)
        self._attach_index(item, path)
        return item

    def update(self, kind: str, *, content: str, expected_sha256: str) -> dict[str, str]:
        if not content.strip() or len(content.encode("utf-8")) > MAX_INSTRUCTION_UPDATE_BYTES:
            raise ValueError("Instruction content must be nonempty and at most 1000000 UTF-8 bytes.")
        with self._update_lock:
            current = self.read(kind)
            if current["sha256"] != expected_sha256:
                raise ValueError("Instruction file changed; read it again before updating.")
            path = self.path(kind)
            if path.is_symlink():
                raise ValueError("Instruction file must not be a symlink.")
            descriptor, temporary = tempfile.mkstemp(prefix=".AGENTS-", dir=path.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            with self._index_lock:
                self._indexes.pop(path, None)
            self._preview_index(path)
            return self.read(kind)

    def search(
        self, *, kind: str, query: str, workspace_root: str | None = None,
        path: str | None = None, limit: int = 8,
    ) -> dict[str, object]:
        if not query.strip() or not 1 <= limit <= 20:
            raise ValueError("query is required and limit must be from 1 to 20.")
        if kind == "project":
            if path is None:
                raise ValueError("path is required for project instructions.")
            selected = Path(path).resolve(strict=False)
            if selected not in self._project_paths(workspace_root):
                raise PermissionError("Project AGENTS.md is outside the selected workspace instruction chain.")
        else:
            selected = self.path(kind)
        index = self._index(selected)
        terms = [term.lower() for term in re.findall(r"[\w\u4e00-\u9fff]+", query) if term]
        scored = []
        for chunk in index.chunks:
            lower = chunk.text.lower()
            score = sum(lower.count(term) for term in terms)
            if score:
                scored.append((score, chunk))
        scored.sort(key=lambda item: (-item[0], item[1].offset))
        return {
            "kind": kind, "path": str(selected), "query": query,
            "matches": [{"offset": chunk.offset, "excerpt": chunk.excerpt, "score": score}
                        for score, chunk in scored[:limit]],
            "total_matches": len(scored), "sha256": index.sha256,
        }

    def for_workspace(self, workspace_root: str | None) -> list[dict[str, object]]:
        """Expose bounded previews and a continuation handle for every file."""
        paths = [self.path("global"), *self._project_paths(workspace_root)]
        result: list[dict[str, object]] = []
        for index, path in enumerate(paths):
            if index and not path.is_file():
                continue
            item = self._page(path, kind="global" if index == 0 else "project",
                              offset=0, max_bytes=4_096, include_hash=False)
            self._attach_index(item, path)
            item["index_hint"] = "Use instructions.search to find relevant passages, then read by byte offset. Summary is extractive and may omit rules."
            result.append(item)
        used = sum(len(str(item["content"]).encode("utf-8"))
                   + len(str(item["summary"]).encode("utf-8")) for item in result)
        # Keep every path visible. When previews compete for budget, preserve
        # the global prelude and the most specific project preview.
        for item in result[1:-1]:
            if used <= MAX_TOTAL_INSTRUCTION_BYTES:
                break
            used -= len(str(item["content"]).encode("utf-8"))
            item["content"] = ""
            item["preview_omitted"] = True
            item["truncated"] = True
            item["next_offset"] = 0
        if used > MAX_TOTAL_INSTRUCTION_BYTES and len(result) > 1:
            item = result[-1]
            used -= len(str(item["content"]).encode("utf-8"))
            item["content"] = ""
            item["preview_omitted"] = True
            item["truncated"] = True
            item["next_offset"] = 0
        for item in result[1:]:
            if used <= MAX_TOTAL_INSTRUCTION_BYTES:
                break
            used -= len(str(item["summary"]).encode("utf-8"))
            item["summary"] = "Summary omitted by context budget; search or read the file."
        for item in result:
            if item["truncated"]:
                item["read_more"] = (
                    f"instructions.read(kind='global', offset={item['next_offset']})"
                    if item["kind"] == "global" else
                    f"instructions.read_project(path={item['path']!r}, offset={item['next_offset']})"
                )
        return result

    def _index(self, path: Path) -> _FileIndex:
        if path.is_symlink():
            raise ValueError("Instruction file must not be a symlink.")
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
        with self._index_lock:
            cached = self._indexes.get(path)
            if cached is not None and cached.signature == signature:
                return cached
        chunks: list[_IndexedChunk] = []
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while True:
                offset = stream.tell()
                raw = stream.read(INDEX_CHUNK_BYTES)
                if not raw:
                    break
                # Complete the last UTF-8 codepoint before recording the page.
                while raw:
                    try:
                        text = raw.decode("utf-8")
                        break
                    except UnicodeDecodeError as exc:
                        if exc.reason != "unexpected end of data":
                            raise ValueError("Instruction file contains invalid UTF-8.") from exc
                        if offset + len(raw) >= stat.st_size or exc.start == 0:
                            raise ValueError("Instruction file contains incomplete UTF-8.") from exc
                        stream.seek(offset + exc.start)
                        raw = raw[:exc.start]
                digest.update(raw)
                normalized = " ".join(text.split())
                important = next((line.strip() for line in text.splitlines()
                                  if IMPORTANT_LINE.search(line)), "")
                excerpt = (important or normalized)[:200]
                chunks.append(_IndexedChunk(offset=offset, text=text, excerpt=excerpt))
        # No title or table-of-contents convention is required. This is an
        # extractive orientation, not a replacement for reading the source.
        selected_offsets = {chunk.offset for chunk in chunks[:2] + chunks[-2:]}
        critical = [chunk for chunk in chunks if IMPORTANT_LINE.search(chunk.text)]
        if critical:
            stride = max(1, len(critical) // 8)
            selected_offsets.update(chunk.offset for chunk in critical[::stride][:8])
        if chunks:
            stride = max(1, len(chunks) // 6)
            selected_offsets.update(chunk.offset for chunk in chunks[::stride][:6])
        selected = [chunk for chunk in chunks if chunk.offset in selected_offsets]
        summary = "\n".join(
            f"byte {chunk.offset}: {chunk.excerpt[:100]}" for chunk in selected
        )[:2_048]
        outline_chunks = (
            selected if len(selected) <= 8 else
            [selected[round(i * (len(selected) - 1) / 7)] for i in range(8)]
        )
        outline = tuple({"offset": chunk.offset, "excerpt": chunk.excerpt[:80]}
                        for chunk in outline_chunks)
        indexed = _FileIndex(signature=signature, chunks=tuple(chunks),
                             summary=summary, outline=outline,
                             sha256=digest.hexdigest())
        with self._index_lock:
            self._indexes[path] = indexed
        return indexed

    def _project_paths(self, workspace_root: str | None) -> list[Path]:
        if not workspace_root:
            return []
        selected = Path(workspace_root).resolve(strict=False)
        allowed = [root for root in self.roots if selected == root or root in selected.parents]
        if not allowed or not selected.is_dir():
            return []
        root = max(allowed, key=lambda item: len(item.parts))
        chain = [root, *reversed([part for part in selected.parents if part != root and root in part.parents]), selected]
        return [folder / "AGENTS.md" for folder in dict.fromkeys(chain)
                if not (folder / "AGENTS.md").is_symlink()]

    @staticmethod
    def _page(
        path: Path, *, kind: str, offset: int, max_bytes: int, include_hash: bool,
    ) -> dict[str, object]:
        if path.is_symlink():
            raise ValueError("Instruction file must not be a symlink.")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a nonnegative byte offset.")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 4 <= max_bytes <= MAX_INSTRUCTION_BYTES:
            raise ValueError("max_bytes must be between 4 and 16384.")
        size = path.stat().st_size
        if offset > size:
            raise ValueError("offset exceeds file size.")
        with path.open("rb") as stream:
            stream.seek(offset)
            raw = stream.read(max_bytes)
        try:
            content = raw.decode("utf-8")
            consumed = len(raw)
        except UnicodeDecodeError as exc:
            if exc.reason != "unexpected end of data" or offset + len(raw) >= size:
                raise ValueError("Instruction file contains invalid UTF-8 or offset is not a character boundary.") from exc
            consumed = exc.start
            content = raw[:consumed].decode("utf-8")
        next_offset = offset + consumed
        result: dict[str, object] = {
            "kind": kind, "path": str(path), "content": content,
            "offset": offset, "next_offset": next_offset if next_offset < size else None,
            "file_size_bytes": size, "truncated": next_offset < size,
        }
        if include_hash:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(65_536), b""):
                    digest.update(block)
            result["sha256"] = digest.hexdigest()
        return result
