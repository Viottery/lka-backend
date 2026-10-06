"""Open workspace entries without following links, including Windows junctions."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace


def is_link_or_reparse(metadata: os.stat_result) -> bool:
    """Windows junctions and other reparse points need not have S_IFLNK mode."""
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & 0x400
    )


def open_posix_path_no_follow(path: Path, flags: int) -> int:
    """Open an absolute path using directory-relative, no-follow descriptors."""
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


class NotRegularFileError(OSError):
    """The opened object is a directory, device, or other non-file entry."""


def open_file_no_follow(path: Path) -> int:
    """Return a caller-owned read-only descriptor for a verified regular file."""
    flags = os.O_RDONLY
    for name in ("O_NONBLOCK", "O_BINARY", "O_NOFOLLOW"):
        flags |= getattr(os, name, 0)
    if os.name == "posix":
        descriptor = open_posix_path_no_follow(path, flags)
    elif os.name == "nt":
        descriptor = open_windows_file_no_follow(path, flags)
    else:
        raise OSError("Safe file opening is not supported on this platform.")
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise NotRegularFileError("Path is not a regular file.")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


class _WindowsFileApi:
    def __init__(self) -> None:
        # Import only on Windows: Linux must not need msvcrt or WinDLL.
        import ctypes
        from ctypes import wintypes

        class FileInformation(ctypes.Structure):
            _fields_ = [
                ("attributes", wintypes.DWORD),
                ("creation_time", wintypes.FILETIME),
                ("access_time", wintypes.FILETIME),
                ("write_time", wintypes.FILETIME),
                ("volume_serial", wintypes.DWORD),
                ("size_high", wintypes.DWORD),
                ("size_low", wintypes.DWORD),
                ("links", wintypes.DWORD),
                ("index_high", wintypes.DWORD),
                ("index_low", wintypes.DWORD),
            ]

        class UnicodeString(ctypes.Structure):
            _fields_ = [
                ("length", wintypes.USHORT),
                ("maximum_length", wintypes.USHORT),
                ("buffer", wintypes.LPWSTR),
            ]

        class ObjectAttributes(ctypes.Structure):
            _fields_ = [
                ("length", wintypes.ULONG),
                ("root_directory", wintypes.HANDLE),
                ("object_name", ctypes.POINTER(UnicodeString)),
                ("attributes", wintypes.ULONG),
                ("security_descriptor", wintypes.LPVOID),
                ("security_quality", wintypes.LPVOID),
            ]

        class IoStatusBlock(ctypes.Structure):
            _fields_ = [("status", ctypes.c_void_p), ("information", ctypes.c_size_t)]

        class DirectoryInformation(ctypes.Structure):
            _fields_ = [
                ("next_offset", wintypes.DWORD), ("file_index", wintypes.DWORD),
                ("creation_time", ctypes.c_longlong), ("access_time", ctypes.c_longlong),
                ("write_time", ctypes.c_longlong), ("change_time", ctypes.c_longlong),
                ("size", ctypes.c_longlong), ("allocation_size", ctypes.c_longlong),
                ("attributes", wintypes.DWORD), ("name_length", wintypes.DWORD),
                ("ea_size", wintypes.DWORD), ("name", wintypes.WCHAR * 1),
            ]

        self.ctypes = ctypes
        self.information_type = FileInformation
        self.unicode_type = UnicodeString
        self.object_type = ObjectAttributes
        self.io_status_type = IoStatusBlock
        self.directory_type = DirectoryInformation
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.create = kernel.CreateFileW
        self.create.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        self.create.restype = wintypes.HANDLE
        self.information = kernel.GetFileInformationByHandle
        self.information.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileInformation)]
        self.information.restype = wintypes.BOOL
        self.file_type = kernel.GetFileType
        self.file_type.argtypes = [wintypes.HANDLE]
        self.file_type.restype = wintypes.DWORD
        self.close = kernel.CloseHandle
        self.close.argtypes = [wintypes.HANDLE]
        self.close.restype = wintypes.BOOL
        self.directory_information = kernel.GetFileInformationByHandleEx
        self.directory_information.argtypes = [
            wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
        ]
        self.directory_information.restype = wintypes.BOOL
        native = ctypes.WinDLL("ntdll")
        self.create_relative = native.NtCreateFile
        self.create_relative.argtypes = [
            ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
            ctypes.POINTER(ObjectAttributes), ctypes.POINTER(IoStatusBlock),
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
            wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        ]
        self.create_relative.restype = wintypes.LONG
        self.status_to_error = native.RtlNtStatusToDosError
        self.status_to_error.argtypes = [wintypes.LONG]
        self.status_to_error.restype = wintypes.ULONG

    def open(self, path: Path, *, directory: bool, parent: int | None = None) -> int:
        if parent is not None:
            handle = self._open_relative(parent, path.name, directory=directory)
            return self._verify(handle, path, directory=directory)
        text = str(path)
        # Extended paths disable Win32 DOS-device / trailing-dot normalization
        # and also support long paths without depending on a host registry flag.
        if not text.startswith("\\\\?\\"):
            text = "\\\\?\\UNC\\" + text[2:] if text.startswith("\\\\") else "\\\\?\\" + text
        handle = self.create(
            text,
            # Attribute-only access does not participate in share-mode checks.
            # Include LIST_DIRECTORY so these handles actually prevent rename.
            0x81 if directory else 0x80000000,  # LIST_DIRECTORY|READ_ATTRIBUTES / GENERIC_READ
            0x1,  # SHARE_READ: prevent data writes and delete/rename, not WRITE_ATTRIBUTES
            None,
            3,  # OPEN_EXISTING
            0x00200000 | 0x02000000,  # OPEN_REPARSE_POINT | BACKUP_SEMANTICS
            None,
        )
        if handle == self.ctypes.c_void_p(-1).value:
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        return self._verify(handle, path, directory=directory)

    def _open_relative(self, parent: int, name: str, *, directory: bool) -> int:
        from ctypes import wintypes

        if not name or name in (".", "..") or any(char in name for char in "\\/:"):
            raise PermissionError("Invalid relative Windows file component.")
        buffer = self.ctypes.create_unicode_buffer(name)
        byte_length = len(name.encode("utf-16-le"))
        unicode_name = self.unicode_type(
            byte_length, byte_length + 2, self.ctypes.cast(buffer, wintypes.LPWSTR),
        )
        attributes = self.object_type(
            self.ctypes.sizeof(self.object_type), parent,
            self.ctypes.pointer(unicode_name), 0x1040,  # DONT_REPARSE | CASE_INSENSITIVE
            None, None,
        )
        status_block = self.io_status_type()
        handle = wintypes.HANDLE()
        status = self.create_relative(
            self.ctypes.byref(handle),
            0x100081 if directory else 0x120089,  # SYNCHRONIZE + list/read access
            self.ctypes.byref(attributes), self.ctypes.byref(status_block),
            None, 0, 0x1, 1,  # SHARE_READ, FILE_OPEN
            0x00200000 | 0x20,  # OPEN_REPARSE_POINT | SYNCHRONOUS_IO_NONALERT
            None, 0,
        )
        if status < 0:
            raise self.ctypes.WinError(self.status_to_error(status))
        return handle.value

    def _verify(self, handle: int, path: Path, *, directory: bool) -> int:
        try:
            information = self.information_type()
            if not self.information(handle, self.ctypes.byref(information)):
                raise self.ctypes.WinError(self.ctypes.get_last_error())
            if information.attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
                raise PermissionError(errno.EACCES, "Reparse points are not browsable.", str(path))
            is_directory = bool(information.attributes & 0x10)
            if directory and not is_directory:
                raise NotADirectoryError(errno.ENOTDIR, "Path is not a directory.", str(path))
            if not directory and (is_directory or self.file_type(handle) != 1):
                raise NotRegularFileError("Path is not a regular disk file.")
            return handle
        except BaseException:
            self.close(handle)
            raise

    def entries(self, handle: int) -> Iterator[_WindowsDirectoryEntry]:
        """Enumerate the opened object; never re-resolve its pathname."""
        buffer = self.ctypes.create_string_buffer(64 * 1024)
        information_class = 15  # FileFullDirectoryRestartInfo, then FullDirectoryInfo
        name_offset = self.directory_type.name.offset
        while True:
            if not self.directory_information(handle, information_class, buffer, len(buffer)):
                error = self.ctypes.get_last_error()
                if error == 18:  # ERROR_NO_MORE_FILES
                    return
                raise self.ctypes.WinError(error)
            information_class = 14
            offset = 0
            while True:
                information = self.directory_type.from_buffer(buffer, offset)
                name = self.ctypes.wstring_at(
                    self.ctypes.addressof(buffer) + offset + name_offset,
                    information.name_length // 2,
                )
                if name not in (".", ".."):
                    yield _WindowsDirectoryEntry(
                        name, information.attributes, information.size,
                        information.write_time / 10_000_000 - 11_644_473_600,
                    )
                if not information.next_offset:
                    break
                offset += information.next_offset


class _WindowsDirectoryEntry:
    def __init__(self, name: str, attributes: int, size: int, modified: float) -> None:
        self.name = name
        self.metadata = SimpleNamespace(
            st_mode=stat.S_IFDIR if attributes & 0x10 else stat.S_IFREG,
            st_file_attributes=attributes, st_size=size, st_mtime=modified,
        )

    def stat(self, *, follow_symlinks: bool = False) -> SimpleNamespace:
        if follow_symlinks:
            raise ValueError("Safe directory entries do not follow links.")
        return self.metadata


@lru_cache(maxsize=1)
def _windows_api() -> _WindowsFileApi:
    return _WindowsFileApi()


@contextmanager
def _windows_handle_chain(path: Path, *, directory: bool) -> Iterator[list[int]]:
    """Open each component relative to its verified parent directory handle.

    Share locks prevent rename but do not exclude WRITE_ATTRIBUTES handles that
    can set reparse data. Relative NtCreateFile and handle-based enumeration avoid
    resolving an ancestor's pathname again, including after in-place mutation.
    https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntcreatefile
    """
    if not path.is_absolute() or ".." in path.parts:
        raise OSError("Safe file opening requires an absolute, normalized path.")
    api = _windows_api()
    handles: list[int] = []
    try:
        current = Path(path.anchor)
        handles.append(api.open(current, directory=True))
        for index, part in enumerate(path.parts[1:], start=1):
            current /= part
            handles.append(api.open(
                current, directory=directory or index < len(path.parts) - 1, parent=handles[-1],
            ))
        yield handles
    finally:
        for handle in reversed(handles):
            api.close(handle)


def open_windows_file_no_follow(path: Path, flags: int) -> int:
    import msvcrt

    with _windows_handle_chain(path, directory=False) as handles:
        if len(path.parts) == 1:
            raise NotRegularFileError("Path is not a regular file.")
        handle = handles.pop()
        try:
            # Ownership transfers to the CRT descriptor; os.close releases it.
            return msvcrt.open_osfhandle(handle, flags)
        except BaseException:
            _windows_api().close(handle)
            raise


@contextmanager
def locked_windows_directory(path: Path) -> Iterator[int]:
    """Keep the directory and its complete ancestor chain pinned during listing."""
    with _windows_handle_chain(path, directory=True) as handles:
        yield handles[-1]


@contextmanager
def scandir_windows_handle(handle: int) -> Iterator[Iterator[_WindowsDirectoryEntry]]:
    yield _windows_api().entries(handle)


@contextmanager
def scandir_no_follow(path: Path) -> Iterator[Iterator]:
    """Enumerate an opened directory; consume entry metadata inside this scope."""
    if os.name == "nt":
        with locked_windows_directory(path) as handle, scandir_windows_handle(handle) as entries:
            yield entries
        return
    if os.name != "posix":
        raise OSError("Safe directory listing is not supported on this platform.")
    descriptor = open_posix_path_no_follow(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with os.scandir(descriptor) as entries:
            yield entries
    finally:
        os.close(descriptor)
