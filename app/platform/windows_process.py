"""Windows Job containment and ConPTY terminals, using only the standard library.

Children are created suspended and assigned to a kill-on-close Job before any
user code runs. ConPTY provides real console input (including Ctrl-C), unlike
redirected anonymous pipes. Requires Windows 10 1809 / Server 2019 or newer.
"""
from __future__ import annotations

import ctypes as c
import os
import subprocess
import threading
from collections.abc import Iterator
from ctypes import wintypes as w
from pathlib import Path

if os.name != "nt":
    raise ImportError("windows_process is only available on Windows")

k = c.WinDLL("kernel32", use_last_error=True)
SIZE_T = c.c_size_t
HANDLE = w.HANDLE


class SECURITY_ATTRIBUTES(c.Structure):
    _fields_ = [("nLength", w.DWORD), ("lpSecurityDescriptor", w.LPVOID), ("bInheritHandle", w.BOOL)]


class STARTUPINFO(c.Structure):
    _fields_ = [("cb", w.DWORD), ("lpReserved", w.LPWSTR), ("lpDesktop", w.LPWSTR),
               ("lpTitle", w.LPWSTR), ("dwX", w.DWORD), ("dwY", w.DWORD),
               ("dwXSize", w.DWORD), ("dwYSize", w.DWORD), ("dwXCountChars", w.DWORD),
               ("dwYCountChars", w.DWORD), ("dwFillAttribute", w.DWORD),
               ("dwFlags", w.DWORD), ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
               ("lpReserved2", c.POINTER(w.BYTE)), ("hStdInput", HANDLE),
               ("hStdOutput", HANDLE), ("hStdError", HANDLE)]


class STARTUPINFOEX(c.Structure):
    _fields_ = [("StartupInfo", STARTUPINFO), ("lpAttributeList", w.LPVOID)]


class PROCESS_INFORMATION(c.Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE), ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(c.Structure):
    _fields_ = [("PerProcessUserTimeLimit", c.c_int64), ("PerJobUserTimeLimit", c.c_int64),
               ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", SIZE_T),
               ("MaximumWorkingSetSize", SIZE_T), ("ActiveProcessLimit", w.DWORD),
               ("Affinity", SIZE_T), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]


class IO_COUNTERS(c.Structure):
    _fields_ = [(name, c.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount",
               "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(c.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
               ("ProcessMemoryLimit", SIZE_T), ("JobMemoryLimit", SIZE_T),
               ("PeakProcessMemoryUsed", SIZE_T), ("PeakJobMemoryUsed", SIZE_T)]


class COORD(c.Structure):
    _fields_ = [("X", c.c_short), ("Y", c.c_short)]


def _api(name, restype, *args):
    fn = getattr(k, name)
    fn.restype, fn.argtypes = restype, args
    return fn


CreateJobObject = _api("CreateJobObjectW", HANDLE, w.LPVOID, w.LPCWSTR)
SetInformationJobObject = _api("SetInformationJobObject", w.BOOL, HANDLE, c.c_int, w.LPVOID, w.DWORD)
AssignProcessToJobObject = _api("AssignProcessToJobObject", w.BOOL, HANDLE, HANDLE)
TerminateJobObject = _api("TerminateJobObject", w.BOOL, HANDLE, w.UINT)
CloseHandle = _api("CloseHandle", w.BOOL, HANDLE)
CreatePipe = _api("CreatePipe", w.BOOL, c.POINTER(HANDLE), c.POINTER(HANDLE), c.POINTER(SECURITY_ATTRIBUTES), w.DWORD)
SetHandleInformation = _api("SetHandleInformation", w.BOOL, HANDLE, w.DWORD, w.DWORD)
ReadFile = _api("ReadFile", w.BOOL, HANDLE, w.LPVOID, w.DWORD, c.POINTER(w.DWORD), w.LPVOID)
WriteFile = _api("WriteFile", w.BOOL, HANDLE, w.LPCVOID, w.DWORD, c.POINTER(w.DWORD), w.LPVOID)
CreateProcess = _api("CreateProcessW", w.BOOL, w.LPCWSTR, w.LPWSTR, w.LPVOID, w.LPVOID,
                     w.BOOL, w.DWORD, w.LPVOID, w.LPCWSTR, w.LPVOID, c.POINTER(PROCESS_INFORMATION))
ResumeThread = _api("ResumeThread", w.DWORD, HANDLE)
WaitForSingleObject = _api("WaitForSingleObject", w.DWORD, HANDLE, w.DWORD)
GetExitCodeProcess = _api("GetExitCodeProcess", w.BOOL, HANDLE, c.POINTER(w.DWORD))
TerminateProcess = _api("TerminateProcess", w.BOOL, HANDLE, w.UINT)
InitializeProcThreadAttributeList = _api("InitializeProcThreadAttributeList", w.BOOL, w.LPVOID, w.DWORD, w.DWORD, c.POINTER(SIZE_T))
UpdateProcThreadAttribute = _api("UpdateProcThreadAttribute", w.BOOL, w.LPVOID, w.DWORD, SIZE_T, w.LPVOID, SIZE_T, w.LPVOID, w.LPVOID)
DeleteProcThreadAttributeList = _api("DeleteProcThreadAttributeList", None, w.LPVOID)


def _check(value):
    if not value:
        raise c.WinError(c.get_last_error())
    return value


def _close(handle):
    if handle:
        CloseHandle(handle)


class WindowsJob:
    """Reusable containment. Assign a *suspended* process before resuming it."""
    def __init__(self):
        self._lock = threading.RLock()
        self.handle = _check(CreateJobObject(None, None))
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        try:
            _check(SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)))
        except BaseException:
            self.close()
            raise

    def assign(self, process_handle):
        with self._lock:
            _check(AssignProcessToJobObject(self.handle, process_handle))

    def terminate(self):
        with self._lock:
            if self.handle:
                _check(TerminateJobObject(self.handle, 1))

    def close(self):
        with self._lock:
            _close(self.handle)
            self.handle = None


def _pipe():
    read, write = HANDLE(), HANDLE()
    sa = SECURITY_ATTRIBUTES(c.sizeof(SECURITY_ATTRIBUTES), None, True)
    _check(CreatePipe(c.byref(read), c.byref(write), c.byref(sa), 0))
    return read.value, write.value


def _chunks(handle) -> Iterator[bytes]:
    buffer = c.create_string_buffer(4096)
    count = w.DWORD()
    while True:
        if not ReadFile(handle, buffer, len(buffer), c.byref(count), None):
            if c.get_last_error() in (6, 109, 232, 995):
                return
            raise c.WinError(c.get_last_error())
        if not count.value:
            return
        yield buffer.raw[:count.value]


class WindowsProcess:
    def __init__(self, argv: list[str], *, cwd: Path, env: dict[str, str], terminal: bool):
        self.job = WindowsJob()
        self.handle = self.input = self.output = self.error = self.console = None
        self.pid = 0
        self.returncode = None
        self.terminal = terminal
        self._closed = False
        self._write_lock = threading.Lock()
        self._console_lock = threading.Lock()
        child_handles = []
        attrs = None
        attrs_initialized = False
        pi = PROCESS_INFORMATION()
        try:
            in_read, self.input = _pipe()
            child_handles.append(in_read)
            self.output, out_write = _pipe()
            child_handles.append(out_write)
            for parent in (self.input, self.output):
                _check(SetHandleInformation(parent, 1, 0))
            si = STARTUPINFOEX()
            si.StartupInfo.cb = c.sizeof(si)
            flags = 0x4 | 0x400 | 0x80000  # SUSPENDED, UNICODE_ENVIRONMENT, EXTENDED_STARTUPINFO_PRESENT
            if terminal:
                try:
                    create_console = _api("CreatePseudoConsole", c.c_long, COORD, HANDLE, HANDLE, w.DWORD, c.POINTER(HANDLE))
                    self._close_console = _api("ClosePseudoConsole", None, HANDLE)
                except AttributeError as exc:
                    raise RuntimeError("Background terminals require Windows 10 1809 / Server 2019 or newer (ConPTY)") from exc
                console = HANDLE()
                result = create_console(COORD(120, 40), in_read, out_write, 0, c.byref(console))
                if result < 0:
                    raise OSError(f"CreatePseudoConsole failed: HRESULT 0x{result & 0xffffffff:08x}")
                self.console = console.value
                # NULL prevents redirected parent handles from being copied;
                # the new console replaces these with its own console handles.
                si.StartupInfo.dwFlags = 0x100
                si.StartupInfo.hStdInput = si.StartupInfo.hStdOutput = si.StartupInfo.hStdError = None
                size = SIZE_T()
                InitializeProcThreadAttributeList(None, 1, 0, c.byref(size))
                attrs = c.create_string_buffer(size.value)
                _check(InitializeProcThreadAttributeList(attrs, 1, 0, c.byref(size)))
                attrs_initialized = True
                _check(UpdateProcThreadAttribute(attrs, 0, 0x20016, self.console,
                                                 c.sizeof(HANDLE), None, None))
            else:
                self.error, err_write = _pipe()
                child_handles.append(err_write)
                _check(SetHandleInformation(self.error, 1, 0))
                # Restrict inheritance to this process's three pipe handles. Concurrent
                # shell launches must not keep each other's output pipes alive.
                size = SIZE_T()
                InitializeProcThreadAttributeList(None, 1, 0, c.byref(size))
                attrs = c.create_string_buffer(size.value)
                _check(InitializeProcThreadAttributeList(attrs, 1, 0, c.byref(size)))
                attrs_initialized = True
                inherited = (HANDLE * 3)(in_read, out_write, err_write)
                _check(UpdateProcThreadAttribute(attrs, 0, 0x20002, inherited,
                                                 c.sizeof(inherited), None, None))
                si.StartupInfo.dwFlags = 0x100
                si.StartupInfo.hStdInput, si.StartupInfo.hStdOutput, si.StartupInfo.hStdError = in_read, out_write, err_write
                flags |= 0x8000000  # CREATE_NO_WINDOW
            si.lpAttributeList = c.cast(attrs, w.LPVOID)
            block = c.create_unicode_buffer("\0".join(f"{key}={value}" for key, value in sorted(env.items(), key=lambda item: item[0].upper())) + "\0\0")
            cmdline = c.create_unicode_buffer(subprocess.list2cmdline(argv))
            _check(CreateProcess(argv[0], cmdline, None, None, not terminal, flags,
                                 block, str(cwd), c.byref(si), c.byref(pi)))
            self.handle, self.pid = pi.hProcess, pi.dwProcessId
            self.job.assign(self.handle)
            if ResumeThread(pi.hThread) == 0xffffffff:
                raise c.WinError(c.get_last_error())
        except BaseException:
            if pi.hProcess:
                TerminateProcess(pi.hProcess, 1)
            self.close()
            raise
        finally:
            _close(pi.hThread)
            for handle in child_handles:
                _close(handle)
            if attrs_initialized:
                DeleteProcThreadAttributeList(attrs)
        if terminal:
            # The console host keeps its output pipe open after the client exits.
            # Close it from a separate thread while the output reader drains it.
            threading.Thread(target=self._watch_terminal, daemon=True).start()

    def _finish_console(self):
        with self._console_lock:
            console, self.console = self.console, None
        if console:
            self._close_console(console)

    def _watch_terminal(self):
        WaitForSingleObject(self.handle, 0xffffffff)
        self.poll()
        try:
            self.job.terminate()
        except OSError:
            pass
        self._finish_console()

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        if self.handle and WaitForSingleObject(self.handle, 0) == 0:
            code = w.DWORD()
            _check(GetExitCodeProcess(self.handle, c.byref(code)))
            self.returncode = code.value
        return self.returncode

    def chunks(self) -> Iterator[bytes]:
        yield from _chunks(self.output)

    def stderr_chunks(self) -> Iterator[bytes]:
        if self.error:
            yield from _chunks(self.error)

    def close_stdin(self) -> None:
        with self._write_lock:
            _close(self.input)
            self.input = None

    def wait(self, timeout: float | None = None) -> int:
        code = self.poll()
        if code is not None:
            return code
        result = WaitForSingleObject(self.handle, 0xffffffff if timeout is None else max(0, int(timeout * 1000)))
        if result == 258:
            raise subprocess.TimeoutExpired("Windows process", timeout)
        if result == 0xffffffff:
            # A queued async wait may begin after shutdown released the handle.
            if self.returncode is not None:
                return self.returncode
            raise c.WinError(c.get_last_error())
        code = self.poll()
        assert code is not None
        return code

    def write(self, raw: bytes) -> int:
        # ConPTY is a console, whose Enter key is CR. Preserve the API's LF stdin
        # convention while sending the terminal equivalent to the console driver.
        data = raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r") if self.terminal else raw
        with self._write_lock:
            offset = 0
            while offset < len(data):
                count = w.DWORD()
                _check(WriteFile(self.input, data[offset:], len(data) - offset, c.byref(count), None))
                offset += count.value
        return len(raw)

    def interrupt(self):
        if self.poll() is None:
            self.write(b"\x03")

    def terminate(self):
        self.job.terminate()
        if self.handle:
            WaitForSingleObject(self.handle, 5000)
            self.poll()

    def communicate(self, timeout: int) -> tuple[bytes, bytes, int | None, bool]:
        _close(self.input)
        self.input = None
        buffers = [bytearray(), bytearray()]
        errors = []
        def read(index, handle):
            try:
                for chunk in _chunks(handle):
                    buffers[index].extend(chunk)
            except OSError as exc:
                errors.append(exc)
        threads = [threading.Thread(target=read, args=(i, handle), daemon=True)
                   for i, handle in enumerate((self.output, self.error))]
        for thread in threads:
            thread.start()
        timed_out = WaitForSingleObject(self.handle, timeout * 1000) == 258
        self.poll()
        # Always clean child processes, including children outliving the shell.
        self.job.terminate()
        for thread in threads:
            thread.join(5)
        if errors:
            raise errors[0]
        return bytes(buffers[0]), bytes(buffers[1]), None if timed_out else self.returncode, timed_out

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.job.close()
        # ClosePseudoConsole can wait for output drainage; the background reader
        # remains alive until it sees pipe EOF. Never close its read end first.
        if self.handle:
            WaitForSingleObject(self.handle, 5000)
            self.poll()
        self._finish_console()
        # Job shutdown releases any blocked WriteFile first. Serialize stdin
        # disposal with writes and a concurrent graceful close_stdin task.
        self.close_stdin()
        for name in ("output", "error", "handle"):
            _close(getattr(self, name))
            setattr(self, name, None)
