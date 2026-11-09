#!/usr/bin/env python3
"""Shared webcam preset engine.

    python3 webcam_presets.py night|golden-am|day|bright-sun|golden-pm [/dev/videoN]

Why this exists: these scripts used to hard-code one camera's numbers. The
older webcam uses the pre-5.10 kernel control names (exposure_auto,
exposure_absolute, focus_auto) on 0-255 ranges, while the Walmart ONN webcam
uses the current names (auto_exposure, exposure_time_absolute) on much smaller
ranges. The same number means a completely different brightness on each. So
this reads the control list from the camera that is actually plugged in, picks
the matching value table, and clamps every value to the range that camera
reports.

Night brightness comes from a long manual exposure, not from the brightness
control. brightness and gain saturate quickly; exposure is what collects more
light. The night preset asks for a long exposure, reads it back, and steps
down until the driver accepts one, because the driver silently clamps any
exposure that does not fit inside the current frame interval.
"""

import os
import re
import subprocess
import sys
import time

VALID = ('night', 'golden_am', 'day', 'bright_sun', 'golden_pm')

# On both APIs the manual setting for auto exposure is 1.
AE_MANUAL = 1

# legacy: the numbers already tuned for the older camera, kept as they were.
LEGACY = {
    'night':      dict(sharpness=250, brightness=193, contrast=32, saturation=32,
                       gain=255, exposure=1873, aepri=1, focus=51, zoom_absolute=1),
    'golden_am':  dict(sharpness=250, brightness=170, contrast=32, saturation=32,
                       gain=150, exposure=200, aepri=0, focus=0, zoom_absolute=1),
    'day':        dict(sharpness=250, brightness=153, contrast=32, saturation=32,
                       gain=104, exposure=4, aepri=0, focus=0, zoom_absolute=1),
    'bright_sun': dict(sharpness=250, brightness=153, contrast=32, saturation=32,
                       gain=0, exposure=4, aepri=0, focus=0, zoom_absolute=1),
    'golden_pm':  dict(sharpness=250, brightness=153, contrast=32, saturation=32,
                       gain=104, exposure=50, aepri=0, focus=0, zoom_absolute=1),
}

# modern: the numbers tuned for the ONN webcam.
MODERN = {
    'night':      dict(sharpness=3, brightness=64, contrast=20, saturation=30,
                       gain=100, exposure=5000, aepri=0, focus=51,
                       backlight_compensation=12, zoom_absolute=0),
    'golden_am':  dict(sharpness=5, brightness=8, contrast=28, saturation=70,
                       gain=45, exposure=350, aepri=0, focus=50,
                       backlight_compensation=10, wb_temp=3400, zoom_absolute=0),
    'day':        dict(sharpness=5, brightness=0, contrast=32, saturation=64,
                       gain=20, exposure=80, aepri=0, focus=50,
                       backlight_compensation=4, zoom_absolute=0),
    'bright_sun': dict(sharpness=7, brightness=-30, contrast=40, saturation=50,
                       gain=0, exposure=2, aepri=0, focus=50,
                       backlight_compensation=0, wb_temp=5500, zoom_absolute=0),
    'golden_pm':  dict(sharpness=5, brightness=8, contrast=28, saturation=70,
                       gain=35, exposure=250, aepri=0, focus=50,
                       backlight_compensation=10, wb_temp=3000, zoom_absolute=0),
}

CTRL_LINE = re.compile(
    r'^\s*(\w+)\s+0x[0-9a-f]+\s+\((?:int|bool|menu|intmenu)\)', re.IGNORECASE)


class Camera:
    def __init__(self, device='/dev/video0'):
        self.device = device
        self.controls = self._read_controls()
        self.modern = 'exposure_time_absolute' in self.controls
        # Control names differ between the two APIs.
        self.exp_ctrl   = 'exposure_time_absolute'     if self.modern else 'exposure_absolute'
        self.ae_ctrl    = 'auto_exposure'              if self.modern else 'exposure_auto'
        self.aepri_ctrl = 'exposure_dynamic_framerate' if self.modern else 'exposure_auto_priority'
        self.focus_auto = 'focus_automatic_continuous' if self.modern else 'focus_auto'
        self.wb_auto    = ('white_balance_automatic' if self.modern
                           else 'white_balance_temperature_auto')

    def _read_controls(self):
        """Parse `v4l2-ctl -L` into {name: (min, max)}."""
        try:
            out = subprocess.run(['v4l2-ctl', '-d', self.device, '-L'],
                                 capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return {}
        controls = {}
        for line in out.splitlines():
            m = CTRL_LINE.match(line)
            if not m:
                continue
            lo = re.search(r'min=(-?\d+)', line)
            hi = re.search(r'max=(-?\d+)', line)
            # bool controls report no min/max
            controls[m.group(1)] = ((int(lo.group(1)), int(hi.group(1)))
                                    if lo and hi else (0, 1))
        return controls

    def set_ctrl(self, name, value):
        """Set one control, clamped to its real range. Missing controls are skipped."""
        if name is None or value is None or name not in self.controls:
            return False
        lo, hi = self.controls[name]
        clamped = max(lo, min(hi, value))
        subprocess.run(['v4l2-ctl', '-d', self.device,
                        '--set-ctrl', f'{name}={clamped}'],
                       stderr=subprocess.DEVNULL)
        note = f' (asked {value}, clamped)' if clamped != value else ''
        print(f'  {name:<30} {clamped}{note}')
        return True

    def get_ctrl(self, name):
        if name not in self.controls:
            return None
        try:
            out = subprocess.run(['v4l2-ctl', '-d', self.device, '--get-ctrl', name],
                                 capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        m = re.search(r':\s*(-?\d+)', out)
        return int(m.group(1)) if m else None

    def set_long_exposure(self, wanted):
        """Ask for the longest exposure the driver will take.

        It clamps anything that does not fit in the frame interval, so request,
        read back, and step down until one holds.
        """
        if self.exp_ctrl not in self.controls:
            return None
        wanted = min(wanted, self.controls[self.exp_ctrl][1])
        for frac in (10, 8, 6, 5, 4, 3, 2, 1):
            try_val = max(1, wanted * frac // 10)
            subprocess.run(['v4l2-ctl', '-d', self.device,
                            '--set-ctrl', f'{self.exp_ctrl}={try_val}'],
                           stderr=subprocess.DEVNULL)
            got = self.get_ctrl(self.exp_ctrl)
            if got is not None and got >= try_val * 0.9:
                print(f'  {self.exp_ctrl:<30} {got}')
                return got
        got = self.get_ctrl(self.exp_ctrl)
        print(f'  {self.exp_ctrl:<30} {got} (driver clamped)')
        return got


def apply_preset(preset, device='/dev/video0'):
    """Apply one preset to whichever camera is plugged in. True on success."""
    preset = preset.replace('-', '_').lower()
    if preset not in VALID:
        print(f'unknown preset: {preset}', file=sys.stderr)
        return False

    cam = Camera(device)
    if not cam.controls:
        print(f'No controls found on {device}. Is the camera plugged in?',
              file=sys.stderr)
        return False

    vals = (MODERN if cam.modern else LEGACY)[preset]
    print(f'{preset} preset on {device} '
          f'({"current" if cam.modern else "legacy"} control names)')

    # Manual exposure first: on both APIs the exposure value is ignored while
    # auto exposure is on, and the driver needs a moment to switch over.
    cam.set_ctrl(cam.ae_ctrl, AE_MANUAL)
    time.sleep(1)
    cam.set_ctrl(cam.aepri_ctrl, vals.get('aepri'))

    for name in ('sharpness', 'brightness', 'contrast', 'saturation', 'gain',
                 'backlight_compensation'):
        if name in vals:
            cam.set_ctrl(name, vals[name])

    # Focus is set manually so the camera does not hunt in the dark.
    cam.set_ctrl(cam.focus_auto, 0)
    cam.set_ctrl('focus_absolute', vals.get('focus'))
    cam.set_ctrl('zoom_absolute', vals.get('zoom_absolute'))

    # White balance: manual and warm during golden hour, automatic otherwise.
    if 'wb_temp' in vals:
        cam.set_ctrl(cam.wb_auto, 0)
        cam.set_ctrl('white_balance_temperature', vals['wb_temp'])
    else:
        cam.set_ctrl(cam.wb_auto, 1)

    # Exposure last, once the frame settings are in place.
    if preset == 'night':
        cam.set_long_exposure(vals['exposure'])
    else:
        cam.set_ctrl(cam.exp_ctrl, vals['exposure'])
    return True


def main():
    if len(sys.argv) < 2:
        print('usage: webcam_presets.py night|golden-am|day|bright-sun|golden-pm '
              '[/dev/videoN]', file=sys.stderr)
        return 2
    device = (sys.argv[2] if len(sys.argv) > 2
              else os.environ.get('WEBCAM_DEVICE', '/dev/video0'))
    return 0 if apply_preset(sys.argv[1], device) else 1


if __name__ == '__main__':
    # Do not traceback when the output is piped into something like `head`.
    try:
        import signal
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (ImportError, AttributeError, ValueError):
        pass
    sys.exit(main())
