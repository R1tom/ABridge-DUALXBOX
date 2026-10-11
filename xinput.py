"""Just enough XInput to read a real Xbox controller (incl. the Guide button), rumble it and
tell it apart from ViGEm's virtual Xbox 360 pads."""
import ctypes

_x = ctypes.WinDLL("xinput1_4")

VIRTUAL_360 = (0x045E, 0x028E)   # what ViGEm's virtual pads (and real wired 360 pads) report
BATTERY_TYPE_DISCONNECTED, BATTERY_TYPE_WIRED = 0x00, 0x01
BATTERY_LEVELS = {0: 5, 1: 20, 2: 55, 3: 100}   # EMPTY/LOW/MEDIUM/FULL -> rough percent


class Gamepad(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_uint16), ("bLeftTrigger", ctypes.c_ubyte), ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short),
                ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]


class State(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint32), ("Gamepad", Gamepad)]


class CapabilitiesEx(ctypes.Structure):
    _fields_ = [("Type", ctypes.c_ubyte), ("SubType", ctypes.c_ubyte), ("Flags", ctypes.c_uint16),
                ("Gamepad", Gamepad), ("wLeftMotorSpeed", ctypes.c_uint16), ("wRightMotorSpeed", ctypes.c_uint16),
                ("VendorId", ctypes.c_uint16), ("ProductId", ctypes.c_uint16), ("VersionNumber", ctypes.c_uint16),
                ("unk1", ctypes.c_uint16), ("unk2", ctypes.c_uint32)]


class Vibration(ctypes.Structure):
    _fields_ = [("wLeftMotorSpeed", ctypes.c_uint16), ("wRightMotorSpeed", ctypes.c_uint16)]


class Battery(ctypes.Structure):
    _fields_ = [("BatteryType", ctypes.c_ubyte), ("BatteryLevel", ctypes.c_ubyte)]


# Undocumented exports by ordinal: 100 = XInputGetStateEx (adds Guide, 0x0400), 108 = XInputGetCapabilitiesEx
_get_state_ex = ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(State))((100, _x))
_get_caps_ex = ctypes.WINFUNCTYPE(ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.POINTER(CapabilitiesEx))((108, _x))
_x.XInputSetState.argtypes = [ctypes.c_uint32, ctypes.POINTER(Vibration)]
_x.XInputGetBatteryInformation.argtypes = [ctypes.c_uint32, ctypes.c_ubyte, ctypes.POINTER(Battery)]


def ids(slot):
    """(vid, pid) of the pad in an XInput slot, or None if the slot is empty."""
    c = CapabilitiesEx()
    if _get_caps_ex(1, slot, 0, ctypes.byref(c)) != 0:
        return None
    return c.VendorId, c.ProductId


def find_real_pad():
    """First XInput slot holding a controller that isn't a ViGEm virtual pad: (slot, pid) or None."""
    for slot in range(4):
        found = ids(slot)
        if found and found != VIRTUAL_360:
            return slot, found[1]
    return None


def get_state(slot):
    """(packet number, Gamepad) or None; the packet number only changes when the input does."""
    s = State()
    if _get_state_ex(slot, ctypes.byref(s)) != 0:
        return None
    return s.dwPacketNumber, s.Gamepad


def set_rumble(slot, large, small):
    """large/small 0-255 like the DualSense side."""
    _x.XInputSetState(slot, ctypes.byref(Vibration(large * 257, small * 257)))


def battery(slot):
    """(type, rough percent) from XInput; type is BATTERY_TYPE_* ."""
    b = Battery()
    if _x.XInputGetBatteryInformation(slot, 0, ctypes.byref(b)) != 0:
        return BATTERY_TYPE_DISCONNECTED, None
    return b.BatteryType, BATTERY_LEVELS.get(b.BatteryLevel)
