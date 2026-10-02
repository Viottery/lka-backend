"""Read-only file browsing for an active session's bound workspace."""

from __future__ import annotations

import ipaddress
import os
import stat
from pathlib import Path, PureWindowsPath

from fastapi import APIRouter, HTTPException, Query, Request, Response

router = APIRouter(prefix="/sessions/{session_id}", tags=["session files"])

MAX_DIRECTORY_ENTRIES = 200
MAX_PREVIEW_BYTES = 256 * 1024
MAX_RAW_PREVIEW_BYTES = 8 * 1024 * 1024

RAW_PREVIEW_TYPES = {
    ".png": ("image/png", lambda data: data.startswith(b"\x89PNG\r\n\x1a\n")),
    ".jpg": ("image/jpeg", lambda data: data.startswith(b"\xff\xd8\xff")),
    ".jpeg": ("image/jpeg", lambda data: data.startswith(b"\xff\xd8\xff")),
    ".webp": (
        "image/webp",
        lambda data: len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP",
    ),
    ".pdf": ("application/pdf", lambda data: data.startswith(b"%PDF-")),
}


def _require_loopback(request: Request) -> None:
    client = request.client
    try:
        address = ipaddress.ip_address(client.host if client else "")
    except ValueError as exc:
        raise HTTPException(
            status_code=403, detail="This endpoint is available only from loopback."
        ) from exc
    if not address.is_loopback:
        raise HTTPException(
            status_code=403, detail="This endpoint is available only from loopback."
        )


def _get_workspace(request: Request, session_id: str) -> Path:
    try:
        detail = request.app.state.runtime.get_session(session_id=session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Active session not found.") from exc

    workspace = detail.session.workspace
    if workspace is None or not workspace.backend_path:
        raise HTTPException(status_code=409, detail="Session has no bound workspace.")
    bound_root = Path(workspace.backend_path)
    try:
        root = bound_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=404, detail="Session workspace is unavailable.") from exc
    if not bound_root.is_absolute() or root != bound_root:
        raise HTTPException(status_code=403, detail="Session workspace path changed.")
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="Session workspace is unavailable.")
    return root


def _relative_parts(path: str) -> tuple[str, ...]:
    if "\x00" in path:
        raise HTTPException(status_code=400, detail="Invalid path.")
    normalized = path.replace("\\", "/")
    windows_path = PureWindowsPath(path)
    if normalized.startswith("/") or windows_path.is_absolute() or windows_path.drive:
        raise HTTPException(
            status_code=400, detail="Path must be relative to the session workspace."
        )
    parts = tuple(part for part in normalized.split("/") if part not in ("", "."))
    if any(part == ".." for part in parts):
        raise HTTPException(status_code=400, detail="Path traversal is not allowed.")
    return parts


def _resolve_workspace_path(root: Path, relative: str) -> Path:
    current = root
    for part in _relative_parts(relative):
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Workspace path not found.") from exc
        except OSError as exc:
            raise HTTPException(
                status_code=400, detail="Workspace path cannot be accessed."
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise HTTPException(status_code=403, detail="Symbolic links are not browsable.")
        try:
            resolved = current.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=403, detail="Workspace path escapes its root.") from exc
        current = resolved
    return current


def _file_descriptor_no_follow(path: Path) -> int:
    flags = os.O_RDONLY
    # A workspace entry may be a FIFO, including after a path-check race.
    # Open without waiting for a writer, then verify the opened object below.
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        if os.name == "posix":
            descriptor = _open_posix_path_no_follow(path, flags)
        else:
            # Windows retains path-based opening. Equivalent protection against
            # ancestor reparse-point replacement requires native handle APIs.
            descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Workspace file not found.") from exc
    except OSError as exc:
        raise HTTPException(
            status_code=403, detail="Workspace file cannot be opened safely."
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise HTTPException(status_code=400, detail="Path is not a regular file.")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_posix_path_no_follow(path: Path, flags: int) -> int:
    """Open an already resolved absolute path without following any symlinks."""
    if not path.is_absolute():
        raise OSError("Safe file opening requires an absolute path.")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(path.anchor, directory_flags)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        return os.open(path.name or ".", flags, dir_fd=directory)
    finally:
        os.close(directory)


@router.get("/files")
def list_session_files(
    session_id: str,
    request: Request,
    path: str = Query(default="", max_length=4096),
) -> dict:
    """List at most 200 immediate children of a workspace directory."""

    _require_loopback(request)
    root = _get_workspace(request, session_id)
    directory = _resolve_workspace_path(root, path)
    if not directory.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory.")

    entries: list[dict] = []
    directory_descriptor: int | None = None
    try:
        scan_target: Path | int = directory
        if os.name == "posix":
            try:
                directory_descriptor = _open_posix_path_no_follow(
                    directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                )
            except OSError as exc:
                raise HTTPException(
                    status_code=403, detail="Workspace directory cannot be opened safely."
                ) from exc
            scan_target = directory_descriptor
        with os.scandir(scan_target) as iterator:
            for item in iterator:
                try:
                    metadata = item.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                mode = metadata.st_mode
                if stat.S_ISLNK(mode):
                    kind = "symlink"
                    size = None
                elif stat.S_ISDIR(mode):
                    kind = "directory"
                    size = None
                elif stat.S_ISREG(mode):
                    kind = "file"
                    size = metadata.st_size
                else:
                    kind = "other"
                    size = None
                child_path = "/".join((*_relative_parts(path), item.name))
                entries.append(
                    {
                        "name": item.name,
                        "path": child_path,
                        "type": kind,
                        "size_bytes": size,
                        "modified_at": metadata.st_mtime,
                    }
                )
                if len(entries) > MAX_DIRECTORY_ENTRIES:
                    break
    except PermissionError as exc:
        raise HTTPException(
            status_code=403, detail="Workspace directory cannot be listed."
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=400, detail="Workspace directory cannot be listed."
        ) from exc
    finally:
        if directory_descriptor is not None:
            os.close(directory_descriptor)

    truncated = len(entries) > MAX_DIRECTORY_ENTRIES
    return {
        "session_id": session_id,
        "path": "/".join(_relative_parts(path)),
        "entries": entries[:MAX_DIRECTORY_ENTRIES],
        "truncated": truncated,
    }


@router.get("/file")
def preview_session_file(
    session_id: str,
    request: Request,
    path: str = Query(..., min_length=1, max_length=4096),
) -> dict:
    """Return a bounded UTF-8 text preview from the session workspace."""

    _require_loopback(request)
    root = _get_workspace(request, session_id)
    file_path = _resolve_workspace_path(root, path)

    descriptor = _file_descriptor_no_follow(file_path)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise HTTPException(status_code=400, detail="Path is not a regular file.")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAX_PREVIEW_BYTES + 1)
    finally:
        os.close(descriptor)

    truncated = len(raw) > MAX_PREVIEW_BYTES
    preview = raw[:MAX_PREVIEW_BYTES]
    try:
        text = preview.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        # A valid multi-byte sequence can straddle the byte cap. Preserve the
        # complete prefix while still rejecting malformed UTF-8 elsewhere.
        if truncated and exc.end == len(preview):
            text = preview[: exc.start].decode("utf-8", errors="strict")
        else:
            raise HTTPException(
                status_code=415, detail="Only UTF-8 text previews are supported."
            ) from exc

    return {
        "session_id": session_id,
        "path": "/".join(_relative_parts(path)),
        "encoding": "utf-8",
        "size_bytes": metadata.st_size,
        "text": text,
        "truncated": truncated,
    }


@router.get("/file/raw")
def preview_session_raw_file(
    session_id: str,
    request: Request,
    path: str = Query(..., min_length=1, max_length=4096),
) -> Response:
    """Return a small, signature-checked image or PDF for inline preview."""

    _require_loopback(request)
    root = _get_workspace(request, session_id)
    file_path = _resolve_workspace_path(root, path)
    declared_type = RAW_PREVIEW_TYPES.get(file_path.suffix.lower())
    if declared_type is None:
        raise HTTPException(status_code=415, detail="This file type cannot be previewed inline.")

    descriptor = _file_descriptor_no_follow(file_path)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise HTTPException(status_code=400, detail="Path is not a regular file.")
        if metadata.st_size > MAX_RAW_PREVIEW_BYTES:
            raise HTTPException(status_code=413, detail="Raw preview exceeds the 8 MiB limit.")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read(MAX_RAW_PREVIEW_BYTES + 1)
    finally:
        os.close(descriptor)

    if len(content) > MAX_RAW_PREVIEW_BYTES:
        raise HTTPException(status_code=413, detail="Raw preview exceeds the 8 MiB limit.")

    media_type, signature_matches = declared_type
    if not signature_matches(content):
        raise HTTPException(
            status_code=415, detail="File extension and content signature do not match."
        )

    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if media_type == "application/pdf":
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return Response(content=content, media_type=media_type, headers=headers)
