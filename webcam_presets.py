#!/usr/bin/env python3
"""Shared webcam preset engine.

    python3 webcam_presets.py night|night-mono|golden-am|day|bright-sun|golden-pm [/dev/videoN]

Why this exists: these scripts used to hard-code one camera's numbers. The
older webcam uses the pre-5.10 kernel control names (exposure_auto,
exposure_absolute, focus_auto) on 0-255 ranges, while the Walmart ONN webcam
uses the current names (auto_exposure, exposure_time_absolute) on much smaller
ranges. The same number means a completely different brightness on each. So
this reads the control list from the camera that is actually plugged in, picks
the matching value table, and clamps every value to the range that camera
reports.

Night brightness comes from a long exposure, not from the brightness control.
brightness and gain saturate quickly; exposure is what collects more light.
The camera is left in aperture priority with the dynamic frame rate allowed,
so it lengthens its own exposure as the light drops rather than being pinned
to one value chosen at the moment the preset ran. See AE_MODE below for why
that beats asking for a long manual exposure.
"""

import os
import re
import subprocess
import sys
import time

# Do not traceback when the output is piped into something like `head`. Set at
# import time so the thin wrapper scripts get it too, not only a direct run.
try:
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except (ImportError, AttributeError, ValueError):
    pass

VALID = ('night', 'night_mono', 'golden_am', 'day', 'bright_sun', 'golden_pm')

# Exposure mode, used by every preset on both cameras.
#
# Aperture priority (3) rather than manual (1), with the dynamic frame rate
# allowed. The kernel documents EXPOSURE_AUTO_PRIORITY as only having any
# effect while auto exposure is AUTO or APERTURE_PRIORITY - in manual mode it
# does nothing at all - and what it does is let the driver drop the frame rate
# so that a longer exposure fits. In manual mode the exposure can never exceed
# the frame interval, so asking for a long one just got it clamped.
#
# Together these let the camera lengthen its own exposure past the frame
# interval as the light drops, and keep adjusting through the night, instead of
# being pinned to one value chosen when the preset ran.
#
# Set AE_MODE back to 1 to return to manual, in which case the exposure numbers
# in the tables below are sent and the search for the longest one the driver
# accepts is used again. Both paths are kept working.
AE_MODE = 3
DYN_FRAMERATE = 1

# Cold white balance on the presets that ask for it.
#
# white_balance_temperature sets the colour temperature the camera ASSUMES the
# light to be, not the look of the picture. A low value tells the camera the
# light is warm, so it compensates with blue and the picture comes out cold.
# 2800 is the coldest-looking setting most webcams accept; anything lower is
# clamped to the camera's own minimum.
#
# The rows below carry auto_wb=1 where automatic white balance is wanted
# instead: Day and Golden AM, which are the camera's own defaults, and Golden
# PM, where the light swings from white through orange to dusk inside the hour
# the preset covers. Pinning that to 2800 was wrong twice over - no single
# temperature is right for more than a few minutes of it, and telling the
# camera to expect warm light makes it compensate with blue, cancelling the
# colour of the evening the preset exists to capture.
COLD_WB = 2800

# ---------------------------------------------------------------------------
# The night camera settings, measured on the camera itself rather than tuned
# here, and the single source for both value tables below.
#
# These are in the legacy 0-255 control scale, which is the scale they were
# measured on (sharpness 250 of 255, gain at the top of the range). The
# modern ONN camera has its own, much smaller ranges - sharpness 0-7,
# contrast 0-95 - so the same numbers are mapped onto those ranges by
# position within the range rather than copied as raw values, which is what
# night_values() below does. Copying them raw would clamp sharpness 250 to 7
# and contrast 32 to 32 of 95, a different picture on each camera.
#
# aperture priority and the exposure are deliberately NOT here: both tables
# keep the exposure they already had, and AE_MODE still owns the mode.
NIGHT_CAMERA = dict(sharpness=250, brightness=193, contrast=32,
                    saturation=32, gain=255)
NIGHT_SCALE = 255          # the control scale NIGHT_CAMERA was measured on

# Night settings do not take on this camera until day settings have been
# written once first - the driver keeps the previous frame settings otherwise
# and the night values never reach the sensor. So the night apply writes the
# day row once, holds it, and goes to night: ONE flip, and the day picture is
# held no longer than this many seconds.
NIGHT_PRIME_HOLD = 4.0


# legacy: the numbers already tuned for the older camera, kept as they were.
LEGACY = {
    # Night is NIGHT_CAMERA: the settings measured on the camera, in the
    # 0-255 scale they were measured in, so they go in here unchanged. The
    # exposure and the aperture priority flag are the ones this table always
    # had and are deliberately untouched - only gain, brightness, contrast,
    # saturation and sharpness come from the measurement.
    'night':      dict(sharpness=250, brightness=193, contrast=32, saturation=32,
                       gain=255, exposure=2047, aepri=1,
                       backlight_compensation=0, zoom_absolute=1),
    # Colour noise is most of what makes a dark frame look muddy, so this
    # trades colour for a cleaner, apparently brighter picture. Everything
    # else is the night row; only the saturation differs.
    'night_mono': dict(sharpness=250, brightness=193, contrast=32, saturation=0,
                       gain=255, exposure=2047, aepri=1,
                       backlight_compensation=0, zoom_absolute=1),
    # backlight_compensation used to be left out of the daylight rows entirely.
    # The apply loop skips any key that is absent, so it was never sent and
    # kept whatever the last preset set. Coming off night that is 255, which
    # washes out a daylight picture badly, and since night runs every evening
    # that was the value that stuck. It is always sent now.
    # Day and Golden AM are the camera's own default light settings, taken from
    # the Defaults preset in the ONN settings GUI and scaled onto this camera's
    # 0-255 ranges the same way the night row was: sharpness 3 of 7 -> 109,
    # brightness mid-range -> 128, contrast 32 of 95 -> 86, saturation 64 of
    # 120 -> 136. Both rows are deliberately identical.
    'golden_am':  dict(sharpness=109, brightness=128, contrast=86, saturation=136,
                       gain=0, exposure=60, aepri=1,
                       backlight_compensation=6, zoom_absolute=0, auto_wb=1),
    'day':        dict(sharpness=109, brightness=128, contrast=86, saturation=136,
                       gain=0, exposure=60, aepri=1,
                       backlight_compensation=6, zoom_absolute=0, auto_wb=1),
    'bright_sun': dict(sharpness=250, brightness=153, contrast=32, saturation=32,
                       gain=0, exposure=4, aepri=0,
                       backlight_compensation=0, zoom_absolute=1),
    'golden_pm':  dict(sharpness=250, brightness=153, contrast=32, saturation=32,
                       gain=104, exposure=50, aepri=0,
                       backlight_compensation=16, zoom_absolute=1, auto_wb=1),
}

# modern: the numbers tuned for the ONN webcam.
MODERN = {
    # Night brightness and contrast were retuned after measuring a real night
    # frame from this camera. brightness on these webcams is a black level
    # lift, not exposure: it collects no extra light, it only raises the floor.
    # Set near maximum, as it was here, the darkest part of the frame sat at 97
    # of 255 and the whole scene was squeezed into about 45 levels - grey fog
    # with the subject invisible in it. Brightness is now well down and
    # contrast well up, so the range is actually used, and the light comes from
    # the aperture priority exposure instead. backlight_compensation is off at
    # night for the same reason: it lifts shadows, which is more of the fog.
    # Retuned again against a second night frame, with the viewer left at 30
    # fps. At 30 fps the driver caps the exposure near 33 ms, so gain has to
    # stay at 100 and the sensor noise that comes with it cannot be removed by
    # any control here - only a longer exposure would do that. What the
    # controls can still fix is measured on that frame: the 1st percentile sat
    # at 41 of 255, so the sky never reached black and the whole scene was
    # veiled grey. brightness is down to 6 to put the floor back on black,
    # contrast eased to 50 now that the floor is right, saturation down to 14
    # because chroma noise at gain 100 was casting the tarmac cyan-green, and
    # sharpness down to 1 because sharpening at this noise level crisps the
    # grain rather than the subject. Measured after: black point 1 of 255,
    # noise down by a third, and the car at 60 ft reads as a car.
    # The five frame controls here are placeholders: night_values() replaces
    # them at apply time with NIGHT_CAMERA mapped onto this camera's own
    # ranges. They are written out as the nominal ONN equivalents so the
    # table still reads as a table, and so that a camera whose ranges cannot
    # be read falls back to something sane rather than to nothing.
    'night':      dict(sharpness=7, brightness=48, contrast=12, saturation=15,
                       gain=255, exposure=5000, aepri=0,
                       backlight_compensation=0, zoom_absolute=0),
    'night_mono': dict(sharpness=7, brightness=48, contrast=12, saturation=0,
                       gain=255, exposure=5000, aepri=0,
                       backlight_compensation=0, zoom_absolute=0),
    # Day and Golden AM are the camera's own default light settings, copied
    # from the Defaults preset in the ONN settings GUI rather than tuned.
    # Both rows are deliberately identical.
    'golden_am':  dict(sharpness=3, brightness=0, contrast=32, saturation=64,
                       gain=0, exposure=157, aepri=1,
                       backlight_compensation=6, zoom_absolute=0, auto_wb=1),
    'day':        dict(sharpness=3, brightness=0, contrast=32, saturation=64,
                       gain=0, exposure=157, aepri=1,
                       backlight_compensation=6, zoom_absolute=0, auto_wb=1),
    'bright_sun': dict(sharpness=7, brightness=-30, contrast=40, saturation=50,
                       gain=0, exposure=2, aepri=0,
                       backlight_compensation=0, zoom_absolute=0),
    'golden_pm':  dict(sharpness=5, brightness=8, contrast=28, saturation=70,
                       gain=35, exposure=250, aepri=0,
                       backlight_compensation=10, zoom_absolute=0, auto_wb=1),
}

CTRL_LINE = re.compile(
    r'^\s*(\w+)\s+0x[0-9a-f]+\s+\((?:int|bool|menu|intmenu)\)', re.IGNORECASE)


class Camera:
    def __init__(self, device='/dev/video0'):
        self.device = device
        self.defaults = {}
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
            # The driver's own default, kept because the daylight presets do
            # not set gamma at all - they leave it wherever the camera starts,
            # which is this value. That is what the night gamma is pinned to.
            default = re.search(r'default=(-?\d+)', line)
            if default:
                self.defaults[m.group(1)] = int(default.group(1))
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

    def _exposure_sticks(self, try_val):
        """Set the exposure and report what held, or None if it was clamped."""
        subprocess.run(['v4l2-ctl', '-d', self.device,
                        '--set-ctrl', f'{self.exp_ctrl}={try_val}'],
                       stderr=subprocess.DEVNULL)
        got = self.get_ctrl(self.exp_ctrl)
        return got if got is not None and got >= try_val * 0.9 else None

    def set_long_exposure(self, wanted):
        """Ask for the longest exposure the driver will actually take.

        The kernel documents exposure as limited by the frame interval, and the
        driver clamps anything longer without saying so, so the only way to
        find the limit is to ask and read back.

        This used to try a fixed ladder of fractions of what was wanted - 100%,
        80%, 60%, 50%, 40%, 30%, 20%, 10% - and keep the first that stuck. That
        gives away a lot of light, because the real limit almost never lands on
        one of eight fixed steps. Measured against a driver that would accept
        626, the ladder settled on 500; against a legacy limit of 333 it
        settled on 204, giving away nearly 40% of the exposure time.

        So: try what was asked for, and if it is clamped, binary search for the
        largest value the driver keeps. A handful more calls, and it lands
        within a couple of percent of the limit instead of within twenty.
        """
        if self.exp_ctrl not in self.controls:
            return None
        lo_range, hi_range = self.controls[self.exp_ctrl]
        wanted = min(wanted, hi_range)
        floor = max(1, lo_range)

        got = self._exposure_sticks(wanted)
        if got is not None:
            print(f'  {self.exp_ctrl:<30} {got}')
            return got

        # Clamped. hi is known to fail, lo is the longest known to work.
        lo, hi = floor, wanted
        best = self._exposure_sticks(lo)
        if best is None:
            # Even the shortest exposure reads back wrong; nothing to search.
            got = self.get_ctrl(self.exp_ctrl)
            print(f'  {self.exp_ctrl:<30} {got} (driver clamped)')
            return got

        # Stop once the gap is under 2%, far below anything visible, rather
        # than burning calls narrowing it to the last unit.
        rounds = 0
        while hi - lo > 1 and hi - lo > lo * 0.02 and rounds < 16:
            rounds += 1
            mid = (lo + hi) // 2
            if mid <= lo or mid >= hi:
                break
            ok = self._exposure_sticks(mid)
            if ok is not None:
                lo, best = mid, ok
            else:
                hi = mid

        # Leave the camera on the best value found, since the search may have
        # ended on a failed attempt.
        final = self._exposure_sticks(lo)
        if final is not None:
            best = final
        print(f'  {self.exp_ctrl:<30} {best} (driver clamped from {wanted})')
        return best


def night_values(cam, preset):
    """NIGHT_CAMERA mapped onto whatever ranges this camera actually has.

    The numbers were measured in the legacy 0-255 scale. On a legacy camera
    they go through untouched. On the modern ONN camera, whose ranges are
    much smaller and in one case signed, each one is placed at the same
    position within the control's own min..max as it sat within 0..255 - so
    sharpness 250 of 255 becomes sharpness near the ONN maximum rather than
    being clamped to it from a number 35 times too large, and brightness 193
    of 255 lands three quarters of the way up a range that starts below zero.

    night_mono keeps its saturation 0: that preset exists to throw the colour
    away, and the measurement is not about colour.
    """
    out = {}
    for name, value in NIGHT_CAMERA.items():
        if preset == 'night_mono' and name == 'saturation':
            out[name] = 0
            continue
        span = cam.controls.get(name)
        if span is None:
            out[name] = value
            continue
        lo, hi = span
        if (lo, hi) == (0, NIGHT_SCALE):
            out[name] = value
        else:
            out[name] = int(round(lo + (hi - lo) * (value / float(NIGHT_SCALE))))
    return out


def day_gamma(cam):
    """The gamma the daylight presets run at.

    None of the preset rows set gamma - the light meter may only ever pull it
    DOWN from where the preset left it, and the rows leave it at the camera's
    own default. So the day gamma is that default.
    """
    if 'gamma' not in cam.controls:
        return None
    want = LEGACY['day'].get('gamma', MODERN['day'].get('gamma'))
    if want is not None:
        return want
    return cam.defaults.get('gamma')


def night_gamma(cam):
    """The gamma the night preset writes: the camera's factory default, which
    is also what the daylight presets run at.

    Gamma is left at the factory default on purpose, day and night. Moved
    either way it turns a night frame blurry and grainy - it is a midtone
    curve applied to a frame whose midtones are mostly sensor noise, so
    lifting it lifts the grain and lowering it crushes what little picture is
    there. So night does not get its own gamma, a window to move inside, or
    any headroom: it gets the same number the camera starts at, and the light
    meter puts it back if anything moves it.
    """
    return day_gamma(cam)


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

    # Tell the harvest which preset is in force, so that every still it files
    # from now on is filed under the preset that produced it. That is what
    # gives the brain training frames from across all the presets and all the
    # light of a day rather than whatever the camera happened to be doing.
    # A missing module here changes nothing about applying the preset.
    try:
        import train_webcam_brain                             # noqa: PLC0415
    except ImportError:
        pass
    else:
        train_webcam_brain.note_preset(preset)

    # Manual exposure first: on both APIs the exposure value is ignored while
    # auto exposure is on, and the driver needs a moment to switch over.
    cam.set_ctrl(cam.ae_ctrl, AE_MODE)
    time.sleep(1)
    # Always on, on every preset, rather than the per-preset aepri value that
    # used to be sent here. It is what lets the driver trade frame rate for
    # exposure.
    cam.set_ctrl(cam.aepri_ctrl, DYN_FRAMERATE)

    FRAME_CTRLS = ('sharpness', 'brightness', 'contrast', 'saturation', 'gain',
                   'backlight_compensation')

    if preset in ('night', 'night_mono'):
        # The night values are the measured ones, on this camera's scales.
        vals = dict(vals)
        vals.update(night_values(cam, preset))

        # The one flip. Night settings do not take until the day settings
        # have been written once, so the day row goes on first, is held, and
        # then night follows - once, in that order, never back again. The
        # hold is capped at NIGHT_PRIME_HOLD seconds, which is as long as the
        # camera is allowed to sit on a day picture in the dark.
        day = (MODERN if cam.modern else LEGACY)['day']
        print(f'  priming with the day settings for {NIGHT_PRIME_HOLD:g}s '
              f'so the night values will take')
        started = time.monotonic()
        for name in FRAME_CTRLS:
            if name in day:
                cam.set_ctrl(name, day[name])
        held = time.monotonic() - started
        if held < NIGHT_PRIME_HOLD:
            time.sleep(NIGHT_PRIME_HOLD - held)

    for name in FRAME_CTRLS:
        if name in vals:
            cam.set_ctrl(name, vals[name])

    # Gamma, at night only: the day value, held inside the 20-39% night
    # window. Without this it keeps whatever the last gamma lift left behind,
    # and a gamma raised once in the dark stays raised - which lifts the noise
    # floor in the stream the recorder is writing. Daylight rows leave gamma
    # alone, as they always have.
    if preset in ('night', 'night_mono'):
        cam.set_ctrl('gamma', night_gamma(cam))

    # Focus is set manually so the camera does not hunt in the dark.
    # Continuous autofocus, always, on every preset. Nothing here ever leaves
    # the camera in manual focus.
    #
    # It is switched off and straight back on rather than simply set to 1,
    # because the light this preset just changed is what the autofocus has to
    # work with: the camera does not always re-run its search on its own, and
    # the first seconds after a preset would otherwise come out soft. The pause
    # is there so the driver sees the control actually change state - without
    # it the two writes can land in the same instant and nothing is
    # re-triggered.
    cam.set_ctrl(cam.focus_auto, 0)
    time.sleep(0.33)
    cam.set_ctrl(cam.focus_auto, 1)
    cam.set_ctrl('zoom_absolute', vals.get('zoom_absolute'))

    # White balance. Automatic where the preset asks for it, and also when this
    # camera has no temperature control at all, because otherwise it would sit
    # in manual at whatever value happened to be set. Everything else gets the
    # cold setting. Automatic is always sent explicitly rather than left alone,
    # since the previous preset may have switched it off.
    if vals.get('auto_wb') or 'white_balance_temperature' not in cam.controls:
        cam.set_ctrl(cam.wb_auto, 1)
    else:
        cam.set_ctrl(cam.wb_auto, 0)
        cam.set_ctrl('white_balance_temperature', COLD_WB)

    # Exposure last, once the frame settings are in place.
    #
    # In aperture priority the camera sets its own exposure and the manual
    # control goes inactive, so there is nothing to send and nothing to search
    # for. Read back what it settled on instead, so the output still says what
    # the camera is actually doing.
    if AE_MODE == 1:
        if preset in ('night', 'night_mono'):
            cam.set_long_exposure(vals['exposure'])
        else:
            cam.set_ctrl(cam.exp_ctrl, vals['exposure'])
    else:
        time.sleep(1)           # let the auto exposure settle before reading
        got = cam.get_ctrl(cam.exp_ctrl)
        print(f'  {cam.exp_ctrl:<30} {got} '
              f'(chosen by the camera, aperture priority)')

    # Night, checked. This preset puts the black point on black, which is
    # right at midnight and wrong at sunset + 14 minutes when there is still
    # light in the sky - applied then it can crush the whole frame. The light
    # meter would normally pull that back, but it is blind whenever the
    # recorder holds the camera and no snapshot is arriving, and blind it
    # leaves the picture black all night. So the picture is measured here and
    # the preset walked back if it has been destroyed. On a properly dark
    # night this measures one frame and does nothing.
    if preset in ('night', 'night_mono'):
        try:
            import webcam_verify                             # noqa: PLC0415
        except ImportError:
            pass
        else:
            try:
                webcam_verify.guard(preset, device)
            except Exception as problem:                      # noqa: BLE001
                print(f'  could not check the night picture: {problem}')
    return True


def main():
    if len(sys.argv) < 2:
        print('usage: webcam_presets.py night|night-mono|golden-am|day|bright-sun|golden-pm '
              '[/dev/videoN]', file=sys.stderr)
        return 2
    device = (sys.argv[2] if len(sys.argv) > 2
              else os.environ.get('WEBCAM_DEVICE', '/dev/video0'))
    return 0 if apply_preset(sys.argv[1], device) else 1


if __name__ == '__main__':
    sys.exit(main())
