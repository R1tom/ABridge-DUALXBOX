"""Shared-memory link with the KILA Local Game Library in-game overlay.

16 bytes in the named mapping Local\\ABridgePadLink:
  0-1  buttons  (XInput bits, little endian) - written here, so the overlay can read the pad even
                while the game is getting a neutral one
  2    hold     - set by the overlay while its panel is open: games get a neutral pad
  3    guide    - set by the overlay when the Guide button opens it: Guide is not passed to games

Cost in the input loop: a compare, and one byte read per report. No locks, no syscalls.
"""
import mmap

NEUTRAL = (0, 0, 0, 0, 0, 0, 0)
GUIDE = 0x0400


class Link:
    def __init__(self):
        try:
            self.mm = mmap.mmap(-1, 16, tagname="Local\\ABridgePadLink")
        except Exception:
            self.mm = None
        self.last = -1

    def filter(self, btn):
        """Publish the real buttons. Returns the buttons the game should get, or None for a neutral pad."""
        mm = self.mm
        if mm is None:
            return btn
        if btn != self.last:
            mm[0] = btn & 0xFF
            mm[1] = (btn >> 8) & 0xFF
            self.last = btn
        if mm[2]:
            return None
        if mm[3]:
            return btn & ~GUIDE
        return btn


LINK = Link()
