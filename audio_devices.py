"""Read and set Windows' default playback device per role (raw COM through ctypes)."""
import ctypes
import uuid
from ctypes import wintypes

ole32 = ctypes.OleDLL("ole32")
ole32.CoTaskMemFree.restype = None
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

E_RENDER = 0
ROLES = (0, 1, 2)  # console, multimedia, communications
CLSCTX_ALL = 0x17

CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
CLSID_PolicyConfigClient = "{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}"
IID_IPolicyConfig = "{F8679F50-850A-41CF-9C72-430F290290C8}"


def _guid(s):
    return (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID(s).bytes_le)


def _create(clsid, iid):
    ptr = ctypes.c_void_p()
    ole32.CoCreateInstance(ctypes.byref(_guid(clsid)), None, CLSCTX_ALL,
                           ctypes.byref(_guid(iid)), ctypes.byref(ptr))
    return ptr


def _call(obj, index, *args, argtypes=()):
    """Call vtable slot `index` of COM object `obj`; raises OSError on a failed HRESULT."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtbl[index])
    return fn(obj, *args)


def _release(obj):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(obj)


def default_output_ids():
    """{role: endpoint id} of the current default playback devices (ids as '{0.0.0.00000000}.{guid}')."""
    ids = {}
    enum = _create(CLSID_MMDeviceEnumerator, IID_IMMDeviceEnumerator)
    try:
        for role in ROLES:
            dev = ctypes.c_void_p()
            try:
                _call(enum, 4, E_RENDER, role, ctypes.byref(dev),
                      argtypes=(ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)))
            except OSError:
                continue  # no playback device at all
            pid = ctypes.c_wchar_p()
            _call(dev, 5, ctypes.byref(pid), argtypes=(ctypes.POINTER(ctypes.c_wchar_p),))
            ids[role] = pid.value.lower()
            ole32.CoTaskMemFree(ctypes.cast(pid, ctypes.c_void_p))
            _release(dev)
    finally:
        _release(enum)
    return ids


def set_default_output(endpoint_id, roles=ROLES):
    policy = _create(CLSID_PolicyConfigClient, IID_IPolicyConfig)
    try:
        for role in roles:
            _call(policy, 13, endpoint_id, role, argtypes=(wintypes.LPCWSTR, ctypes.c_int))
    finally:
        _release(policy)
