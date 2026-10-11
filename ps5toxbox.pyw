"""PadBridge: PS5 (DualSense) and Xbox controllers on Windows PC.
DualSense -> virtual Xbox 360 controller (ViGEmBus) with rumble, adaptive triggers and audio haptics;
Xbox controller -> calibrated virtual copy with impulse-trigger effects over Bluetooth;
per-game trigger profiles, battery alarm and battery saver."""
import ctypes
import glob
import json
import math
import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser
import winreg
import zlib
import tkinter as tk
from tkinter import ttk, colorchooser, messagebox

import hid
import pystray
import vgamepad as vg
from PIL import Image, ImageDraw
try:
    import sv_ttk   # Windows 11 look; the app still works with plain ttk without it
except ImportError:
    sv_ttk = None

import audio_devices
import hidhide
import processes
import xinput

APP_NAME = "PadBridge"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
STARTUP_CMD = os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs\Startup\PS5toXbox.cmd")

SONY_VID = 0x054C
MODELS = {0x0CE6: "DualSense", 0x0DF2: "DualSense Edge"}
HID_BUS_BLUETOOTH = 2
BT_HID_SERVICE = "00001124-0000-1000-8000-00805f9b34fb"
SONY_UPDATER_PAGE = "https://controller.dl.playstation.net/controller/lang/en/fwupdater.html"
UNINSTALL_KEYS = [
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
]

DEFAULTS = {
    "touchpad_as_back": True,
    "player_leds": True,
    "rumble_strength": 100,
    "lightbar": [0, 90, 255],
    "calibration": None,     # set by the calibration wizard; None = factory centre/range
    "stick_deadzone": 0,     # percent, radial
    "trigger_deadzone": 0,   # percent
    "audio_haptics": True,   # feel the PC's sound through the haptic motors (USB only)
    "audio_haptics_strength": 100,
    "audio_haptics_gate": 30,  # percent of HAPTIC_GATE_FULL; quieter bass (music, ambience) is not felt
    "audio_output": None,   # last real (non-controller) default playback device
    "battery_warning": 20,   # percent; tray warning below this (both controllers)
    "adaptive_triggers": True,
    "trigger_default": {"l2": "off", "r2": "off", "kick": False},   # games not in the list
    "games": {},             # exe name (lower) -> {"path", "native", "l2", "r2", "kick", "recoil"}
    "mute_switches_gun": True,   # mic button cycles R2 through the gun presets for the current game
    "hidhide_games": [],     # game exes we let see the real DualSense (native games), to undo later
    "theme": "system",       # "system" (follow Windows), "dark" or "light"
    "battery_saver": "auto",     # "off", "on", or "auto" = on while a controller is below battery_saver_below %
    "battery_saver_below": 30,
    "lightbar_brightness": 60,   # percent of the lightbar colour (it is lit the whole time)
    "xbox":{"mode": "virtual",   # "virtual" = hide it, games get a corrected copy; "direct" = games use it as is
             "rumble_strength": 100, "calibration": None, "stick_deadzone": 0, "trigger_deadzone": 0,
             "trigger_rumble": True,          # a held LT/RT buzzes (impulse triggers) when the game rumbles
             "trigger_rumble_strength": 100},
}

# Xbox Bluetooth HID rumble: output report 0x03 = [enable mask, LT, RT, strong, weak (0-100 each),
# on-time (10 ms units), off-time, repeats]. The only way to reach the trigger motors from a background
# app: XInput has no trigger motors and Windows.Gaming.Input ignores apps that aren't in the foreground.
XBOX_RUMBLE_REPORT = 0x03
XBOX_RUMBLE_ALL = 0x0F
XBOX_TRIGGER_HELD = 25      # 0-255 trigger value that counts as "held"
XBOX_RUMBLE_REFRESH = 1.0   # seconds; re-send a running effect before its 2.55 s on-time runs out

HAPTIC_RATE = 48000
HAPTIC_CUTOFF = 250      # Hz; the voice-coil actuators only reproduce the low end
HAPTIC_MAX_LAG = 0.04    # seconds of queued haptics before blocks get dropped
HAPTIC_GATE_FULL = 0.3   # bass RMS that the gate slider's 100% stands for
HAPTIC_RELEASE = 0.12    # seconds for the gate to fade out after a hit

# Raw DualSense ranges: per stick axis [min, centre, max]; per trigger its full-press value.
DEFAULT_CAL = {"lx": [0, 127.5, 255], "ly": [0, 127.5, 255],
               "rx": [0, 127.5, 255], "ry": [0, 127.5, 255], "l2": 255, "r2": 255}

# Xbox 360 (XUSB) button bits
UP, DOWN, LEFT, RIGHT = 0x0001, 0x0002, 0x0004, 0x0008
START, BACK, LS, RS = 0x0010, 0x0020, 0x0040, 0x0080
LB, RB, GUIDE = 0x0100, 0x0200, 0x0400
A, B, X, Y = 0x1000, 0x2000, 0x4000, 0x8000
HAT = {0: UP, 1: UP | RIGHT, 2: RIGHT, 3: DOWN | RIGHT, 4: DOWN, 5: DOWN | LEFT, 6: LEFT, 7: UP | LEFT}
BUTTON_NAMES = [(A, "A"), (B, "B"), (X, "X"), (Y, "Y"), (LB, "LB"), (RB, "RB"), (BACK, "View"),
                (START, "Menu"), (GUIDE, "Guide"), (LS, "LS"), (RS, "RS"),
                (UP, "Up"), (DOWN, "Down"), (LEFT, "Left"), (RIGHT, "Right")]

# DualSense player-indicator LED patterns for XInput slots 1-4
PLAYER_LEDS = [0x04, 0x0A, 0x15, 0x1B]


# ---- Adaptive triggers: 11-byte effect blocks (Sony's standard effect formats) ----
def _zones(mode, start, level, extra=b""):
    """Effect `mode` from trigger zone `start` (0-9) to the end, `level` 1-8 in every zone."""
    active, levels = 0, 0
    for i in range(start, 10):
        active |= 1 << i
        levels |= (level - 1) << (3 * i)
    return (bytes([mode]) + active.to_bytes(2, "little") + levels.to_bytes(4, "little") + extra).ljust(11, b"\0")


def trigger_off():
    return b"\x05".ljust(11, b"\0")


def trigger_feedback(start, strength):
    """Steady resistance from `start` on."""
    return _zones(0x21, start, strength)


def trigger_weapon(start, end, strength):
    """Resistance between two points that gives way with a click - a gun's hammer."""
    return (bytes([0x25]) + ((1 << start) | (1 << end)).to_bytes(2, "little") + bytes([strength - 1])).ljust(11, b"\0")


def trigger_vibration(start, amplitude, hz):
    """The trigger itself buzzes from `start` on."""
    return _zones(0x26, start, amplitude, bytes([0, 0, hz]))


TRIGGER_STYLES = {   # config value -> (label, effect)
    "off": ("Off", trigger_off()),
    "light": ("Light resistance", trigger_feedback(3, 3)),
    "heavy": ("Heavy resistance (bow, brake)", trigger_feedback(1, 7)),
    "gun": ("Gun - tight, then clicks (hammer)", trigger_weapon(4, 7, 8)),
    "stiff_gun": ("Stiff gun - clicks late", trigger_weapon(2, 8, 8)),
    "rattle": ("Machine gun - rattles when pulled", trigger_vibration(3, 6, 12)),
}
KICK = [trigger_vibration(0, a, 35) for a in range(1, 9)]   # rumble kick, by strength 1-8


# ---- Gun feel: the trigger acts like a weapon's and "fires" in the controller when pulled ----
# rest: effect until the shot breaks; fire_at: raw trigger value (0-255) that fires;
# mode: semi (one shot per pull), auto (fires while held), burst (`burst` shots per pull);
# rpm: rate of fire; kick: (amplitude 1-8, Hz, seconds) the trigger buzzes per shot;
# recoil: (large, small) motor pulse per shot, for the kick's duration.
def _gun(label, rest, fire_at, mode="semi", rpm=0, burst=0, kick=(8, 30, 0.06), recoil=(0, 0)):
    return {"label": label, "rest": rest, "fire_at": fire_at, "mode": mode, "rpm": rpm, "burst": burst,
            "kick": kick, "kick_fx": trigger_vibration(0, kick[0], kick[1]), "recoil": recoil}


GUNS = {
    "gun_pistol": _gun("Gun: Pistol - crisp click, single shot", trigger_weapon(3, 6, 5), 190,
                       kick=(6, 40, 0.06), recoil=(60, 200)),
    "gun_assault": _gun("Gun: Assault rifle - full auto", trigger_weapon(3, 5, 4), 165, "auto", rpm=650,
                        kick=(7, 35, 0.04), recoil=(130, 90)),
    "gun_burst": _gun("Gun: Burst rifle - 3 rounds per pull", trigger_weapon(3, 6, 6), 190, "burst", rpm=800, burst=3,
                      kick=(7, 35, 0.035), recoil=(120, 110)),
    "gun_smg": _gun("Gun: SMG - fast, light rattle", trigger_feedback(3, 2), 120, "auto", rpm=900,
                    kick=(5, 50, 0.028), recoil=(50, 140)),
    "gun_lmg": _gun("Gun: Machine gun (LMG) - heavy auto", trigger_feedback(2, 5), 120, "auto", rpm=550,
                    kick=(8, 28, 0.05), recoil=(210, 70)),
    "gun_shotgun": _gun("Gun: Shotgun - stiff pull, big blast", trigger_weapon(2, 7, 8), 215,
                        kick=(8, 20, 0.14), recoil=(255, 200)),
    "gun_sniper": _gun("Gun: Sniper - long heavy pull, hard break", trigger_weapon(5, 8, 8), 230,
                       kick=(8, 25, 0.11), recoil=(255, 120)),
}
def step_guns(guns, vals, states, recoil_on):
    """Simulated shots: past the trigger's break point it kicks (and the body recoils) once per
    shot - one per pull, a burst, or at the gun's rate of fire while held.
    guns: {"l2"/"r2": GUNS entry or None}; vals: raw trigger values {"l2", "r2"} (0-255) or None;
    states: per-trigger shot state, kept between calls.
    Returns ({trigger: None | "rest" | "kick" | "free"}, body recoil (large, small))."""
    now = time.perf_counter()
    recoil, phase = [0, 0], {}
    for t in ("l2", "r2"):
        g, st = guns.get(t), states[t]
        if g is None or vals is None:
            st.update(armed=True, shots=0, kick_until=0.0)
            phase[t] = None
            continue
        v = vals[t]
        if v < g["fire_at"] * 0.6:
            st.update(armed=True, shots=0)   # trigger let go: the next pull fires again
        shoot = False
        if st["armed"] and v >= g["fire_at"]:
            st["armed"], shoot = False, True
        elif not st["armed"] and st["shots"] and now >= st["next"]:
            shoot = g["mode"] == "auto" or (g["mode"] == "burst" and st["shots"] < g["burst"])
        if shoot:
            st["shots"] += 1
            st["kick_until"] = now + g["kick"][2]
            st["next"] = now + 60 / g["rpm"] if g["rpm"] else float("inf")
        if now < st["kick_until"]:
            phase[t] = "kick"
            if recoil_on:
                recoil = [max(a, b) for a, b in zip(recoil, g["recoil"])]
        else:
            phase[t] = "rest" if st["armed"] else "free"
    return phase, tuple(recoil)


def new_gun_states():
    return {t: {"armed": True, "shots": 0, "next": 0.0, "kick_until": 0.0} for t in ("l2", "r2")}


# Xbox trigger motors (no resistance, only vibration): per-game choice for LT/RT
XBOX_STYLES = {"rumble": "Buzz with the game's rumble (while held)",
               "aim": "Aim - soft click when you aim, then buzz with the game's rumble",
               "off": "Off", "rattle": "Machine gun - buzzes while pulled"}


BOULDER_LAUNCH = 0.12   # s of fixed launch thump right at the pull (the game's own rumble may start later)
BOULDER_WAIT = 0.5      # s the surge waits for the game to start rumbling before giving up
BOULDER_QUIET = 0.15    # s of game silence that ends the surge
BOULDER_BOOST = 1.6     # game rumble is amplified this much during the surge


def boulder_env(t, level, st):
    """Launching a boulder (CONTROL's power surge): (trigger 0-100, body large, small 0-255), None when over.
    Only the launch thump is timed; the surge follows the game's own rumble (`level`, 0-255) for as long
    and as hard as the game makes it, boosted and throbbing. `st` keeps "loud": last time the game rumbled."""
    if t < BOULDER_LAUNCH:
        return 100, 255, 255
    now = st["start"] + t
    if level > 10:
        st["loud"] = now
    loud = st.get("loud")
    if (loud is None and t > BOULDER_WAIT) or (loud is not None and now - loud > BOULDER_QUIET):
        return None
    g = min(1.0, level / 255 * BOULDER_BOOST)
    pulse = 0.65 + 0.35 * abs(math.sin(now * 2 * math.pi * 7))   # ~7 Hz throb
    return 100 * g * pulse, 255 * g * pulse, 160 * g


# Abilities on the Xbox triggers: one per pull past fire_at (raw 0-255), next pull after dropping below rearm.
POWERS = {
    "power_boulder": {"label": "Power: Launch a boulder - thump, then the game's surge boosted",
                      "fire_at": 170, "rearm": 100, "env": boulder_env},
}

XBOX_STYLE_KEYS = list(XBOX_STYLES) + list(POWERS) + list(GUNS)
XBOX_STYLE_LABELS = (list(XBOX_STYLES.values()) + [w["label"] for w in POWERS.values()]
                     + [g["label"] for g in GUNS.values()])
XBOX_RATTLE = 35            # 0-100 motor level of the "rattle" style
XBOX_AIM = (128, 77, 40, 0.045)

# ---- Battery ----
SAVER_RUMBLE = 0.5          # battery saver: body rumble scale
SAVER_TRIGGERS = 0.5        # battery saver: Xbox trigger motor scale
SAVER_LIGHTBAR = 10         # battery saver: lightbar brightness, percent
STUCK_RUMBLE_S = 8.0        # unchanged rumble + untouched controller this long = the game left it on


def saver_on(cfg, pct, charging):
    """Battery saver for one controller: forced on/off, or automatic below the threshold (not while charging)."""
    mode = cfg.get("battery_saver", "auto")
    if mode == "auto":
        return pct is not None and not charging and pct < cfg.get("battery_saver_below", 30)
    return mode == "on"


class RumbleGuard:
    """Stops rumble a game leaves running (paused, menu, alt-tabbed): once the same nonzero rumble has run
    unchanged for STUCK_RUMBLE_S while nobody touches the controller, it is muted until the game changes it."""

    def __init__(self):
        self.value, self.since, self.touched, self.last = (0, 0), 0.0, time.time(), None

    def touch(self, report):
        """report: (buttons, lt, rt, lx, ly, rx, ry). Stick/trigger noise doesn't count as a touch."""
        last = self.last
        if (last is None or report[0] != last[0] or any(abs(a - b) > 20 for a, b in zip(report[1:3], last[1:3]))
                or any(abs(a - b) > 3000 for a, b in zip(report[3:], last[3:]))):
            self.touched, self.last = time.time(), report

    def filter(self, rumble):
        now = time.time()
        if rumble != self.value:
            self.value, self.since = rumble, now
        if any(rumble) and now - self.since > STUCK_RUMBLE_S and now - self.touched > STUCK_RUMBLE_S:
            return 0, 0
        return rumble   # "aim": clicks past 50% (raw 128), re-arms below 30%; level 0-100, seconds


STYLE_KEYS = list(TRIGGER_STYLES) + list(GUNS)
STYLE_LABELS = [v[0] for v in TRIGGER_STYLES.values()] + [g["label"] for g in GUNS.values()]
MUTE_BUTTON = 0x04   # report byte b2: the mic (mute) button - unused by Xbox games, so it switches guns


def tune_process_for_latency():
    """Keep input forwarding instant even while a game loads the CPU or the app sits in the tray."""
    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    proc = ctypes.c_void_p(k32.GetCurrentProcess())
    k32.SetPriorityClass(proc, 0x00008000)   # ABOVE_NORMAL_PRIORITY_CLASS

    # Opt out of Windows 11 power throttling (EcoQoS / slow cores and coarse timers for background apps).
    class PowerThrottling(ctypes.Structure):
        _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong), ("StateMask", ctypes.c_ulong)]
    execution_speed, ignore_timer_resolution = 0x1, 0x4
    state = PowerThrottling(1, execution_speed | ignore_timer_resolution, 0)
    k32.SetProcessInformation(proc, 4, ctypes.byref(state), ctypes.sizeof(state))   # ProcessPowerThrottling
    # 0.5 ms system timer (Windows' finest; games usually ask for this too): the Xbox poll loop's sleep
    # drops from ~0.68 ms to ~0.50 ms, so input waits ~0.25 ms on average instead of ~0.34 ms.
    ctypes.windll.ntdll.NtSetTimerResolution(5000, 1, ctypes.byref(ctypes.c_ulong()))
    # Let an input thread that just woke take the GIL from audio/UI work within 0.2 ms instead of 5 ms.
    sys.setswitchinterval(0.0002)


def realtime_thread():
    """Called at the start of the controller threads: they sleep almost always, so this costs nothing."""
    k32 = ctypes.windll.kernel32
    k32.GetCurrentThread.restype = ctypes.c_void_p
    k32.SetThreadPriority(ctypes.c_void_p(k32.GetCurrentThread()), 15)   # THREAD_PRIORITY_TIME_CRITICAL


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    cfg["xbox"] = {**DEFAULTS["xbox"], **cfg.get("xbox", {})}
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def find_dualsense():
    for info in hid.enumerate(SONY_VID):
        if info["product_id"] in MODELS:
            return info
    return None


def find_sony_updater():
    """Path to Sony's official 'Firmware updater for DualSense' if installed."""
    for hive, root in UNINSTALL_KEYS:
        try:
            key = winreg.OpenKey(hive, root)
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(key)[0]):
            try:
                sub = winreg.OpenKey(key, winreg.EnumKey(key, i))
                name = winreg.QueryValueEx(sub, "DisplayName")[0].lower()
            except OSError:
                continue
            if "dualsense" not in name or "updater" not in name:
                continue
            for value in ("DisplayIcon", "InstallLocation"):
                try:
                    loc = winreg.QueryValueEx(sub, value)[0].split(",")[0].strip('"')
                except OSError:
                    continue
                if loc.lower().endswith(".exe") and os.path.exists(loc):
                    return loc
                exes = [e for e in glob.glob(os.path.join(loc, "*.exe")) if "unins" not in e.lower()]
                if exes:
                    return exes[0]
    return None


def tray_image():
    """A small white gamepad on an Xbox-green disc."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, 63, 63), fill=(16, 124, 16, 255))
    d.rounded_rectangle((10, 20, 54, 42), radius=10, fill="white")
    d.ellipse((8, 26, 26, 50), fill="white")
    d.ellipse((38, 26, 56, 50), fill="white")
    green = (16, 124, 16, 255)
    d.rectangle((17, 28, 27, 31), fill=green)
    d.rectangle((20, 25, 23, 35), fill=green)
    d.ellipse((38, 24, 43, 29), fill=green)
    d.ellipse((44, 30, 49, 35), fill=green)
    return img


def norm_axis(v, lo, mid, hi):
    """Raw 0-255 reading -> -1..1 around the calibrated centre, each side scaled separately."""
    if v >= mid:
        return min((v - mid) / max(hi - mid, 1), 1.0)
    return max((v - mid) / max(mid - lo, 1), -1.0)


def map_stick(x, y, cal, side, deadzone):
    """Calibrated stick -> Xbox axes, with a radial deadzone that rescales so output still starts at 0."""
    nx = norm_axis(x, *cal[side + "x"])
    ny = norm_axis(y, *cal[side + "y"])
    mag = math.hypot(nx, ny)
    if mag <= deadzone:
        return 0, 0
    scale = min((mag - deadzone) / (1 - deadzone), 1.0) / mag
    return int(nx * scale * 32767), int(-ny * scale * 32767)


def map_trigger(v, full, deadzone):
    t = min(v / max(full, 1), 1.0)
    if t <= deadzone:
        return 0
    return int(round((t - deadzone) / (1 - deadzone) * 255))


class DualSense:
    def __init__(self, info):
        self.name = MODELS[info["product_id"]]
        path = info["path"]
        self.bt = info.get("bus_type") == HID_BUS_BLUETOOTH or BT_HID_SERVICE in path.decode(errors="ignore").lower()
        self.dev = hid.device()
        self.dev.open_path(path)
        self.lock = threading.Lock()
        self.seq = 0
        self.rumbling = False
        # Reading calibration switches a Bluetooth DualSense from its reduced
        # 0x01 report into the full 0x31 report (touchpad, battery, etc.).
        try:
            self.dev.get_feature_report(0x05, 41)
        except OSError:
            pass
        try:
            fw = self.dev.get_feature_report(0x20, 64)
            self.update_version = fw[44] | fw[45] << 8
            self.build_date = bytes(fw[1:12]).decode("ascii", "ignore").strip()
        except (OSError, IndexError):
            self.update_version = 0
            self.build_date = ""
        # Firmware 2.24+ only honours rumble through the newer "vibration v2" flag.
        self.vibration_v2 = self.update_version >= 0x0224
        # Release the lightbar from the firmware's startup animation so our colour sticks.
        common = bytearray(47)
        common[38] = 0x02
        common[41] = 0x02
        self._write(common)

    def close(self):
        try:
            self.dev.close()
        except OSError:
            pass

    def read(self, timeout_ms):
        r = self.dev.read(80, timeout_ms)
        if not r:
            return None
        if self.bt:
            if r[0] == 0x31:
                return self._parse_full(r, 2)
            if r[0] == 0x01:
                return self._parse_simple(r)
            return None
        if r[0] == 0x01:
            return self._parse_full(r, 1)
        return None

    @staticmethod
    def _parse_full(r, o):
        status = r[o + 52]
        return {"lx": r[o], "ly": r[o + 1], "rx": r[o + 2], "ry": r[o + 3],
                "l2": r[o + 4], "r2": r[o + 5],
                "b0": r[o + 7], "b1": r[o + 8], "b2": r[o + 9],
                "battery": min((status & 0x0F) * 10 + 5, 100),
                "charging": status >> 4}

    @staticmethod
    def _parse_simple(r):
        return {"lx": r[1], "ly": r[2], "rx": r[3], "ry": r[4],
                "b0": r[5], "b1": r[6], "b2": r[7], "l2": r[8], "r2": r[9],
                "battery": None, "charging": 0}

    def send(self, large, small, rgb, player_leds, triggers=None):
        if large or small or self.rumbling:
            self._send(large, small, rgb, player_leds, triggers, rumble=True)
            self.rumbling = bool(large or small)
            if self.rumbling:
                return
        # With no game rumble, leave the rumble-emulation flags off so audio haptics get through.
        self._send(0, 0, rgb, player_leds, triggers, rumble=False)

    def _send(self, large, small, rgb, player_leds, triggers, rumble):
        common = bytearray(47)
        if rumble:
            common[0] = 0x02 if self.vibration_v2 else 0x03   # haptics select (+ legacy rumble flag)
            common[38] = 0x04 if self.vibration_v2 else 0x00
        if triggers:
            common[0] |= 0x04 | 0x08                      # right + left trigger effects
            common[10:21], common[21:32] = triggers[1], triggers[0]
        common[1] = 0x04 | 0x10                          # lightbar + player LEDs
        common[38] |= 0x01                                # player LED brightness control:
        common[42] = 0x02                                 # low (still easy to read, less battery)
        common[2] = small                                 # right motor: high-frequency
        common[3] = large                                 # left motor: low-frequency
        common[43] = player_leds
        common[44:47] = bytes(rgb)
        self._write(common)

    def _write(self, common):
        if self.bt:
            rep = bytearray(78)
            rep[0] = 0x31
            rep[1] = (self.seq << 4) & 0xF0
            rep[2] = 0x10
            rep[3:50] = common
            self.seq = (self.seq + 1) & 0x0F
            crc = zlib.crc32(b"\xa2" + bytes(rep[:74]))
            rep[74:78] = crc.to_bytes(4, "little")
        else:
            rep = bytearray(48)
            rep[0] = 0x02
            rep[1:48] = common
        with self.lock:
            self.dev.write(bytes(rep))


class Bridge(threading.Thread):
    def __init__(self, cfg):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.status = "Starting..."
        self.state = None
        self.buttons = 0
        self.output = None
        self.rumble = (0, 0)
        self.xinput_slot = None
        self.test_until = 0.0
        self.firmware = ""
        self.paused = threading.Event()
        self.stopping = threading.Event()
        self.triggers = None       # adaptive-trigger profile {"l2", "r2", "kick"}, set by GameWatcher
        self.native_game = None    # a game with PS5 support is running: it drives the controller itself
        self.guns = {t: {"armed": True, "shots": 0, "next": 0.0, "kick_until": 0.0} for t in ("l2", "r2")}
        self.gun_fx = {"l2": None, "r2": None}   # trigger effect while a gun is firing / after its break
        self.recoil = (0, 0)
        self.gun_switched = 0      # bumped when the mic button changes gun, so the UI can refresh
        self.rguard = RumbleGuard()
        self.game_rumble = (0, 0)  # game rumble after the stuck-rumble guard
        self.saving = False        # battery saver active for this controller
        self._mute_held = False
        self.pad = None
        self._plug()
        self._last_report = None

    def _plug(self):
        if self.pad is None:
            self.pad = vg.VX360Gamepad()
            self.pad.register_notification(callback_function=self._on_rumble)

    def _unplug(self):
        # Removing the virtual pad means the native game sees only the real DualSense (no double input).
        if self.pad is not None:
            self.pad.unregister_notification()
            self.pad = None
        self.xinput_slot, self.rumble = None, (0, 0)

    def _on_rumble(self, client, target, large_motor, small_motor, led_number, user_data):
        self.rumble = (large_motor, small_motor)
        self.xinput_slot = led_number

    def stop(self):
        self.stopping.set()

    def _neutral(self):
        if self.pad is not None:
            self.pad.reset()
            self.pad.update()
        self._last_report = None
        self.state = None
        self.buttons = 0
        self.output = None

    def _apply(self, s):
        b0, b1, b2 = s["b0"], s["b1"], s["b2"]
        btn = HAT.get(b0 & 0x0F, 0)
        if b0 & 0x10: btn |= X
        if b0 & 0x20: btn |= A
        if b0 & 0x40: btn |= B
        if b0 & 0x80: btn |= Y
        if b1 & 0x01: btn |= LB
        if b1 & 0x02: btn |= RB
        if b1 & 0x10: btn |= BACK
        if b1 & 0x20: btn |= START
        if b1 & 0x40: btn |= LS
        if b1 & 0x80: btn |= RS
        if b2 & 0x01: btn |= GUIDE
        if b2 & 0x02 and self.cfg["touchpad_as_back"]: btn |= BACK
        mute = bool(b2 & MUTE_BUTTON)
        if mute and not self._mute_held:
            self._switch_gun()
        self._mute_held = mute
        cal = self.cfg["calibration"] or DEFAULT_CAL
        sdz = self.cfg["stick_deadzone"] / 100
        tdz = self.cfg["trigger_deadzone"] / 100
        report = (btn, map_trigger(s["l2"], cal["l2"], tdz), map_trigger(s["r2"], cal["r2"], tdz),
                  *map_stick(s["lx"], s["ly"], cal, "l", sdz), *map_stick(s["rx"], s["ry"], cal, "r", sdz))
        self.output = report
        self.state = s
        self.buttons = btn
        self.rguard.touch(report)
        if report != self._last_report:
            r = self.pad.report
            (r.wButtons, r.bLeftTrigger, r.bRightTrigger,
             r.sThumbLX, r.sThumbLY, r.sThumbRX, r.sThumbRY) = report
            self.pad.update()
            self._last_report = report

    def rumble_out(self):
        return self.desired_output()[:2]

    def battery_info(self):
        s = self.state
        if not s or s["battery"] is None:
            return None, False, True
        return s["battery"], s["charging"] in (1, 2), True

    def desired_output(self):
        pct, charging, _ = self.battery_info()
        self.saving = saver_on(self.cfg, pct, charging)
        self.game_rumble = self.rguard.filter(self.rumble)
        if time.time() < self.test_until:
            large, small = 200, 200
        else:
            k = self.cfg["rumble_strength"] / 100 * (SAVER_RUMBLE if self.saving else 1)
            large, small = (min(255, int(max(v, r) * k)) for v, r in zip(self.game_rumble, self.recoil))
        leds = 0
        if self.cfg["player_leds"] and not self.saving and self.xinput_slot is not None and 0 <= self.xinput_slot < 4:
            leds = PLAYER_LEDS[self.xinput_slot]
        b = (SAVER_LIGHTBAR if self.saving else self.cfg["lightbar_brightness"]) / 100
        rgb = tuple(int(c * b) for c in self.cfg["lightbar"])
        return large, small, rgb, leds, self.trigger_effects()

    def trigger_effects(self):
        """(L2, R2) effect blocks for the current game's profile; a held trigger kicks while the game rumbles."""
        p = self.triggers
        if not p or not self.cfg["adaptive_triggers"] or p.get("native"):
            return trigger_off(), trigger_off()
        level = max(self.game_rumble)
        s = self.state
        out = []
        for t in ("l2", "r2"):
            if self.saving:   # battery saver: no resistance motors, only the short gun kicks
                kick = p[t] in GUNS and self.gun_fx[t] == GUNS[p[t]]["kick_fx"]
                out.append(self.gun_fx[t] if kick else trigger_off())
            elif p[t] in GUNS:
                out.append(self.gun_fx[t] or GUNS[p[t]]["rest"])
            elif p["kick"] and level > 30 and s and s[t] > 40:
                out.append(KICK[level * 7 // 255])
            else:
                out.append(TRIGGER_STYLES.get(p[t], TRIGGER_STYLES["off"])[1])
        return tuple(out)

    def _guns_active(self):
        p = self.triggers
        return p if p and self.cfg["adaptive_triggers"] and not p.get("native") else None

    def _fire_guns(self):
        p = self._guns_active()
        guns = {t: GUNS.get(p[t]) for t in ("l2", "r2")} if p else {}
        phase, self.recoil = step_guns(guns, self.state, self.guns, p.get("recoil", True) if p else False)
        for t in ("l2", "r2"):
            # kicking: the trigger buzzes; after the break it travels free; before it, the gun's rest effect
            self.gun_fx[t] = (guns[t]["kick_fx"] if phase[t] == "kick" else
                              trigger_off() if phase[t] == "free" else None)

    def _switch_gun(self):
        """Mic button: R2 moves on to the next gun preset (weapon swapped in game)."""
        p = self._guns_active()
        if not p or not self.cfg["mute_switches_gun"]:
            return
        keys = list(GUNS)
        p["r2"] = keys[(keys.index(p["r2"]) + 1) % len(keys)] if p["r2"] in GUNS else keys[0]
        self.gun_switched += 1
        threading.Thread(target=save_config, args=(self.cfg,), daemon=True).start()

    def run(self):
        realtime_thread()
        while not self.stopping.is_set():
            if self.paused.is_set():
                self.status = "Paused - controller released for firmware update"
                self.stopping.wait(0.5)
                continue
            if self.native_game:
                self._unplug()
                self.status = f"{self.native_game} is using the PS5 controller directly (its own adaptive triggers)"
                self.stopping.wait(0.5)
                continue
            self._plug()
            info = find_dualsense()
            if not info:
                self.status = "Waiting for DualSense (USB or Bluetooth)..."
                self.stopping.wait(1.0)
                continue
            try:
                ds = DualSense(info)
            except OSError as e:
                self.status = f"Could not open controller: {e}"
                self.stopping.wait(2.0)
                continue
            self.status = f"Connected: {ds.name} over {'Bluetooth' if ds.bt else 'USB'}"
            self.firmware = f"Firmware {ds.update_version:04X}" + (f" ({ds.build_date})" if ds.build_date else "")
            last_out = None
            got_input = time.time()
            try:
                while not self.stopping.is_set() and not self.paused.is_set() and not self.native_game:
                    s = ds.read(8)
                    if s:
                        self._apply(s)
                        got_input = time.time()
                    elif time.time() - got_input > 3 and not find_dualsense():
                        break  # Bluetooth drop without a read error
                    self._fire_guns()
                    out = self.desired_output()
                    if out != last_out:
                        ds.send(*out)
                        last_out = out
            except OSError:
                pass
            try:
                ds.send(0, 0, (0, 0, 0), 0, (trigger_off(), trigger_off()))
            except OSError:
                pass
            ds.close()
            self._neutral()
            if not self.stopping.is_set() and not self.paused.is_set() and not self.native_game:
                self.status = "Controller disconnected - waiting..."
                self.stopping.wait(1.0)


def is_controller_speaker(name):
    name = name.lower()
    return "dualsense" in name or "wireless controller" in name


class AudioHaptics(threading.Thread):
    """Captures what the real speakers play (WASAPI loopback), keeps the bass and plays it on the
    DualSense's haptic channels 3-4 only - its speaker channels 1-2 stay silent. Also stops Windows
    from using the controller's speaker as a default playback device."""

    def __init__(self, cfg):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.status = ""
        self.level = 0.0
        self.bass = 0.0   # incoming bass loudness, in gate-slider units (1.0 = 100%)
        self.suspend = threading.Event()   # set while a native PS5 game drives the controller
        self.stopping = threading.Event()

    def stop(self):
        self.stopping.set()

    def run(self):
        import warnings
        import numpy as np
        import soundcard as sc   # imported here: it initialises COM (multithreaded) on this thread
        warnings.simplefilter("ignore")
        n = 511
        t = np.arange(n) - (n - 1) / 2
        taps = np.sinc(2 * HAPTIC_CUTOFF / HAPTIC_RATE * t) * np.blackman(n)
        self.taps = (taps / taps.sum()).astype(np.float32)
        while not self.stopping.is_set():
            try:
                self._session(sc, np)
            except Exception as e:  # controller unplugged, output device changed mid-stream, ...
                self.status = f"Audio haptics: restarting ({str(e)[:60]})"
                self.level = 0.0
                self.stopping.wait(2.0)

    def _guard_defaults(self, sc, ds_id):
        """If Windows made the controller a default playback device (any role), switch that role back."""
        ids = audio_devices.default_output_ids()
        if ds_id not in ids.values():
            good = ids.get(1)
            if good and good != self.cfg["audio_output"]:
                self.cfg["audio_output"] = good
                save_config(self.cfg)
            return
        speakers = {s.id.lower() for s in sc.all_speakers() if not is_controller_speaker(s.name)}
        good = next((i for i in (ids.get(1), ids.get(0), self.cfg["audio_output"]) if i in speakers), None)
        good = good or next(iter(sorted(speakers)), None)
        if good:
            audio_devices.set_default_output(good, [r for r, i in ids.items() if i == ds_id])

    def _session(self, sc, np):
        ds = next((s for s in sc.all_speakers() if is_controller_speaker(s.name)), None)
        if not ds:
            self.status = "Audio haptics: needs the USB cable (Bluetooth carries no controller audio)"
            self.level = 0.0
            self.stopping.wait(2.0)
            return
        ds_id = ds.id.lower()
        self._guard_defaults(sc, ds_id)
        if self.suspend.is_set():
            self.status = "Audio haptics: paused - the game drives the controller itself"
            self.level = self.bass = 0.0
            self.stopping.wait(1.0)
            return
        if not self.cfg["audio_haptics"]:
            self.status = "Audio haptics: off"
            self.level = 0.0
            self.stopping.wait(1.0)
            return
        src = sc.default_speaker()
        if src.id.lower() == ds_id:
            self.stopping.wait(1.0)
            return
        loop = sc.get_microphone(src.id, include_loopback=True)
        self.status = f"Audio haptics: feeling the sound from {src.name}"
        hist = len(self.taps) - 1
        tail = np.zeros((hist, 2), np.float32)
        g_prev = 0.0
        check_at = time.time() + 2
        with loop.recorder(HAPTIC_RATE, channels=2, blocksize=4800) as rec, \
                ds.player(HAPTIC_RATE, channels=4, blocksize=4800) as out:
            while not self.stopping.is_set() and self.cfg["audio_haptics"] and not self.suspend.is_set():
                if time.time() > check_at:
                    check_at = time.time() + 2
                    self._guard_defaults(sc, ds_id)
                    if sc.default_speaker().id != src.id:
                        return  # a different output was picked: follow it
                x = rec.record(None)
                if x.ndim != 2 or not len(x):
                    continue
                buf = np.concatenate((tail, x[:, :2]))
                tail = buf[-hist:]
                y = np.stack([np.convolve(buf[:, c], self.taps, "valid") for c in (0, 1)], axis=1)
                # Noise gate: steady background bass (music, engines, ambience) would buzz non-stop,
                # so only hits louder than the gate get through - soft knee up to 2x the threshold.
                rms = float(np.sqrt(np.mean(y * y)))
                self.bass = rms / HAPTIC_GATE_FULL
                thr = self.cfg["audio_haptics_gate"] / 100 * HAPTIC_GATE_FULL
                target = min(1.0, max(0.0, (rms - thr) / thr)) if thr > 0 else 1.0
                g = max(target, g_prev * float(np.exp(-len(y) / HAPTIC_RATE / HAPTIC_RELEASE)))
                ramp = np.linspace(g_prev, g, len(y), dtype=np.float32)[:, None]  # no clicks
                g_prev = g
                y *= ramp * (self.cfg["audio_haptics_strength"] / 100 * 3)
                np.clip(y, -1, 1, out=y)
                self.level = float(np.abs(y).max())
                if out.currentpadding > HAPTIC_RATE * HAPTIC_MAX_LAG:
                    continue  # the two sound cards' clocks drift; drop a block rather than lag behind
                frame = np.zeros((len(y), 4), np.float32)
                frame[:, 2:] = y   # channels 3-4 = left/right haptic actuators; 1-2 (speaker) stay silent
                out.play(frame)
        self.level = self.bass = 0.0


class XboxRumbleHID(threading.Thread):
    """Sends rumble, trigger motors included, straight to a Bluetooth Xbox controller's HID device.
    Writes wait for the radio, so they run here and never stall the input loop."""

    def __init__(self, path):
        super().__init__(daemon=True)
        self.dev = hid.device()
        self.dev.open_path(path)
        self.want = (0, 0, 0, 0)   # LT, RT, strong, weak; 0-100
        self.wake = threading.Event()
        self.closing = False

    @staticmethod
    def open():
        for d in hid.enumerate(0x045E, 0):
            if d["usage_page"] == 0x01 and d["usage"] == 0x05 and b"{00001812-" in d["path"]:   # BLE HID gamepad
                try:
                    w = XboxRumbleHID(d["path"])
                except OSError:
                    continue
                w.start()
                return w
        return None

    def set(self, lt, rt, strong, weak):
        want = (lt, rt, strong, weak)
        if want != self.want:
            self.want = want
            self.wake.set()

    def close(self):
        self.closing = True
        self.want = (0, 0, 0, 0)
        self.wake.set()

    def run(self):
        sent = None
        while True:
            # A set() wakes this at once; the timeout only paces the keep-alive re-send.
            self.wake.wait(XBOX_RUMBLE_REFRESH)
            self.wake.clear()
            want = self.want
            if want != sent or any(want):
                try:
                    if self.dev.write(bytes([XBOX_RUMBLE_REPORT, XBOX_RUMBLE_ALL, *want, 255, 0, 0])) < 0:
                        break
                except (OSError, ValueError):
                    break
                sent = want
            if self.closing:
                break
        self.dev.close()


class XboxBridge(threading.Thread):
    """Reads the real Xbox controller (hidden from games by HidHide), applies its calibration and
    deadzones and re-presents it as a virtual Xbox 360 pad; game rumble goes back to the real one."""

    def __init__(self, cfg):
        super().__init__(daemon=True)
        self.cfg = cfg                  # the config's "xbox" section
        self.status = "Starting..."
        self.state = None
        self.buttons = 0
        self.output = None
        self.rumble = (0, 0)
        self.xinput_slot = None         # slot of our virtual pad, as games see it
        self.real_slot = None
        self.battery = None
        self.battery_exact = False
        self.wired = False
        self.container = ""             # BTHLE device node (battery %), filled in by ControllerGuard
        self.test_until = 0.0
        self.pad = None
        self._last_report = None
        self.hid_rumble = None          # XboxRumbleHID while a Bluetooth pad is connected
        self.trigger_motors = (0, 0)    # LT/RT motor 0-100, for the UI
        self.body_out = (0, 0)          # body motors 0-255 as sent, for the UI
        self.triggers = None            # current game's trigger profile, set by GameWatcher
        self.guns = new_gun_states()
        self.power = {t: {"armed": True, "start": None} for t in ("l2", "r2")}
        self.root_cfg = {}              # whole config (battery saver settings), set by main()
        self.rguard = RumbleGuard()
        self.saving = False
        self.aim_until = {"l2": 0.0, "r2": 0.0}   # "aim" style: click running until; 0 = armed, -1 = clicked
        self.stopping = threading.Event()

    def _on_rumble(self, client, target, large_motor, small_motor, led_number, user_data):
        self.rumble = (large_motor, small_motor)
        self.xinput_slot = led_number

    def stop(self):
        self.stopping.set()

    @property
    def virtual(self):
        return self.cfg["mode"] == "virtual"

    def games_slot(self):
        """XInput player slot games see: our virtual pad's, or the real pad's in as-is mode."""
        return self.xinput_slot if self.virtual else self.real_slot

    def rumble_out(self):
        return self.body_out

    def _effects(self):
        """What the motors should do now: (body large/small 0-255, LT/RT trigger motors 0-100).
        Per trigger, the game's profile picks: buzz with the game's rumble while held, rattle, a gun, or off."""
        test = time.time() < self.test_until
        out, p = self.output, self.triggers or {}
        self.saving = saving = saver_on(self.root_cfg, self.battery, self.wired)
        rumble = self.rguard.filter(self.rumble)
        on = self.cfg["trigger_rumble"] and (self.virtual or test) and out is not None
        styles = {t: p.get("xbox_" + t, "rumble") if on else "off" for t in ("l2", "r2")}
        guns = {t: GUNS.get(styles[t]) for t in ("l2", "r2")}
        phase, recoil = step_guns(guns, self.state, self.guns, p.get("recoil", True))
        now = time.perf_counter()
        power = {}
        for t in ("l2", "r2"):
            pw, st = POWERS.get(styles[t]), self.power[t]
            if pw is None or not self.state:
                st.update(armed=True, start=None)
                continue
            raw = self.state[t]
            if raw < pw["rearm"]:
                st["armed"] = True
            if st["armed"] and raw >= pw["fire_at"]:
                st.update(armed=False, start=now, loud=None)
            e = pw["env"](now - st["start"], max(rumble), st) if st["start"] is not None else None
            if e is None:
                st["start"] = None
                continue
            power[t] = e[0]
            if p.get("recoil", True):
                recoil = (max(recoil[0], int(e[1])), max(recoil[1], int(e[2])))
        if test:
            body = (200, 200)
        elif self.virtual:
            k = self.cfg["rumble_strength"] / 100 * (SAVER_RUMBLE if saving else 1)
            body = tuple(min(255, int(max(v, r) * k)) for v, r in zip(rumble, recoil))
        else:
            body = (0, 0)   # as-is mode: games rumble it themselves
        level = 200 if test else max(rumble)
        k = self.cfg["trigger_rumble_strength"] / 100 * (SAVER_TRIGGERS if saving else 1)
        trig = []
        for i, t in ((1, "l2"), (2, "r2")):
            held = on and out[i] > XBOX_TRIGGER_HELD
            aim = 0
            if styles[t] == "aim" and self.state:
                raw, until = self.state[t], self.aim_until[t]
                if raw < XBOX_AIM[1]:
                    self.aim_until[t] = until = 0.0               # let go: the next aim clicks again
                elif until == 0.0 and raw >= XBOX_AIM[0]:
                    self.aim_until[t] = until = now + XBOX_AIM[3]
                aim = XBOX_AIM[2] if until > 0 and now < until else 0
                if until > 0 and now >= until:
                    self.aim_until[t] = -1.0                      # clicked; wait for release
            if t in power:
                v = power[t]
            elif aim:
                v = aim
            elif phase[t] == "kick":
                v = guns[t]["kick"][0] * 100 / 8
            elif held and styles[t] == "rattle" and not saving:
                v = XBOX_RATTLE
            elif held and ((styles[t] in ("rumble", "aim") and not saving) or test):
                v = level * 100 / 255
            else:
                v = 0
            trig.append(min(100, int(v * k)))
        return body, tuple(trig)

    def _send_rumble(self, slot, body, trig):
        """Body motors plus trigger motors over Bluetooth HID when we can, else XInput (no trigger motors)."""
        w = self.hid_rumble
        if w is not None and not w.is_alive():
            w = self.hid_rumble = None
        if w is None:
            self.trigger_motors = (0, 0)
            xinput.set_rumble(slot, *body)
        else:
            self.trigger_motors = trig
            w.set(*trig, body[0] * 100 // 255, body[1] * 100 // 255)

    def _effects_loop(self, slot, done):
        """Rumble, trigger motors, simulated guns and battery, on their own thread so the input
        loop only ever reads the controller and forwards it to the virtual pad.
        Normal priority on purpose: it must never take CPU time from the input thread."""
        last, idle, next_battery = None, ((0, 0), (0, 0)), 0.0
        while not done.is_set():
            body, trig = self._effects()
            self.body_out = body
            cur = (body, trig)
            # As-is mode: only our test rumble (and its stop) goes out.
            if cur != last and (self.virtual or cur != idle or last not in (None, idle)):
                try:
                    self._send_rumble(slot, body, trig)
                except OSError:
                    pass
            last = cur
            if time.time() > next_battery:
                self._update_battery(slot)
                # until the first reading arrives (the BLE battery node is found a moment later), retry soon
                next_battery = time.time() + (15 if self.battery is not None or self.wired else 2)
            time.sleep(0.002)   # 2 ms: fine enough for a 900 rpm gun's 28 ms kicks
        try:
            xinput.set_rumble(slot, 0, 0)
        except OSError:
            pass
        self._close_hid()
        self.body_out = (0, 0)

    def _close_hid(self):
        if self.hid_rumble is not None:
            self.hid_rumble.close()
            self.hid_rumble = None
        self.trigger_motors = (0, 0)

    def battery_info(self):
        """(percent or None, charging/powered, exact percent?)"""
        return self.battery, self.wired, self.battery_exact

    def _plug(self):
        if self.pad is None:
            self.pad = vg.VX360Gamepad()
            self.pad.register_notification(callback_function=self._on_rumble)

    def _unplug(self):
        # Only have a virtual pad while the real one is connected, so games don't see a phantom controller.
        if self.pad is not None:
            self.pad.unregister_notification()
            self.pad = None
        self.state, self.output, self.buttons = None, None, 0
        self.xinput_slot, self.real_slot, self.battery = None, None, None
        self.rumble, self._last_report = (0, 0), None

    def _update_battery(self, slot):
        btype, approx = xinput.battery(slot)
        self.wired = btype == xinput.BATTERY_TYPE_WIRED
        exact = hidhide.ble_battery(self.container) if self.container else None
        if self.wired:
            self.battery, self.battery_exact = None, False
        elif exact is not None:
            self.battery, self.battery_exact = exact, True
        else:
            self.battery = approx if btype != xinput.BATTERY_TYPE_DISCONNECTED else None
            self.battery_exact = False

    @staticmethod
    def _read(g):
        """XInput -> the DualSense's raw layout (0-255, y down) so calibration code is shared."""
        return {"lx": (g.sThumbLX + 32768) / 257, "ly": (32767 - g.sThumbLY) / 257,
                "rx": (g.sThumbRX + 32768) / 257, "ry": (32767 - g.sThumbRY) / 257,
                "l2": g.bLeftTrigger, "r2": g.bRightTrigger, "b": g.wButtons}

    def _apply(self, s):
        cal = self.cfg["calibration"] or DEFAULT_CAL
        sdz = self.cfg["stick_deadzone"] / 100
        tdz = self.cfg["trigger_deadzone"] / 100
        report = (s["b"], map_trigger(s["l2"], cal["l2"], tdz), map_trigger(s["r2"], cal["r2"], tdz),
                  *map_stick(s["lx"], s["ly"], cal, "l", sdz), *map_stick(s["rx"], s["ry"], cal, "r", sdz))
        self.state, self.output, self.buttons = s, report, s["b"]
        self.rguard.touch(report)
        if self.pad is not None and report != self._last_report:
            r = self.pad.report
            (r.wButtons, r.bLeftTrigger, r.bRightTrigger,
             r.sThumbLX, r.sThumbLY, r.sThumbRX, r.sThumbRY) = report
            self.pad.update()
            self._last_report = report

    def run(self):
        realtime_thread()
        while not self.stopping.is_set():
            found = xinput.find_real_pad()
            if not found:
                self._unplug()
                self.status = "Waiting for an Xbox controller (Bluetooth, USB or adapter)..."
                self.stopping.wait(1.0)
                continue
            slot, _ = found
            self.real_slot = slot
            self.hid_rumble = XboxRumbleHID.open()
            done = threading.Event()
            fx = threading.Thread(target=self._effects_loop, args=(slot, done), daemon=True)
            fx.start()
            virtual, last_packet = None, None
            while not self.stopping.is_set():
                if self.virtual != virtual:
                    virtual, last_packet = self.virtual, None
                    if virtual:
                        self._plug()
                        self.status = "Connected: Xbox controller - games get the corrected virtual copy"
                    elif self.pad is not None:
                        self.pad.unregister_notification()
                        self.pad, self.xinput_slot, self.rumble, self._last_report = None, None, (0, 0), None
                    if not virtual:
                        self.status = "Connected: Xbox controller - games use it as is"
                # Input path only: read -> calibrate -> virtual pad. Rumble/guns/battery live in _effects_loop.
                got = xinput.get_state(slot)
                if got is None:
                    break
                packet, g = got
                if packet != last_packet:
                    self._apply(self._read(g))
                    last_packet = packet
                # XInput has no "new input" event, so poll: ~0.7 ms period, ~3 us per check.
                # (time.sleep is high-resolution; Event.wait would round up to the 15.6 ms system tick.)
                time.sleep(0.0002)
            done.set()
            fx.join(1.0)
            self._unplug()


class ControllerGuard(threading.Thread):
    """Keeps the physical controllers hidden from games while the app runs (HidHide), so games only
    see the corrected virtual pads; shows them again on quit."""

    def __init__(self, bridge, xbox):
        super().__init__(daemon=True)
        self.bridges = {"VID_054C": bridge, "VID_045E": xbox}
        self.xbox = xbox
        self.status = ""
        self.pending = {}   # device path whose hiding just changed -> True if now hidden; needs one reconnect
        self.stopping = threading.Event()
        self.refresh = threading.Event()

    def stop(self):
        self.stopping.set()
        self.refresh.set()

    def run(self):
        if not hidhide.available():
            self.status = "HidHide not installed - games also see the real controllers"
            return
        while not self.stopping.is_set():
            self.refresh.clear()
            try:
                cfg = self.bridges["VID_054C"].cfg
                native = sorted({g["path"] for g in cfg["games"].values() if g["native"] and cfg["adaptive_triggers"]})
                forget = [p for p in cfg["hidhide_games"] if p not in native]
                devices, changed = hidhide.protect(hide_xbox=self.xbox.virtual, games=native, forget=forget)
                if cfg["hidhide_games"] != native:
                    cfg["hidhide_games"] = native
                    save_config(cfg)
                self.pending.update({p: not hidhide.is_xbox(p) or self.xbox.virtual for p in changed})
                ble = [d["container"] for d in devices if d["container"].upper().startswith("BTHLE")
                       and hidhide.is_xbox(d["path"])]
                self.xbox.container = ble[0] if ble else ""
                n = sum(1 for d in devices if self.xbox.virtual or not hidhide.is_xbox(d["path"]))
                self.status = f"Real controllers hidden from games: {n}"
            except Exception as e:
                self.status = f"HidHide error: {str(e)[:60]}"
            # Every second: a controller that disconnected has picked up its new hidden state.
            for _ in range(30):
                if self.refresh.wait(1.0):
                    break
                for vid, b in self.bridges.items():
                    if b.state is None:
                        self.pending = {p: h for p, h in self.pending.items() if vid not in p.upper()}

    def reconnect_hint(self, vid):
        states = [h for p, h in self.pending.items() if vid in p.upper()]
        if not states:
            return ""
        return ("Turn this controller off and on (or re-plug it) once so games "
                + ("stop seeing it directly." if states[-1] else "see it directly again."))


class GameWatcher(threading.Thread):
    """Once a second: which listed game is running. A game with PS5 support gets the real DualSense
    (virtual pad unplugged, our output and audio haptics paused); any other gets its trigger profile."""

    def __init__(self, cfg, bridge, haptics, xbox):
        super().__init__(daemon=True)
        self.cfg, self.bridge, self.haptics, self.xbox = cfg, bridge, haptics, xbox
        self.preview = None   # game key being set up in the Adaptive triggers tab ("" = other games)
        self.running = None   # key of the listed game that's running
        self.wake = threading.Event()
        self.stopping = threading.Event()

    def stop(self):
        self.stopping.set()
        self.wake.set()

    def run(self):
        while not self.stopping.is_set():
            self.wake.clear()
            try:
                names = processes.running_names()
            except OSError:
                names = set()
            games = self.cfg["games"]
            self.running = next((k for k in list(games) if k in names), None)
            native = self.running if (self.cfg["adaptive_triggers"] and self.running
                                      and games[self.running]["native"]) else None
            key = self.preview if self.preview is not None and not native else self.running
            self.bridge.triggers = games.get(key, self.cfg["trigger_default"]) if key is not None else self.cfg["trigger_default"]
            # The Xbox pad isn't affected by "native" (that only hands the DualSense to the game).
            xkey = self.preview if self.preview is not None else self.running
            self.xbox.triggers = games.get(xkey, self.cfg["trigger_default"]) if xkey else self.cfg["trigger_default"]
            self.bridge.native_game = os.path.basename(games[native]["path"]) if native else None
            if native:
                self.haptics.suspend.set()
            else:
                self.haptics.suspend.clear()
            self.wake.wait(1.0)


class AddGameDialog(tk.Toplevel):
    """Pick a running game (or browse for its .exe) to give it its own trigger settings."""

    def __init__(self, app, on_pick):
        super().__init__(app.root)
        self.on_pick = on_pick
        self.title("Add game")
        self.transient(app.root)
        f = ttk.Frame(self, padding=10)
        f.grid()
        ttk.Label(f, text="Start the game, then pick it here (or browse for its .exe):").grid(row=0, column=0, sticky="w")
        pal = app.pal
        self.lst = tk.Listbox(f, width=70, height=12, bg=pal["panel"], fg=pal["text"], selectbackground=pal["on"],
                              selectforeground=pal["on_text"], highlightthickness=1, highlightcolor=pal["line"],
                              highlightbackground=pal["line"], relief="flat", activestyle="none")
        self.lst.grid(row=1, column=0, pady=6)
        self.lst.bind("<Double-Button-1>", lambda _: self._add())
        self.progs = processes.windowed_programs()
        for title, path in self.progs:
            self.lst.insert("end", f"{os.path.basename(path)}  -  {title}")
        bf = ttk.Frame(f)
        bf.grid(row=2, column=0, sticky="e")
        ttk.Button(bf, text="Browse for .exe...", command=self._browse).pack(side="left")
        ttk.Button(bf, text="Cancel", command=self.destroy).pack(side="left", padx=6)
        ttk.Button(bf, text="Add", style="Accent.TButton", command=self._add).pack(side="left")
        style_window(self, app.dark)

    def _add(self):
        sel = self.lst.curselection()
        if sel:
            self.on_pick(self.progs[sel[0]][1])
            self.destroy()

    def _browse(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(parent=self, title="Game .exe", filetypes=[("Programs", "*.exe")])
        if path:
            self.on_pick(os.path.normpath(path))
            self.destroy()


class CalibrationWizard(tk.Toplevel):
    """Two steps on raw readings: centre (hands off), then range (roll sticks, press triggers)."""
    SIZE = 140
    AXES = ("lx", "ly", "rx", "ry")

    def __init__(self, app, source, save, triggers=("L2", "R2")):
        super().__init__(app.root)
        self.bridge, self.save, self.tnames = source, save, triggers
        self.title("Calibrate sticks & triggers")
        self.resizable(False, False)
        self.transient(app.root)
        f = ttk.Frame(self, padding=12)
        f.grid()
        self.text = ttk.Label(f, text="", wraplength=310, justify="left")
        self.text.grid(row=0, column=0, sticky="w")
        self.canvas = tk.Canvas(f, width=2 * self.SIZE + 30, height=self.SIZE + 30, bg="#1e1e1e", highlightthickness=0)
        self.canvas.grid(row=1, column=0, pady=8)
        self.trig = ttk.Label(f, text="", font=("Consolas", 9))
        self.trig.grid(row=2, column=0, sticky="w")
        bf = ttk.Frame(f)
        bf.grid(row=3, column=0, sticky="e", pady=(8, 0))
        ttk.Button(bf, text="Reset to default", command=self._reset).pack(side="left")
        ttk.Button(bf, text="Cancel", command=self.destroy).pack(side="left", padx=6)
        self.next_btn = ttk.Button(bf, text="Next", style="Accent.TButton", command=self._next)
        self.next_btn.pack(side="left")
        style_window(self, app.dark)
        self.alive = True
        self.step = "centre"
        self.samples = []
        self.text.config(text="Step 1 of 2 - Centre\n\nPut the controller down and let go of both sticks "
                              "and both triggers. Click Next, then don't touch it for a second.")
        self._loop()

    def destroy(self):
        self.alive = False
        super().destroy()

    def _reset(self):
        self.save(None)
        self.destroy()

    def _next(self):
        if self.bridge.state is None:
            messagebox.showwarning("Calibrate", "Connect the controller first.", parent=self)
        elif self.step == "centre":
            self.step, self.samples = "measuring", []
            self.next_btn.state(["disabled"])
            self.text.config(text="Measuring centre... hands off.")
            self.after(1000, self._centre_done)
        elif self.step == "range":
            self._finish()

    def _centre_done(self):
        if not self.alive:
            return
        self.next_btn.state(["!disabled"])
        spread = max(max(s[a] for s in self.samples) - min(s[a] for s in self.samples) for a in self.AXES) if self.samples else 99
        if spread > 8:
            self.step = "centre"
            self.text.config(text="The sticks moved while measuring. Let go of the controller completely and click Next again.")
            return
        self.centre = {a: sum(s[a] for s in self.samples) / len(self.samples) for a in self.AXES}
        self.lo = {a: self.centre[a] for a in self.AXES}
        self.hi = dict(self.lo)
        self.trig_max = {"l2": 0, "r2": 0}
        self.step = "range"
        self.next_btn.config(text="Finish")
        self.text.config(text="Step 2 of 2 - Range\n\nSlowly roll each stick around its outer edge 2-3 times, "
                              f"then press {self.tnames[0]} and {self.tnames[1]} all the way down. Click Finish.")

    def _finish(self):
        names = {"lx": "left stick", "ly": "left stick", "rx": "right stick", "ry": "right stick"}
        for a in self.AXES:
            if self.hi[a] - self.centre[a] < 40 or self.centre[a] - self.lo[a] < 40:
                messagebox.showwarning("Calibrate", f"The {names[a]} didn't reach its edge in every direction. "
                                                    "Keep rolling it around the rim, then click Finish.", parent=self)
                return
        for t, name in zip(("l2", "r2"), self.tnames):
            if self.trig_max[t] < 100:
                messagebox.showwarning("Calibrate", f"Press {name} all the way down, then click Finish.", parent=self)
                return
        # Pull the edges in slightly so full deflection reliably reaches 100%.
        cal = {a: [self.lo[a] + 2, self.centre[a], self.hi[a] - 2] for a in self.AXES}
        cal.update({t: self.trig_max[t] - 3 for t in ("l2", "r2")})
        self.save(cal)
        messagebox.showinfo("Calibrate", "Calibration saved.", parent=self)
        self.destroy()

    def _loop(self):
        if not self.alive:
            return
        s = self.bridge.state
        if s:
            if self.step == "measuring":
                self.samples.append(s)
            elif self.step == "range":
                for a in self.AXES:
                    self.lo[a] = min(self.lo[a], s[a])
                    self.hi[a] = max(self.hi[a], s[a])
                for t in self.trig_max:
                    self.trig_max[t] = max(self.trig_max[t], s[t])
            self._draw(s)
        self.after(15, self._loop)

    def _draw(self, s):
        c, n = self.canvas, self.SIZE
        c.delete("all")
        for i, side in enumerate("lr"):
            x0, y0 = 10 + i * (n + 10), 20
            px = lambda v: x0 + v / 255 * n
            py = lambda v: y0 + v / 255 * n
            c.create_text(x0 + n / 2, 10, text="Left stick" if side == "l" else "Right stick", fill="#ccc")
            c.create_rectangle(x0, y0, x0 + n, y0 + n, outline="#555")
            c.create_oval(x0, y0, x0 + n, y0 + n, outline="#444")
            if self.step == "range":
                c.create_rectangle(px(self.lo[side + "x"]), py(self.lo[side + "y"]),
                                   px(self.hi[side + "x"]), py(self.hi[side + "y"]), outline="#2e9e2e")
            x, y = px(s[side + "x"]), py(s[side + "y"])
            c.create_oval(x - 5, y - 5, x + 5, y + 5, fill="#3a8dde", outline="")
        l, r = self.tnames
        if self.step == "range":
            self.trig.config(text=f"{l} {s['l2']:3d} (max {self.trig_max['l2']:3d})    "
                                  f"{r} {s['r2']:3d} (max {self.trig_max['r2']:3d})")
        else:
            self.trig.config(text=f"{l} {s['l2']:3d}    {r} {s['r2']:3d}")


def windows_dark():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def theme_is_dark(cfg):
    return windows_dark() if cfg.get("theme", "system") == "system" else cfg["theme"] == "dark"


def style_window(win, dark):
    """Dark or light Windows title bar to match the theme."""
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        v = ctypes.c_int(1 if dark else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(v), 4)   # DWMWA_USE_IMMERSIVE_DARK_MODE
    except Exception:
        pass


PALETTES = {
    True: {"bg": "#1c1c1c", "panel": "#2b2b2b", "line": "#454545", "text": "#e6e6e6", "dim": "#9a9a9a",
           "accent": "#57c8ff", "on": "#57c8ff", "on_text": "#000000", "ok": "#6ccb5f", "warn": "#fcb251",
           "bad": "#ff6b6b"},
    False: {"bg": "#fafafa", "panel": "#ffffff", "line": "#d0d0d0", "text": "#1a1a1a", "dim": "#6b6b6b",
            "accent": "#005fb8", "on": "#005fb8", "on_text": "#ffffff", "ok": "#0f7b0f", "warn": "#9d5d00",
            "bad": "#c42b1c"},
}


class Slider:
    """Label + slider + live value; every change is passed to on_change (which saves)."""

    def __init__(self, parent, row, text, value, top, on_change, bottom=0, unit="%"):
        ttk.Label(parent, text=text).grid(row=row, column=0, sticky="w", pady=3)
        self.var = tk.IntVar(value=value)
        self.val = ttk.Label(parent, text=f"{value}{unit}", width=5, anchor="e")

        def changed(_):
            v = int(self.var.get())
            self.val.config(text=f"{v}{unit}")
            on_change(v)
        ttk.Scale(parent, from_=bottom, to=top, variable=self.var, length=190,
                  command=changed).grid(row=row, column=1, sticky="ew", padx=(10, 6))
        self.val.grid(row=row, column=2, sticky="e")


def meter(parent, row, text, top=100):
    ttk.Label(parent, text=text).grid(row=row, column=0, sticky="w", pady=2)
    bar = ttk.Progressbar(parent, maximum=top, length=190)
    bar.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(10, 0), pady=2)
    return bar


def card(parent, title, row, column=0, columnspan=1, **grid):
    f = ttk.LabelFrame(parent, text=f" {title} ", padding=(12, 8))
    f.grid(row=row, column=column, columnspan=columnspan, sticky="nsew", padx=6, pady=6, **grid)
    f.columnconfigure(1, weight=1)
    return f


def hint(parent, row, text="", **kw):
    lbl = ttk.Label(parent, text=text, foreground=parent.winfo_toplevel().palette["dim"], wraplength=360,
                    justify="left", **kw)
    lbl.grid(row=row, column=0, columnspan=3, sticky="w", pady=(4, 0))
    return lbl


class PadCanvas(tk.Canvas):
    """Live drawing of a controller: triggers, both sticks and every button."""
    W, H, R = 380, 196, 44
    CHIPS = [(A, "A"), (B, "B"), (X, "X"), (Y, "Y"), (LB, "LB"), (RB, "RB"), (LS, "LS"), (RS, "RS"),
             (UP, "↑"), (DOWN, "↓"), (LEFT, "←"), (RIGHT, "→"), (BACK, "View"), (START, "Menu"),
             (GUIDE, "Home")]

    def __init__(self, parent, pal, trigger_names):
        super().__init__(parent, width=self.W, height=self.H, bg=pal["panel"], highlightthickness=0)
        self.pal = pal
        W, R = self.W, self.R
        self.centres = [(122, 66), (W - 122, 66)]
        self.trig = []
        for i, (x0, name) in enumerate(((14, trigger_names[0]), (W - 34, trigger_names[1]))):
            self.create_text(x0 + 10, 10, text=name, fill=pal["dim"], font=("Segoe UI", 8, "bold"))
            self.create_rectangle(x0, 20, x0 + 20, 120, outline=pal["line"], width=1)
            self.trig.append((self.create_rectangle(x0 + 2, 118, x0 + 18, 118, fill=pal["accent"], outline=""), x0))
        self.dots = []
        for (cx, cy), name in zip(self.centres, ("Left stick", "Right stick")):
            self.create_oval(cx - R, cy - R, cx + R, cy + R, outline=pal["line"], width=2)
            self.create_line(cx - R, cy, cx + R, cy, fill=pal["line"], dash=(2, 3))
            self.create_line(cx, cy - R, cx, cy + R, fill=pal["line"], dash=(2, 3))
            self.create_text(cx, cy + R + 10, text=name, fill=pal["dim"], font=("Segoe UI", 8))
            self.dots.append(self.create_oval(cx - 7, cy - 7, cx + 7, cy + 7, fill=pal["accent"], outline=""))
        self.chips = []
        cw, gap = 40, 6
        for i, (bit, name) in enumerate(self.CHIPS):
            r, c = divmod(i, 8)
            n = 8 if r == 0 else len(self.CHIPS) - 8
            x = (W - (n * cw + (n - 1) * gap)) / 2 + c * (cw + gap)
            y = 136 + r * 28
            box = self.create_rectangle(x, y, x + cw, y + 22, outline=pal["line"], fill=pal["panel"])
            txt = self.create_text(x + cw / 2, y + 11, text=name, fill=pal["dim"], font=("Segoe UI", 8, "bold"))
            self.chips.append((bit, box, txt))
        self._last = None

    def show(self, output, buttons):
        state = (output, buttons)
        if state == self._last:   # nothing moved: skip the redraw
            return
        self._last = state
        pal, R = self.pal, self.R
        lt, rt, sticks = (output[1], output[2], output[3:]) if output else (0, 0, (0, 0, 0, 0))
        for (item, x0), v in zip(self.trig, (lt, rt)):
            self.coords(item, x0 + 2, 118 - v / 255 * 96, x0 + 18, 118)
        for i, ((cx, cy), dot) in enumerate(zip(self.centres, self.dots)):
            x = cx + sticks[2 * i] / 32767 * (R - 6)
            y = cy - sticks[2 * i + 1] / 32767 * (R - 6)
            self.coords(dot, x - 7, y - 7, x + 7, y + 7)
            self.itemconfig(dot, fill=pal["accent"] if output else pal["line"])
        for bit, box, txt in self.chips:
            on = bool(buttons & bit)
            self.itemconfig(box, fill=pal["on"] if on else pal["panel"], outline=pal["on"] if on else pal["line"])
            self.itemconfig(txt, fill=pal["on_text"] if on else pal["dim"])


class PadView:
    """One controller's tab: live view on the left, tuning on the right."""

    def __init__(self, app, f, bridge, sec, triggers):
        self.app, self.bridge, self.sec, self.triggers = app, bridge, sec, triggers
        pal = app.pal
        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)
        top = ttk.Frame(f)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=6, pady=(4, 2))
        self.dot = ttk.Label(top, text="●", font=("Segoe UI", 14))
        self.dot.pack(side="left")
        self.status = ttk.Label(top, text="", font=("Segoe UI", 11, "bold"))
        self.status.pack(side="left", padx=(6, 0))
        self.info = ttk.Label(f, text="", foreground=pal["dim"])
        self.info.grid(row=1, column=0, columnspan=2, sticky="w", padx=8)
        self.hint = ttk.Label(f, text="", foreground=pal["warn"], wraplength=740)
        self.hint.grid(row=2, column=0, columnspan=2, sticky="w", padx=8)

        live = card(f, "Live", 3, 0)
        self.canvas = PadCanvas(live, pal, triggers)
        self.canvas.grid(row=0, column=0, columnspan=3, pady=(2, 6))
        self.large = meter(live, 1, "Rumble (large)", 255)
        self.small = meter(live, 2, "Rumble (small)", 255)

        tune = card(f, "Feel", 3, 1)
        Slider(tune, 0, "Rumble strength", sec["rumble_strength"], 200, lambda v: self._set("rumble_strength", v))
        Slider(tune, 1, "Stick deadzone", sec["stick_deadzone"], 30, lambda v: self._set("stick_deadzone", v))
        Slider(tune, 2, "Trigger deadzone", sec["trigger_deadzone"], 30, lambda v: self._set("trigger_deadzone", v))
        ttk.Separator(tune).grid(row=3, column=0, columnspan=3, sticky="ew", pady=8)
        self.cal_lbl = ttk.Label(tune, text="")
        self.cal_lbl.grid(row=4, column=0, columnspan=3, sticky="w")
        bf = ttk.Frame(tune)
        bf.grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Button(bf, text="Calibrate…", command=self._calibrate).pack(side="left")
        ttk.Button(bf, text="Test rumble", style="Accent.TButton", command=self.test).pack(side="left", padx=8)
        self.tune = tune
        self.next_row = 4   # where the tab can add its own cards

    def _set(self, key, value):
        self.sec[key] = value
        save_config(self.app.cfg)

    def _calibrate(self):
        CalibrationWizard(self.app, self.bridge, lambda cal: self._set("calibration", cal), self.triggers)

    def test(self):
        self.bridge.test_until = time.time() + 0.8

    def battery_text(self):
        pct, charging, exact = self.bridge.battery_info()
        if pct is None:
            return "USB powered" if charging and self.bridge.state else ""
        return f"Battery {'' if exact else '~'}{pct}%" + (" (charging)" if charging else "")

    def tick(self, hint=""):
        b, pal = self.bridge, self.app.pal
        connected = b.state is not None
        self.dot.config(foreground=pal["ok"] if connected else pal["warn"])
        self.status.config(text=b.status)
        parts = [t for t in [self.battery_text()] if t]
        if getattr(b, "firmware", "") and connected:
            parts.append(b.firmware)
        slot = b.games_slot() if hasattr(b, "games_slot") else b.xinput_slot
        if slot is not None and connected:
            parts.append(f"Games see it as Xbox player {slot + 1}")
        self.info.config(text="   ·   ".join(parts))
        self.hint.config(text=hint)
        self.canvas.show(b.output, b.buttons)
        self.large["value"], self.small["value"] = b.rumble_out()[:2]
        self.cal_lbl.config(text="Sticks & triggers: " + ("calibrated ✓" if self.sec["calibration"] else "factory default"))


class App:
    def __init__(self, root, cfg, bridge, haptics, xbox, guard, watcher):
        self.root, self.cfg, self.bridge, self.haptics, self.xbox, self.guard = root, cfg, bridge, haptics, xbox, guard
        self.watcher = watcher
        self.dark = theme_is_dark(cfg)
        self.pal = root.palette = PALETTES[self.dark]
        if sv_ttk:
            sv_ttk.set_theme("dark" if self.dark else "light")
            try:
                root.tk.call("configure_colors")   # sv_ttk only applies its colours on a later event; do it now
            except tk.TclError:
                pass
        root.configure(bg=self.pal["bg"])
        root.title(APP_NAME)
        root.resizable(False, False)
        style_window(root, self.dark)
        pal = self.pal

        outer = ttk.Frame(root, padding=(14, 10, 14, 12))
        outer.grid(sticky="nsew")
        head = ttk.Frame(outer)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(head, text=APP_NAME, font=("Segoe UI Variable Display", 18, "bold")).pack(side="left")
        self.chips = {}
        for name in ("Xbox", "PS5"):
            fr = ttk.Frame(head)
            fr.pack(side="right", padx=(14, 0))
            dot = ttk.Label(fr, text="●", font=("Segoe UI", 12))
            dot.pack(side="left")
            lbl = ttk.Label(fr, text=name, foreground=pal["dim"])
            lbl.pack(side="left", padx=(4, 0))
            self.chips[name] = (dot, lbl)

        nb = ttk.Notebook(outer)
        nb.grid(row=1, column=0, sticky="nsew")
        nb.enable_traversal()   # Ctrl+Tab / Ctrl+Shift+Tab switch tabs
        self.nb = nb

        # ---- PS5 tab ----
        f = ttk.Frame(nb, padding=8)
        nb.add(f, text="  PS5 controller  ")
        self.ds_view = PadView(self, f, bridge, cfg, ("L2", "R2"))
        r = self.ds_view.next_row
        opts = card(f, "Controller", r, 0)
        self.touch = tk.BooleanVar(value=cfg["touchpad_as_back"])
        ttk.Checkbutton(opts, text="Touchpad click = View (Back)", variable=self.touch,
                        command=lambda: self._set("touchpad_as_back", self.touch.get())).grid(row=0, column=0, columnspan=3, sticky="w")
        self.leds = tk.BooleanVar(value=cfg["player_leds"])
        ttk.Checkbutton(opts, text="Player number on the controller LEDs", variable=self.leds,
                        command=lambda: self._set("player_leds", self.leds.get())).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        Slider(opts, 2, "Lightbar brightness", cfg["lightbar_brightness"], 100,
               lambda v: self._set("lightbar_brightness", v))
        bf = ttk.Frame(opts)
        bf.grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Button(bf, text="Lightbar colour…", command=self._colour).pack(side="left")
        self.fw_btn = ttk.Button(bf, text="Update firmware…", command=self._firmware)
        self.fw_btn.pack(side="left", padx=8)

        hap = card(f, "Audio haptics (USB)", r, 1)
        self.haptics_on = tk.BooleanVar(value=cfg["audio_haptics"])
        ttk.Checkbutton(hap, text="Feel the game's sound in the controller", style="Switch.TCheckbutton",
                        variable=self.haptics_on,
                        command=lambda: self._set("audio_haptics", self.haptics_on.get())).grid(row=0, column=0, columnspan=3, sticky="w")
        Slider(hap, 1, "Strength", cfg["audio_haptics_strength"], 300, lambda v: self._set("audio_haptics_strength", v))
        Slider(hap, 2, "Ignore quiet sound", cfg["audio_haptics_gate"], 100, lambda v: self._set("audio_haptics_gate", v))
        self.hbass = meter(hap, 3, "Game bass")
        self.hlevel = meter(hap, 4, "Haptics level")
        self.haptics_status = hint(hap, 5)

        # ---- Xbox tab ----
        xf = ttk.Frame(nb, padding=8)
        nb.add(xf, text="  Xbox controller  ")
        self.xb_view = PadView(self, xf, xbox, cfg["xbox"], ("LT", "RT"))
        r = self.xb_view.next_row
        mode = card(xf, "How games get this controller", r, 0)
        self.xmode = tk.StringVar(value=cfg["xbox"]["mode"])
        ttk.Radiobutton(mode, text="Virtual copy (recommended)", variable=self.xmode, value="virtual",
                        command=self._xbox_mode).grid(row=0, column=0, columnspan=3, sticky="w")
        hint(mode, 1, "Calibration, deadzones, rumble strength and trigger effects apply in games. "
                      "The real controller is hidden from games while this app runs.").grid(padx=(28, 0), pady=(0, 6))
        ttk.Radiobutton(mode, text="Use as is", variable=self.xmode, value="direct",
                        command=self._xbox_mode).grid(row=2, column=0, columnspan=3, sticky="w")
        hint(mode, 3, "Games read the real controller directly; this tab is for testing and battery only.").grid(padx=(28, 0))

        trig = card(xf, "Trigger motors (impulse triggers)", r, 1)
        self.xtrig = tk.BooleanVar(value=cfg["xbox"]["trigger_rumble"])
        ttk.Checkbutton(trig, text="Trigger effects in all games", style="Switch.TCheckbutton", variable=self.xtrig,
                        command=self._xtrig_toggle).grid(row=0, column=0, columnspan=3, sticky="w")
        Slider(trig, 1, "Trigger strength", cfg["xbox"]["trigger_rumble_strength"], 200,
               lambda v: self._xset("trigger_rumble_strength", v))
        self.xtbars = [meter(trig, 2, "LT motor"), meter(trig, 3, "RT motor")]
        self.xtstatus = hint(trig, 4)
        ttk.Button(trig, text="Per-game effects →", command=lambda: nb.select(self.tf)).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))

        # ---- Triggers tab (both controllers, per game) ----
        self.tf = tf = ttk.Frame(nb, padding=8)
        nb.add(tf, text="  Triggers  ")
        tf.columnconfigure(0, weight=1)
        tf.columnconfigure(1, weight=1)
        gsel = ttk.Frame(tf)
        gsel.grid(row=0, column=0, columnspan=2, sticky="ew", padx=6, pady=(4, 2))
        ttk.Label(gsel, text="Game", font=("Segoe UI", 11, "bold")).pack(side="left")
        self.tgame = ttk.Combobox(gsel, state="readonly", width=36)
        self.tgame.pack(side="left", padx=10)
        self.tgame.bind("<<ComboboxSelected>>", lambda _: self._at_load())
        ttk.Button(gsel, text="Add game…", command=lambda: AddGameDialog(self, self._at_add)).pack(side="left")
        self.tremove = ttk.Button(gsel, text="Remove", command=self._at_remove)
        self.tremove.pack(side="left", padx=(6, 0))
        self.tstatus = ttk.Label(tf, text="", font=("Segoe UI", 9, "bold"), foreground=pal["accent"], wraplength=740)
        self.tstatus.grid(row=1, column=0, columnspan=2, sticky="w", padx=8, pady=(2, 0))
        ttk.Label(tf, text="While this tab is open the selected game's effects are live on both controllers - "
                           "pull the triggers to try them.", foreground=pal["dim"]).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=8)

        ps = card(tf, "PS5 adaptive triggers", 3, 0)
        self.at_on = tk.BooleanVar(value=cfg["adaptive_triggers"])
        ttk.Checkbutton(ps, text="Adaptive triggers on", style="Switch.TCheckbutton", variable=self.at_on,
                        command=self._at_toggle).grid(row=0, column=0, columnspan=3, sticky="w")
        self.tnative = tk.BooleanVar()
        self.tnative_cb = ttk.Checkbutton(ps, text="Game supports the PS5 controller (its own effects)",
                                          variable=self.tnative, command=self._at_save)
        self.tnative_cb.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 4))
        self.tstyle = {}
        for i, t in enumerate(("l2", "r2")):
            ttk.Label(ps, text=t.upper()).grid(row=2 + i, column=0, sticky="w", pady=2)
            cb = ttk.Combobox(ps, state="readonly", width=40, values=STYLE_LABELS, height=len(STYLE_LABELS))
            cb.grid(row=2 + i, column=1, columnspan=2, sticky="ew", padx=(10, 0), pady=2)
            cb.bind("<<ComboboxSelected>>", lambda _: self._at_save())
            self.tstyle[t] = cb
        self.tkick = tk.BooleanVar()
        self.tkick_cb = ttk.Checkbutton(ps, text="Held trigger kicks when the game rumbles", variable=self.tkick,
                                        command=self._at_save)
        self.tkick_cb.grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.mute_gun = tk.BooleanVar(value=cfg["mute_switches_gun"])
        ttk.Checkbutton(ps, text="Mic button switches R2 to the next gun", variable=self.mute_gun,
                        command=lambda: self._set("mute_switches_gun", self.mute_gun.get())).grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 0))
        hint(ps, 6, "Tick \"supports the PS5 controller\" for Cyberpunk, Spider-Man, Returnal... and restart the game; "
                    "USB cable works best.")

        xb = card(tf, "Xbox trigger motors", 3, 1)
        self.xtrig2 = ttk.Checkbutton(xb, text="Trigger effects in all games", style="Switch.TCheckbutton",
                                      variable=self.xtrig, command=self._xtrig_toggle)
        self.xtrig2.grid(row=0, column=0, columnspan=3, sticky="w")
        self.xstyle = {}
        for i, (t, name) in enumerate((("l2", "LT"), ("r2", "RT"))):
            ttk.Label(xb, text=name).grid(row=1 + i, column=0, sticky="w", pady=2)
            cb = ttk.Combobox(xb, state="readonly", width=40, values=XBOX_STYLE_LABELS, height=len(XBOX_STYLE_LABELS))
            cb.grid(row=1 + i, column=1, columnspan=2, sticky="ew", padx=(10, 0), pady=2)
            cb.bind("<<ComboboxSelected>>", lambda _: self._at_save())
            self.xstyle[t] = cb
        self.xtbars2 = [meter(xb, 3, "LT motor"), meter(xb, 4, "RT motor")]
        hint(xb, 5, "Bluetooth only. Needs \"Virtual copy\" on the Xbox tab.")

        shared = card(tf, "Gun effects (both controllers)", 4, 0, columnspan=2)
        self.trecoil = tk.BooleanVar()
        self.trecoil_cb = ttk.Checkbutton(shared, text="Recoil pulse in the controller body with every shot",
                                          variable=self.trecoil, command=self._at_save)
        self.trecoil_cb.grid(row=0, column=0, columnspan=3, sticky="w")
        hint(shared, 1, "Guns are simulated: pulling past the break point fires. Pistol, shotgun, sniper: one shot per "
                        "pull. Assault rifle, SMG, machine gun: at their rate of fire while held. Burst: 3 rounds.").config(wraplength=740)
        self.gun_switched = bridge.gun_switched
        self._at_fill("")

        # ---- Settings tab ----
        sf = ttk.Frame(nb, padding=8)
        nb.add(sf, text="  Settings  ")
        sf.columnconfigure(0, weight=1)
        gen = card(sf, "General", 0, 0)
        self.startup = tk.BooleanVar(value=os.path.exists(STARTUP_CMD))
        ttk.Checkbutton(gen, text="Start with Windows (in the tray)", style="Switch.TCheckbutton", variable=self.startup,
                        command=self._toggle_startup).grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(gen, text="Theme").grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.theme = ttk.Combobox(gen, state="readonly", width=16, values=["Same as Windows", "Dark", "Light"])
        self.theme.current(["system", "dark", "light"].index(cfg.get("theme", "system")))
        self.theme.grid(row=2, column=1, sticky="w", padx=(10, 0), pady=(10, 0))
        self.theme.bind("<<ComboboxSelected>>", lambda _: self._theme())
        self.theme_hint = hint(gen, 3)
        bat = card(sf, "Battery", 1, 0)
        Slider(bat, 0, "Low battery warning below", cfg["battery_warning"], 50,
               lambda v: self._set("battery_warning", v), bottom=5)
        ttk.Label(bat, text="Battery saver").grid(row=1, column=0, sticky="w", pady=(10, 2))
        self.saver = tk.StringVar(value=cfg["battery_saver"])
        sv = ttk.Frame(bat)
        sv.grid(row=1, column=1, columnspan=2, sticky="w", padx=(10, 0), pady=(10, 2))
        for text, value in (("Automatic", "auto"), ("Always on", "on"), ("Off", "off")):
            ttk.Radiobutton(sv, text=text, variable=self.saver, value=value,
                            command=lambda: self._set("battery_saver", self.saver.get())).pack(side="left", padx=(0, 14))
        Slider(bat, 2, "Automatic below", cfg["battery_saver_below"], 60,
               lambda v: self._set("battery_saver_below", v), bottom=10)
        self.saver_status = ttk.Label(bat, text="", font=("Segoe UI", 9, "bold"), foreground=pal["accent"])
        self.saver_status.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        hint(bat, 4, "Battery saver (each controller on its own battery): rumble at half strength; PS5 lightbar at 10%, "
                     "player LEDs off, no trigger resistance (gun kicks stay); Xbox trigger motors at half and only for "
                     "aim clicks, guns and powers. Always on, even without the saver: rumble a game leaves running "
                     "(paused, menus, alt-tab) stops after 8 s if you don't touch the controller, and the PS5 player "
                     "LEDs run at low brightness.").config(wraplength=740)
        hh = card(sf, "Hiding the real controllers (HidHide)", 2, 0)
        self.guard_lbl = ttk.Label(hh, text="")
        self.guard_lbl.grid(row=0, column=0, columnspan=3, sticky="w")
        hint(hh, 1, "Games only see the corrected virtual controllers while this app runs; quitting shows the real ones again.").config(wraplength=740)

        # A mouse wheel over a dropdown would silently pick another value (and save it): ignore the wheel there.
        root.bind_class("TCombobox", "<MouseWheel>", lambda e: "break")

        foot = ttk.Frame(outer)
        foot.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(foot, text="Quit", command=self._quit).pack(side="right")
        ttk.Button(foot, text="Hide to tray", command=self.hide).pack(side="right", padx=8)
        ttk.Label(foot, text="Closing the window keeps it running in the tray.", foreground=pal["dim"]).pack(side="left")

        # Tray menu callbacks run on pystray's thread; hand them to Tk through this queue.
        self.commands = queue.Queue()
        self.tray = pystray.Icon("PS5toXbox", tray_image(), APP_NAME, menu=pystray.Menu(
            pystray.MenuItem("Open", lambda: self.commands.put(self.show), default=True),
            pystray.MenuItem("Test rumble", lambda: self.commands.put(self._test)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: self.commands.put(self._quit))))
        self.tray.run_detached()
        self.tray_tip = ""
        self.battery_alarm = {}   # controller name -> time of last warning

        root.protocol("WM_DELETE_WINDOW", self.hide)
        self._tick()

    def hide(self):
        self.root.withdraw()

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _set(self, key, value):
        self.cfg[key] = value
        save_config(self.cfg)

    # ---- Adaptive triggers tab ----
    def _at_keys(self):
        return [""] + sorted(self.cfg["games"])

    def _at_fill(self, select):
        keys = self._at_keys()
        self.tgame["values"] = ["Other games"] + [os.path.splitext(os.path.basename(self.cfg["games"][k]["path"]))[0]
                                                  for k in keys[1:]]
        self.tgame.current(keys.index(select) if select in keys else 0)
        self._at_load()

    def _at_key(self):
        return self._at_keys()[max(self.tgame.current(), 0)]

    def _at_profile(self):
        key = self._at_key()
        return self.cfg["games"][key] if key else self.cfg["trigger_default"]

    def _at_load(self):
        key, p = self._at_key(), self._at_profile()
        for t, cb in self.tstyle.items():
            cb.current(STYLE_KEYS.index(p[t]) if p[t] in STYLE_KEYS else 0)
        self.tkick.set(p["kick"])
        self.trecoil.set(p.get("recoil", True))
        for t, cb in self.xstyle.items():
            v = p.get("xbox_" + t, "rumble")
            cb.current(XBOX_STYLE_KEYS.index(v) if v in XBOX_STYLE_KEYS else 0)
        self.tnative.set(bool(key) and p["native"])
        self.tnative_cb.state(["!disabled"] if key else ["disabled"])
        self.tremove.state(["!disabled"] if key else ["disabled"])
        self._at_controls()
        self.watcher.wake.set()

    def _at_controls(self):
        """PS5 effect pickers don't apply to a game that drives the triggers itself."""
        state = ["disabled"] if self.tnative.get() else ["!disabled"]
        for w in (*self.tstyle.values(), self.tkick_cb):
            w.state(state)

    def _at_save(self):
        key, p = self._at_key(), self._at_profile()
        for t, cb in self.tstyle.items():
            p[t] = STYLE_KEYS[max(cb.current(), 0)]
        p["kick"] = self.tkick.get()
        p["recoil"] = self.trecoil.get()
        for t, cb in self.xstyle.items():
            p["xbox_" + t] = XBOX_STYLE_KEYS[max(cb.current(), 0)]
        if key and p.get("native") != self.tnative.get():
            p["native"] = self.tnative.get()
            self.guard.refresh.set()   # let the game see the real controller (or stop it) right away
        save_config(self.cfg)
        self._at_controls()
        self.watcher.wake.set()

    def _at_add(self, path):
        key = os.path.basename(path).lower()
        self.cfg["games"].setdefault(key, {"path": path, "native": False, "l2": "light", "r2": "gun_assault",
                                           "kick": True, "recoil": True, "xbox_l2": "rumble", "xbox_r2": "gun_assault"})
        save_config(self.cfg)
        self._at_fill(key)

    def _at_remove(self):
        key = self._at_key()
        if key and messagebox.askyesno("Adaptive triggers", f"Remove {key} from the list?", parent=self.root):
            del self.cfg["games"][key]
            save_config(self.cfg)
            self.guard.refresh.set()
            self._at_fill("")

    def _at_toggle(self):
        self._set("adaptive_triggers", self.at_on.get())
        self.guard.refresh.set()
        self.watcher.wake.set()

    def _at_tick(self, visible):
        preview = self._at_key() if visible and self.nb.select() == str(self.tf) else None
        if preview != self.watcher.preview:
            self.watcher.preview = preview
            self.watcher.wake.set()
        if not visible:
            return
        w, b = self.watcher, self.bridge
        if b.gun_switched != self.gun_switched:   # mic button picked another gun: show it
            self.gun_switched = b.gun_switched
            self._at_load()
        if b.native_game:
            text = f"{b.native_game} is running and drives the triggers itself."
        elif w.preview is not None:
            text = "Trying: " + self.tgame.get() + ("  (PS5: the game controls them while it runs)"
                                                     if self.tnative.get() else "")
        elif w.running:
            text = f"Playing {w.running} - using its trigger effects."
        else:
            text = "No listed game running - using \"Other games\" effects."
        self.tstatus.config(text=text)

    def _xset(self, key, value):
        self.cfg["xbox"][key] = value
        save_config(self.cfg)

    def _xtrig_text(self):
        x = self.xbox
        if not self.cfg["xbox"]["trigger_rumble"]:
            return "Trigger effects are off."
        if x.state is None:
            return "Connect the Xbox controller by Bluetooth."
        if x.hid_rumble is None:
            return "Trigger motors are only reachable over Bluetooth - this connection gets normal rumble only."
        if not x.virtual:
            return ("Needs \"Virtual copy on top\" above: in \"Use as is\" games talk to the controller directly, "
                    "so only games with their own trigger rumble (Forza, Gears...) use the trigger motors.")
        return "On. Hold LT/RT while the game rumbles, or pick guns per game in the Triggers tab."

    def _xtrig_toggle(self):
        self._xset("trigger_rumble", self.xtrig.get())

    def _theme(self):
        self._set("theme", ["system", "dark", "light"][self.theme.current()])
        self.theme_hint.config(text="Restart the app (Quit, then open it again) to switch the theme.")

    def _xbox_mode(self):
        self.cfg["xbox"]["mode"] = self.xmode.get()
        save_config(self.cfg)
        self.guard.refresh.set()   # re-apply HidHide now instead of within 30 s

    def _test(self):
        self.ds_view.test()
        self.xb_view.test()

    def _colour(self):
        rgb, _ = colorchooser.askcolor(color="#%02x%02x%02x" % tuple(self.cfg["lightbar"]), title="Lightbar colour")
        if rgb:
            self._set("lightbar", [int(c) for c in rgb])

    def _firmware(self):
        b = self.bridge
        if b.paused.is_set():
            b.paused.clear()
            self.fw_btn.config(text="Update firmware...")
            return
        exe = find_sony_updater()
        if not exe:
            if messagebox.askyesno(
                    "Update firmware",
                    "Sony's official 'Firmware updater for DualSense' isn't installed.\n\n"
                    "Open Sony's download page? Install it, then press this button again.\n"
                    "(The controller must be connected by USB cable to update.)"):
                webbrowser.open(SONY_UPDATER_PAGE)
            return
        b.paused.set()  # release the controller so Sony's tool can open it
        self.fw_btn.config(text="Resume Xbox mode")
        proc = subprocess.Popen([exe], cwd=os.path.dirname(exe))

        def resume_when_done():
            proc.wait()
            b.paused.clear()
            self.root.after(0, lambda: self.fw_btn.config(text="Update firmware..."))
        threading.Thread(target=resume_when_done, daemon=True).start()

    def _toggle_startup(self):
        if self.startup.get():
            pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            with open(STARTUP_CMD, "w", encoding="utf-8") as f:
                f.write(f'@start "" "{pyw}" "{os.path.abspath(__file__)}" --minimized\n')
        elif os.path.exists(STARTUP_CMD):
            os.remove(STARTUP_CMD)

    def _check_battery(self, name, bridge):
        """Tray warning when a controller drops below the threshold; repeats every 15 minutes while low."""
        pct, charging, _ = bridge.battery_info()
        limit = self.cfg["battery_warning"]
        if pct is None or charging or bridge.state is None or pct >= limit:
            if pct is None or charging or (pct is not None and pct >= limit + 5):
                self.battery_alarm.pop(name, None)
            return
        if time.time() - self.battery_alarm.get(name, 0) < 15 * 60:
            return
        self.battery_alarm[name] = time.time()
        try:
            self.tray.notify(f"{name} battery is at {pct}% - time to charge it.", "Low controller battery")
        except Exception:
            pass
        import winsound
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)

    def _tick(self):
        visible = self.root.state() != "withdrawn"
        if visible:
            self.ds_view.tick(self.guard.reconnect_hint("VID_054C"))
            self.xb_view.tick(self.guard.reconnect_hint("VID_045E"))
            for bar, v in zip(self.xtbars + self.xtbars2, self.xbox.trigger_motors * 2):
                bar["value"] = v
            self.xtstatus.config(text=self._xtrig_text())
            self.hbass["value"] = self.haptics.bass * 100
            self.hlevel["value"] = self.haptics.level * 100
            self.haptics_status.config(text=self.haptics.status)
            self.guard_lbl.config(text=self.guard.status)
            for name, view in (("PS5", self.ds_view), ("Xbox", self.xb_view)):
                dot, lbl = self.chips[name]
                on = view.bridge.state is not None
                dot.config(foreground=self.pal["ok"] if on else self.pal["line"])
                saver = "  \u00b7  battery saver" if on and view.bridge.saving else ""
                lbl.config(text=f"{name}  {view.battery_text() or 'connected'}{saver}" if on else f"{name}  off",
                           foreground=self.pal["text"] if on else self.pal["dim"])
            states = [f"{n}: {'saving' if v.bridge.saving else 'normal'}"
                      for n, v in (("PS5", self.ds_view), ("Xbox", self.xb_view)) if v.bridge.state is not None]
            self.saver_status.config(text="Now: " + ("   ".join(states) if states else "no controller connected"))
        self._at_tick(visible)
        self._check_battery("PS5 controller", self.bridge)
        self._check_battery("Xbox controller", self.xbox)
        lines = [APP_NAME]
        for label, view in (("PS5", self.ds_view), ("Xbox", self.xb_view)):
            if view.bridge.state is not None:
                lines.append(f"{label}: {view.battery_text() or 'connected'}")
        tip = "\n".join(lines if len(lines) > 1 else lines + ["No controller connected"])
        if tip != self.tray_tip:
            self.tray.title = tip[:127]
            self.tray_tip = tip
        while not self.commands.empty():
            self.commands.get_nowait()()
        # In the tray only the battery alarm and tray tooltip need updating: stay mostly idle.
        self.root.after(50 if visible else 500, self._tick)

    def _quit(self):
        self.tray.stop()
        for t in (self.bridge, self.haptics, self.xbox, self.guard, self.watcher):
            t.stop()
        self.bridge.join(timeout=2)
        self.xbox.join(timeout=2)
        if hidhide.available():
            try:
                hidhide.release()   # games see the real controllers again while the app isn't running
            except Exception:
                pass
        self.root.destroy()


def main():
    k32 = ctypes.windll.kernel32
    k32.CreateEventW.restype = ctypes.c_void_p
    # A second launch (e.g. the desktop shortcut) signals this event so the running copy pops out of the tray.
    show_event = k32.CreateEventW(None, False, False, "Local\\PS5toXbox_show")
    k32.CreateMutexW(None, False, "Local\\PS5toXbox_single_instance")
    if k32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        k32.SetEvent(ctypes.c_void_p(show_event))
        return
    tune_process_for_latency()
    cfg = load_config()
    try:
        bridge = Bridge(cfg)
    except Exception as e:
        messagebox.showerror(APP_NAME, f"Could not create the virtual Xbox controller.\n"
                                            f"Is ViGEmBus installed?\n\n{e}")
        return
    bridge.start()
    haptics = AudioHaptics(cfg)
    haptics.start()
    xbox = XboxBridge(cfg["xbox"])
    xbox.root_cfg = cfg
    xbox.start()
    guard = ControllerGuard(bridge, xbox)
    guard.start()
    watcher = GameWatcher(cfg, bridge, haptics, xbox)
    watcher.start()
    root = tk.Tk()
    app = App(root, cfg, bridge, haptics, xbox, guard, watcher)

    def wait_for_show():
        while k32.WaitForSingleObject(ctypes.c_void_p(show_event), 0xFFFFFFFF) == 0:
            app.commands.put(app.show)
    threading.Thread(target=wait_for_show, daemon=True).start()

    if "--minimized" in sys.argv:
        app.hide()
    elif "--tab" in sys.argv:   # open on a given tab (0-3), e.g. from a shortcut
        app.nb.select(int(sys.argv[sys.argv.index("--tab") + 1]))
    root.mainloop()


if __name__ == "__main__":
    main()
