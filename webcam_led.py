#!/usr/bin/env python3
"""Turn the webcam's LED off, when the camera lets you.

Most webcams do not. The LED is a privacy light and on a lot of hardware it
is wired to the sensor's power rail, so nothing in software can touch it - on
those cameras it comes on whenever the camera is streaming and that is the
end of it. A few expose it, as a UVC extension control called `led1_mode`:

    0  off       1  on       2  blink       3  auto

So this looks for that control, sets it, and then reads it back to see
whether the camera actually took the setting - which is the part that
matters, because a camera that ignores the write still returns success. If
there is no such control it says so plainly rather than pretending, because
"the button did nothing and said it worked" is worse than "this camera will
not let me".

Two ways in, tried in order:

  1. v4l2-ctl, when the driver has mapped the control. The usual case.
  2. uvcdynctrl with Logitech's mapping file, which exposes 'LED1 Mode' on
     some cameras whose driver has not mapped it. Only if it is installed.

There is a known kernel regression - 6.1 to 6.3 and after, on some cameras -
where led1_mode turns up with min=4 max=4 and no working values. That is
detected and named, because from the outside it looks exactly like a camera
that is ignoring the write.

    python3 webcam_led.py --off
    python3 webcam_led.py --on
    python3 webcam_led.py --status
"""

import os
import re
import shutil
import subprocess
import sys

DEVICE = os.environ.get('WEBCAM_DEVICE', '/dev/video0')

MODES = {'off': 0, 'on': 1, 'blink': 2, 'auto': 3}
NAMES = {0: 'off', 1: 'on', 2: 'blink', 3: 'auto'}

# Anything the camera calls an LED. Spelt out rather than hardcoding
# led1_mode, because a few cameras name it led_mode or led1_mode_alt.
LED_RE = re.compile(r'^\s*(\w*led\w*)\s+0x[0-9a-f]+\s+\((menu|int|bool)\)\s*:(.*)$',
                    re.I)


def _run(args, timeout=10):
    try:
        done = subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout)
        return done.returncode, done.stdout or '', done.stderr or ''
    except (OSError, subprocess.SubprocessError) as problem:
        return 1, '', str(problem)


def find_control(device=DEVICE):
    """The camera's LED control, if it has one.

    Returns a dict with the control's name, its range and its current value,
    or None. The range matters: the kernel regression shows up here as
    min=4 max=4, which is a control that exists and cannot be set.
    """
    if not shutil.which('v4l2-ctl'):
        return None
    code, out, _err = _run(['v4l2-ctl', '-d', device, '-L'])
    if code != 0:
        return None
    for line in out.splitlines():
        match = LED_RE.match(line)
        if not match:
            continue
        name, kind, rest = match.group(1), match.group(2), match.group(3)
        found = {'name': name, 'type': kind, 'min': None, 'max': None,
                 'value': None}
        for key in ('min', 'max', 'value'):
            got = re.search(rf'\b{key}=(-?\d+)', rest)
            if got:
                found[key] = int(got.group(1))
        return found
    return None


def _uvcdynctrl_set(mode):
    """The fallback, for cameras whose driver never mapped the control."""
    if not shutil.which('uvcdynctrl'):
        return False, 'uvcdynctrl is not installed'
    mapping = '/usr/share/uvcdynctrl/data/046d/logitech.xml'
    if os.path.exists(mapping):
        _run(['uvcdynctrl', '-i', mapping])
    code, _out, err = _run(['uvcdynctrl', '-s', 'LED1 Mode', str(mode)])
    if code == 0:
        return True, "set through uvcdynctrl ('LED1 Mode')"
    return False, (err.strip().splitlines() or ['uvcdynctrl refused it'])[0]


def status(device=DEVICE):
    """What the LED is doing now, in a sentence fit for a label."""
    control = find_control(device)
    if control is None:
        return False, ('this camera does not expose its LED - on most webcams '
                       'the light is wired to the sensor and no software can '
                       'turn it off')
    if control['min'] == control['max']:
        return False, (f"{control['name']} is there but has no usable values "
                       f"(min={control['min']} max={control['max']}) - a known "
                       f"kernel bug on some cameras since 6.3")
    value = control['value']
    where = NAMES.get(value, str(value))
    return True, f"{control['name']} is {where}"


def set_mode(mode='off', device=DEVICE):
    """Set the LED and check the camera took it.

    Returns (worked, what happened). The read-back is the whole point: a
    camera that quietly ignores the write returns success from v4l2-ctl, and
    reporting that as done would be a lie the user can see is a lie by
    looking at the camera.
    """
    if isinstance(mode, str):
        if mode not in MODES:
            return False, f'no such LED mode: {mode}'
        wanted = MODES[mode]
    else:
        wanted = int(mode)

    control = find_control(device)
    if control is None:
        worked, how = _uvcdynctrl_set(wanted)
        if worked:
            return True, how
        return False, ('this camera does not expose its LED, and '
                       f'{how}. On most webcams the light is wired to the '
                       'sensor and nothing in software can turn it off.')

    if control['min'] == control['max']:
        return False, (f"{control['name']} has no usable values "
                       f"(min={control['min']} max={control['max']}) - a known "
                       f"kernel bug on some cameras since 6.3, not something "
                       f"this program can work around")

    name = control['name']
    code, _out, err = _run(['v4l2-ctl', '-d', device,
                            '--set-ctrl', f'{name}={wanted}'])
    if code != 0:
        first = (err.strip().splitlines() or ['the camera refused it'])[0]
        worked, how = _uvcdynctrl_set(wanted)
        if worked:
            return True, how
        return False, f'{name}: {first}'

    # Read it back. This is the check that counts.
    after = find_control(device)
    now = after['value'] if after else None
    if now == wanted:
        return True, f'{name} is now {NAMES.get(wanted, wanted)}'
    return False, (f'{name} was set to {NAMES.get(wanted, wanted)} but the '
                   f'camera reports {NAMES.get(now, now)} - it is ignoring '
                   f'the setting, which some firmware does')


def off(device=DEVICE):
    return set_mode('off', device)


def on(device=DEVICE):
    return set_mode('on', device)


def main():
    args = sys.argv[1:]
    device = DEVICE
    if '--device' in args:
        try:
            device = args[args.index('--device') + 1]
        except IndexError:
            pass
    if '--status' in args:
        worked, text = status(device)
    elif '--on' in args:
        worked, text = on(device)
    else:
        worked, text = off(device)
    # One line, so the perl window can read it with a single readline.
    print(f"{1 if worked else 0}|{text}")
    return 0 if worked else 1


if __name__ == '__main__':
    sys.exit(main())
