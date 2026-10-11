"""Hide the physical controllers from games with HidHide (only whitelisted apps - this one - still see
them), plus the Bluetooth LE battery percentage Windows keeps for paired devices."""
import ctypes
import json
import os
import subprocess
import sys

CLI = r"C:\Program Files\Nefarius Software Solutions\HidHide\x64\HidHideCLI.exe"
NO_WINDOW = 0x08000000


def available():
    return os.path.exists(CLI)


def _run(*args):
    out = subprocess.run([CLI, *args], capture_output=True, text=True, creationflags=NO_WINDOW, timeout=20)
    return out.stdout


def is_xbox(path):
    p = path.upper()
    return "VID_045E" in p and "PID_028E" not in p   # Xbox pads, but never ViGEm's virtual 360 pads


def _is_real_controller(path):
    return "VID_054C" in path.upper() or is_xbox(path)   # DualSense / DualSense Edge, or an Xbox pad


def gaming_devices():
    """Present HID gamepads that are real controllers: [{path, container}]."""
    try:
        groups = json.loads(_run("--dev-gaming") or "[]")
    except (ValueError, OSError, subprocess.SubprocessError):
        return []
    found = []
    for g in groups:
        for d in g.get("devices", []):
            if d.get("present") and _is_real_controller(d["deviceInstancePath"]):
                found.append({"path": d["deviceInstancePath"], "container": d.get("baseContainerDeviceInstancePath", "")})
    return found


def hidden_paths():
    out = _run("--dev-list")
    return {line.split('"')[1].upper() for line in out.splitlines() if line.startswith("--dev-hide")}


def protect(hide_xbox=True, games=(), forget=()):
    """Whitelist this app (and `games`: native-PS5 games that talk to the DualSense themselves), un-whitelist
    `forget`, hide the real controllers (Xbox pads only if hide_xbox), switch cloaking on.
    Returns (devices, paths whose hidden state just changed - each needs one reconnect)."""
    exes = {os.path.abspath(sys.executable), os.path.abspath(getattr(sys, "_base_executable", sys.executable))}
    exes |= {os.path.join(os.path.dirname(e), n) for e in list(exes) for n in ("python.exe", "pythonw.exe")}
    exes |= set(games)
    devices = gaming_devices()
    hidden = hidden_paths()
    want = {d["path"] for d in devices if hide_xbox or not is_xbox(d["path"])}
    hide = [p for p in want if p.upper() not in hidden]
    show = [d["path"] for d in devices if d["path"] not in want and d["path"].upper() in hidden]
    args = []
    for e in sorted(exes):
        if os.path.exists(e):
            args += ["--app-reg", e]
    for e in forget:
        args += ["--app-unreg", e]
    for p in hide:
        args += ["--dev-hide", p]
    for p in show:
        args += ["--dev-unhide", p]
    _run(*args, "--cloak-on")
    return devices, hide + show


def release():
    """Let games see the real controllers again (used when the app quits)."""
    _run("--cloak-off")


# ---- Bluetooth LE battery (the % shown in Windows Settings) ----
_cfg = ctypes.WinDLL("cfgmgr32")


class _DevPropKey(ctypes.Structure):
    _fields_ = [("fmtid", ctypes.c_byte * 16), ("pid", ctypes.c_uint32)]


def _battery_key():
    import uuid
    k = _DevPropKey()
    ctypes.memmove(k.fmtid, uuid.UUID("{104EA319-6EE2-4701-BD47-8DDBF425BBE5}").bytes_le, 16)
    k.pid = 2
    return k


_BATTERY_KEY = _battery_key()


def ble_battery(instance_id):
    """Battery percent of a BTHLE device node, or None."""
    if not instance_id.upper().startswith("BTHLE"):
        return None
    inst = ctypes.c_uint32()
    if _cfg.CM_Locate_DevNodeW(ctypes.byref(inst), ctypes.c_wchar_p(instance_id), 0) != 0:
        return None
    ptype, value, size = ctypes.c_uint32(), ctypes.c_ubyte(), ctypes.c_uint32(1)
    if _cfg.CM_Get_DevNode_PropertyW(inst, ctypes.byref(_BATTERY_KEY), ctypes.byref(ptype),
                                     ctypes.byref(value), ctypes.byref(size), 0) != 0:
        return None
    return value.value
