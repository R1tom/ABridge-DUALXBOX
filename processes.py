"""Which programs are running: exe names of all processes (cheap, polled every second) and the
windowed ones with full paths (for the "Add game" picker)."""
import ctypes
import os
from ctypes import wintypes

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_u32 = ctypes.WinDLL("user32")
_k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
_k32.OpenProcess.restype = ctypes.c_void_p
_k32.CloseHandle.argtypes = [ctypes.c_void_p]

TH32CS_SNAPPROCESS = 0x2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
INVALID_HANDLE = ctypes.c_void_p(-1).value


class _Entry(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]


def running_names():
    """Lower-case exe names of every running process."""
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE:
        return set()
    names = set()
    e = _Entry()
    e.dwSize = ctypes.sizeof(e)
    try:
        ok = _k32.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(e))
        while ok:
            names.add(e.szExeFile.lower())
            ok = _k32.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(e))
    finally:
        _k32.CloseHandle(snap)
    return names


class _UnicodeString(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]


class _ProcessIdInfo(ctypes.Structure):
    _fields_ = [("ProcessId", ctypes.c_void_p), ("ImageName", _UnicodeString)]


def _nt_device_map():
    """'\\Device\\HarddiskVolume3' -> 'D:' for every drive letter."""
    out = {}
    buf = ctypes.create_unicode_buffer(1024)
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if _k32.QueryDosDeviceW(f"{letter}:", buf, len(buf)):
            out[buf.value.lower()] = f"{letter}:"
    return out


def _exe_path_unopened(pid):
    """Full exe path without opening the process - anti-cheat (EAC, BattlEye) blocks OpenProcess on games."""
    buf = ctypes.create_unicode_buffer(1024)
    info = _ProcessIdInfo(ctypes.c_void_p(pid), _UnicodeString(0, ctypes.sizeof(buf), ctypes.cast(buf, ctypes.c_void_p)))
    if ctypes.windll.ntdll.NtQuerySystemInformation(88, ctypes.byref(info), ctypes.sizeof(info), None) != 0:
        return None   # 88 = SystemProcessIdInformation
    nt = ctypes.wstring_at(info.ImageName.Buffer, info.ImageName.Length // 2)
    for dev, drive in _nt_device_map().items():
        if nt.lower().startswith(dev + "\\"):
            return drive + nt[len(dev):]
    return None


def exe_path(pid):
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return _exe_path_unopened(pid)
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if _k32.QueryFullProcessImageNameW(ctypes.c_void_p(h), 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        _k32.CloseHandle(h)


def windowed_programs():
    """[(window title, exe path)] of visible top-level windows, one per exe, this app excluded."""
    found = {}
    me = os.getpid()
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        n = _u32.GetWindowTextLengthW(hwnd)
        if n and _u32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != me:
                path = exe_path(pid.value)
                if path and path.lower() not in found and not path.lower().startswith(os.environ.get("WINDIR", r"C:\Windows").lower()):
                    title = ctypes.create_unicode_buffer(n + 1)
                    _u32.GetWindowTextW(hwnd, title, n + 1)
                    found[path.lower()] = (title.value, path)
        return True
    _u32.EnumWindows(proto(cb), 0)
    return sorted(found.values(), key=lambda t: os.path.basename(t[1]).lower())
