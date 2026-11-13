#!/usr/bin/env python3
"""Did the camera actually take the setting, and did the picture get better?

Two questions that are easy to assume and were both wrong at sunset.

**Did it take.** v4l2-ctl returns success for a write the camera then ignores,
and the camera is usually being held by the recorder - Xeoma, here - which has
its own idea about exposure and white balance and will happily put them back.
Controls are per-device rather than per-handle, so writing them from outside
the recorder does reach the stream; what is not guaranteed is that they stay.
So every control is written and then read back, and what is reported is what
the camera says afterwards, not what it was told.

**Did it help.** The night preset is tuned to put the black point on black,
which is right in full dark and wrong at sunset, when there is still light in
the sky. Applied at sunset + 14 minutes it can crush the whole frame, and the
light meter that would have corrected it is blind whenever the recorder has
the camera and no snapshot is arriving. Blind, it leaves the picture black all
night.

So after a preset is applied the newest snapshot is measured, and if the frame
has been crushed the preset is walked back - a step at a time, measuring after
each - until the picture is in range again. The numbers it works to:

    crushed   more than 55% of the frame at or near black
    dark      mean below 28 of 255
    blown     more than 4% at or near white

Walking back beats re-tuning the constants, because the constants cannot be
right for both sunset and midnight, and this does not need them to be.

    python3 webcam_verify.py --readback night
    python3 webcam_verify.py --measure
    python3 webcam_verify.py --guard night
"""

import glob
import os
import re
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import webcam_errors
except ImportError:                                           # noqa: BLE001
    webcam_errors = None

DEVICE = os.environ.get('WEBCAM_DEVICE', '/dev/video0')

# What counts as a picture worth keeping. Deliberately generous: this is a
# guard against a frame that has been destroyed, not a second light meter.
CRUSHED = 0.55          # fraction at or near black
DARK_MEAN = 28.0        # mean out of 255
BLOWN = 0.04            # fraction at or near white

# The way back, in order. Each is (control, step, limit) - the step is added
# each round until the limit, which is roughly where the day preset sits, so
# the worst case is a night frame that looks like a day frame rather than a
# black one.
BACKOFF = [
    ('brightness', +8, 48),
    ('contrast', -6, 24),
    ('backlight_compensation', +2, 8),
]
MAX_ROUNDS = 6


def _note(stage, text):
    if webcam_errors is not None:
        try:
            webcam_errors.note(stage, text)
        except Exception:                                     # noqa: BLE001
            pass
    print(f'{stage}: {text}')


def _run(args, timeout=15):
    try:
        done = subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout)
        return done.returncode, done.stdout or '', done.stderr or ''
    except (OSError, subprocess.SubprocessError) as problem:
        return 1, '', str(problem)


# ---------------------------------------------------------------- read back


def read_controls(device=DEVICE):
    """Every control the camera has now, as name -> value."""
    found = {}
    if not shutil.which('v4l2-ctl'):
        return found
    code, out, _err = _run(['v4l2-ctl', '-d', device, '-L'])
    if code != 0:
        return found
    for line in out.splitlines():
        match = re.match(r'\s*(\w+)\s+0x[0-9a-f]+\s+\(\w+\)\s*:(.*)', line)
        if not match:
            continue
        value = re.search(r'\bvalue=(-?\d+)', match.group(2))
        if value:
            found[match.group(1)] = int(value.group(1))
    return found


def set_control(name, value, device=DEVICE):
    """Write one control and report what the camera says afterwards.

    The read-back is the whole point. A camera that is being streamed by
    something else will take some controls and quietly refuse others, and
    the only way to tell which is to ask it.
    """
    code, _out, err = _run(['v4l2-ctl', '-d', device,
                            '--set-ctrl', f'{name}={value}'])
    now = read_controls(device).get(name)
    if code != 0:
        return False, now, (err.strip().splitlines() or ['refused'])[0]
    if now is None:
        return False, None, 'no such control on this camera'
    if now != value:
        return False, now, f'asked {value}, camera reports {now}'
    return True, now, 'took'


def readback(wanted, device=DEVICE):
    """Check a whole preset's worth of values against the camera.

    `wanted` is name -> value. Returns (all_took, rows), where each row says
    what was asked, what the camera reports, and whether it stuck. This is
    the answer to "is anything I do here reaching the stream Xeoma is
    recording" - and if the recorder is putting settings back, this is where
    it shows up, as a control that reads differently a moment after it was
    written.
    """
    now = read_controls(device)
    rows = []
    for name, value in sorted(wanted.items()):
        got = now.get(name)
        rows.append({'name': name, 'wanted': value, 'got': got,
                     'ok': got == value,
                     'why': ('not on this camera' if got is None else
                             'matches' if got == value else
                             f'camera has {got}')})
    return all(row['ok'] for row in rows), rows


# ----------------------------------------------------------------- the frame


def newest_snapshot(folder=None):
    """The most recent still, whoever wrote it.

    Xeoma holds the camera most of the time, so these are usually its
    snapshots rather than anything this program captured - which is exactly
    why measuring them is worth doing: they are what is actually being
    recorded.
    """
    folders = [folder] if folder else []
    folders += [os.environ.get('WEBCAM_SNAPSHOT', ''),
                os.path.expanduser('~/Pictures/webcam-sample-images'),
                os.path.expanduser('~/Pictures/onn-webcam/'
                                   'program-snapshots-for-auto-adjust-picture'),
                os.path.expanduser('~/webcam-snapshots')]
    best, when = '', 0
    for place in folders:
        if not place or not os.path.isdir(place):
            continue
        for path in glob.glob(os.path.join(place, '*.jpg')):
            # The enhanced copies and the comparison frames are this
            # program's own output - measuring those would be measuring the
            # correction, not the camera.
            if os.sep + 'enhanced' + os.sep in path or os.sep + 'compare' \
                    + os.sep in path:
                continue
            try:
                stamp = os.path.getmtime(path)
            except OSError:
                continue
            if stamp > when:
                best, when = path, stamp
    return best, when


def measure(path):
    """mean out of 255, the fraction near white, the fraction near black."""
    if not path or not os.path.exists(path):
        return None
    try:
        import cv2                                            # noqa: PLC0415
        import numpy                                          # noqa: PLC0415
    except ImportError:
        return None
    image = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if image is None:
        return None
    total = image.size or 1
    return {'mean': float(image.mean()),
            'blown': float(numpy.count_nonzero(image >= 248)) / total,
            'crushed': float(numpy.count_nonzero(image <= 8)) / total,
            'path': path}


def verdict(shot):
    """Is this a picture, or has it been destroyed?"""
    if not shot:
        return False, 'no snapshot to measure'
    if shot['crushed'] > CRUSHED:
        return False, (f"{shot['crushed'] * 100:.0f}% of the frame is black - "
                       f'the settings have crushed it')
    if shot['mean'] < DARK_MEAN:
        return False, (f"mean {shot['mean']:.0f} of 255 - too dark to see "
                       f'anything in')
    if shot['blown'] > BLOWN:
        return False, f"{shot['blown'] * 100:.0f}% of the frame is blown white"
    return True, (f"mean {shot['mean']:.0f}, {shot['crushed'] * 100:.0f}% "
                  f"black, {shot['blown'] * 100:.0f}% white - usable")


# ------------------------------------------------------------------ the guard


def guard(preset='night', device=DEVICE, folder=None, wait=6.0):
    """After a preset: measure, and walk it back if the picture is destroyed.

    This runs after the night preset because that is the one that can crush a
    frame - it puts the black point on black, which is right at midnight and
    wrong at sunset + 14 minutes when there is still light in the sky.

    Each round nudges brightness up, contrast down and backlight compensation
    up, then waits for a fresh snapshot and measures again. It stops the
    moment the picture is usable, so on a properly dark night it does nothing
    at all and costs one measurement.
    """
    before_path, before_when = newest_snapshot(folder)
    before = measure(before_path)
    ok, why = verdict(before)
    if ok:
        _note('night correction',
              f'{preset} applied, picture checked: {why}')
        return True, why, 0

    _note('night correction',
          f'{preset} applied but the picture is not usable: {why} - '
          f'walking the preset back')

    now = read_controls(device)
    rounds = 0
    while rounds < MAX_ROUNDS:
        rounds += 1
        moved = []
        for name, step, limit in BACKOFF:
            if name not in now:
                continue
            value = now[name] + step
            value = min(value, limit) if step > 0 else max(value, limit)
            if value == now[name]:
                continue
            took, got, why_set = set_control(name, value, device)
            now[name] = got if got is not None else now[name]
            moved.append(f'{name}->{got}' + ('' if took else f' ({why_set})'))
        if not moved:
            _note('night correction',
                  'nothing left to walk back - the controls are already at '
                  'their limits and the picture is still not usable')
            return False, 'out of room', rounds

        # Wait for a snapshot written after the change. Measuring the one
        # that was already there would be measuring the old settings.
        fresh, deadline = None, time.time() + wait
        while time.time() < deadline:
            path, when = newest_snapshot(folder)
            if path and when > before_when:
                fresh = measure(path)
                break
            time.sleep(0.5)
        if fresh is None:
            _note('night correction',
                  f'round {rounds}: {", ".join(moved)} - but no new snapshot '
                  f'arrived within {wait:.0f}s, so this cannot be checked. '
                  f'Is the recorder still writing stills?')
            return False, 'no fresh snapshot to check against', rounds

        before_when = time.time()
        ok, why = verdict(fresh)
        _note('night correction', f'round {rounds}: {", ".join(moved)} - {why}')
        if ok:
            return True, why, rounds

    return False, f'still not usable after {MAX_ROUNDS} rounds', rounds


def main():
    args = sys.argv[1:]
    device = DEVICE
    if '--device' in args:
        try:
            device = args[args.index('--device') + 1]
        except IndexError:
            pass

    if '--coverage' in args:
        return print_coverage()

    if '--measure' in args:
        path, _when = newest_snapshot()
        shot = measure(path)
        ok, why = verdict(shot)
        print(f'newest snapshot: {path or "(none found)"}')
        print(f'{"OK " if ok else "BAD"}  {why}')
        return 0 if ok else 1

    if '--readback' in args:
        try:
            import webcam_presets                             # noqa: PLC0415
            preset = args[args.index('--readback') + 1]
            table = (webcam_presets.MODERN if 'exposure_time_absolute' in
                     read_controls(device) else webcam_presets.LEGACY)
            wanted = {k: v for k, v in table[preset].items()
                      if k not in ('exposure', 'aepri', 'auto_wb')}
        except Exception as problem:                          # noqa: BLE001
            print(f'could not read that preset: {problem}')
            return 1
        every, rows = readback(wanted, device)
        for row in rows:
            mark = 'took' if row['ok'] else 'NO  '
            print(f"  {mark}  {row['name']:<26} asked {row['wanted']:<6} "
                  f"{row['why']}")
        print('every control took' if every else
              'some controls did not take - if the recorder is streaming, it '
              'may be putting them back')
        return 0 if every else 1

    preset = 'night'
    if '--guard' in args:
        try:
            preset = args[args.index('--guard') + 1]
        except IndexError:
            pass
    ok, why, rounds = guard(preset, device)
    print(f'{"OK " if ok else "BAD"}  {why} (after {rounds} rounds)')
    return 0 if ok else 1




# ------------------------------------------------- is the whole day covered


# Every mode the camera runs in across a day, in the order they happen.
ALL_PRESETS = ['golden_am', 'day', 'bright_sun', 'golden_pm', 'night',
               'night_mono']


def _enhance_module():
    try:
        import webcam_enhance                                 # noqa: PLC0415
    except ImportError:
        return None
    return webcam_enhance


def would_correct():
    """Which modes the switches currently correct, and why.

    This is the question underneath "is the whole day covered": not whether
    corrected frames happen to exist on disk, but whether the settings as
    they stand would correct a frame arriving now, in each mode.
    """
    enhance = _enhance_module()
    if enhance is None:
        return {}, 'webcam_enhance.py is not importable'
    settings = enhance.read_settings()
    if settings.get(enhance.KEY_ALL_DAY):
        return ({name: True for name in ALL_PRESETS},
                '24 hour correction is on - every mode is corrected')
    if settings.get(enhance.KEY_NIGHT):
        covered = {name: name.startswith('night') for name in ALL_PRESETS}
        return (covered, 'only the night switch is on - the daylight modes '
                         'are left uncorrected')
    return ({name: False for name in ALL_PRESETS},
            'both correction switches are off - nothing is corrected')


def _hour_counts(folder, enhanced_name='enhanced'):
    """Stills per hour of the day, and corrected copies per hour.

    Counted by hour of day rather than by file, because the corrected copy is
    named for when it was written and not for its source, so there is no
    one-to-one pairing to be had. An hour with stills and no corrected copies
    is the gap worth seeing, and that this does show.
    """
    stills = [0] * 24
    done = [0] * 24
    if not folder or not os.path.isdir(folder):
        return stills, done
    for root, _dirs, files in os.walk(folder):
        corrected = (os.sep + enhanced_name) in root + os.sep
        if os.sep + 'compare' in root + os.sep or os.sep + 'thumbs' in root:
            continue
        for name in files:
            if not name.lower().endswith(('.jpg', '.jpeg')):
                continue
            try:
                hour = time.localtime(
                    os.path.getmtime(os.path.join(root, name))).tm_hour
            except OSError:
                continue
            (done if corrected else stills)[hour] += 1
    return stills, done


def coverage(folder=None):
    """The whole answer: what the switches cover, what has actually been
    corrected per mode, and which hours of the day have stills with nothing
    corrected in them."""
    covered, why = would_correct()

    harvested = {}
    try:
        import train_webcam_brain                             # noqa: PLC0415
        for frame in train_webcam_brain.read_index().get('frames', []):
            name = frame.get('preset') or 'unknown'
            harvested[name] = harvested.get(name, 0) + 1
    except Exception:                                         # noqa: BLE001
        harvested = {}

    if folder is None:
        path, _when = newest_snapshot()
        folder = os.path.dirname(path) if path else ''
    stills, done = _hour_counts(folder)

    gaps = [hour for hour in range(24) if stills[hour] and not done[hour]]
    return {'covered': covered, 'why': why, 'harvested': harvested,
            'stills': stills, 'done': done, 'gaps': gaps, 'folder': folder}


def print_coverage():
    report = coverage()
    print(report['why'])
    print()
    print('  mode          corrected   frames harvested')
    for name in ALL_PRESETS:
        mark = 'yes' if report['covered'].get(name) else 'NO '
        print(f"  {name:<14}{mark:<12}{report['harvested'].get(name, 0)}")
    missing = [n for n in ALL_PRESETS if n not in report['harvested']]
    if missing:
        print(f"\n  no frames harvested yet from: {', '.join(missing)}")
        print('  (a mode that has not come round yet is not a fault - '
              'bright sun and golden PM only happen in their own hour)')

    print(f"\n  stills by hour, in {report['folder'] or '(no folder found)'}")
    rows = [(h, report['stills'][h], report['done'][h]) for h in range(24)
            if report['stills'][h] or report['done'][h]]
    if not rows:
        print('    nothing on disk yet')
    for hour, raw, fixed in rows:
        flag = '  <- stills but nothing corrected' if raw and not fixed else ''
        print(f'    {hour:02d}:00  {raw:5d} stills  {fixed:5d} corrected{flag}')
    if report['gaps']:
        print(f"\n  hours with stills and no corrected copy: "
              f"{', '.join(f'{h:02d}:00' for h in report['gaps'])}")
    else:
        print('\n  every hour that has stills has corrected copies too')
    return 0 if not report['gaps'] else 1


if __name__ == '__main__':
    sys.exit(main())
