#!/usr/bin/env python3
"""Night image enhancement: frame averaging, CLAHE, and an ML auto correct.

This is the part of the picture that no camera control can reach.

The presets and the light meter work the camera itself - gain, exposure,
brightness - and that is the right place to start, because a control change
adds real signal to the sensor's own output. But a small sensor behind a slow
lens at 55 to 75 feet in the dark runs out of photons long before it runs out
of gain, and past that point every further step of gain amplifies noise and
nothing else. What is left to do has to be done to the picture after the fact.

Three things are done here, in this order, because each one makes the next one
work better:

  1. Frame averaging. The snapshots in the folder are consecutive stills of
     the same scene, so averaging the newest few cancels sensor noise while
     leaving the scene itself alone - noise falls as the square root of the
     number of frames, so eight frames is about two and a half times less
     noise, which at high gain is the single largest improvement available.
     Anything that moved during the stack blurs, which is the price, and for a
     parked vehicle it costs nothing.

  2. CLAHE - contrast-limited adaptive histogram equalization. Ordinary
     contrast stretching works on the whole frame at once, so a security light
     in one corner sets the scale for everything and the dark half stays dark.
     CLAHE works on small tiles and limits how far each one may stretch, which
     pulls detail out of the shadows without blowing the bright parts and
     without the blotching that uncontrolled local equalization gives.

  3. Brain G, the night vision brain. It was trained on clear photographs:
     for each one the trainer made a dark, noisy copy with a camera model and
     had the brain learn how much every pixel has to be lifted to get back to
     the clear original. So what it predicts, per pixel, is a gain and a
     colour correction - which means a car in deep shadow can be lifted hard
     while the security light above it is left alone, something no single
     exposure setting can do. It has a frozen hand-weighted foundation of nine
     named detectors with a small trained head on top, the same shape as the
     trading robot's brains; train_webcam_brain.py is the whole of it.

     With no brain on disk, or a stale one, an analytic correct runs instead
     and the status line says so.

What this does NOT do, and cannot: it does not change what the recording
software records. The recorder holds the camera and owns the video; these are
enhanced copies of the stills, written to an `enhanced` folder beside them.
That is the honest limit of working from snapshots. The presets and the light
meter remain the only things that affect the recording itself.

And the brain in particular is making an informed guess. It was trained to
produce what a clear photograph of this scene would have looked like, so it
will confidently fill in what it expects to be there. It is a good way to see
whether a vehicle is present and roughly what it is doing. It is not evidence:
a number plate the sensor never captured can come back looking sharp and being
wrong. The frame averaging in step 1 is the only one of the three that adds
real information, and it is the one to trust.

Both switches live in one small file, ~/.webcam-enhance.conf, so that the
window and the scheduler agree about them - tick the box in the window and the
scheduler running under cron picks it up on its next pass.

WHICH LIBRARY DOES WHAT

OpenCV does the foundational processing: decoding the stills, averaging the
stack, the LAB conversion and the CLAHE, the box blurs behind the twelve
feature planes, the resizing, the denoise. Fixed, rule-based, fast, and none
of it learnt. TensorFlow does only the part that is learnt - running brain G
over those features. Brain G therefore needs both, since the features are
OpenCV's work; with TensorFlow but no OpenCV the analytic correct runs
instead of half a brain. ImageMagick is the fallback for the OpenCV half on a
machine without it, and is weaker on exactly the frames that need it most.

    sudo apt-get -y install imagemagick
    python3 -m pip install opencv-python numpy      # the foundational half
    python3 -m pip install tensorflow-cpu           # the learnt half, brain G
    python3 webcam_enhance.py --test                # correct the newest snapshot
"""

import glob
import json
import os
import re
import shutil
import subprocess
import time

# ---- the two switches ----
#
# A file rather than a variable, because two separate programs have to agree
# about them: the window is where they get ticked, and the scheduler, which
# may be running under cron with no window anywhere, is what is usually doing
# the measuring at three in the morning.
CONF_FILE = os.environ.get(
    'WEBCAM_ENHANCE_CONF',
    os.path.join(os.path.expanduser('~'), '.webcam-enhance.conf'))

KEY_NIGHT = 'night_clahe_ml'
KEY_ALL_DAY = 'all_day_clahe_ml'
# Find the car or the person and aim a second, stronger correction at them.
# Independent of the two above: it only does anything on a frame that is being
# corrected at all, because the detector needs the correction to see.
KEY_DETECT = 'detect_and_boost'

# Optional, off by default: as well as the stills the recorder writes, open
# the camera every so often and correct a frame straight off the stream. The
# stills are the default source and cost nothing, because something else is
# writing them anyway; this goes to the device, which is usually held by the
# recorder, so it is opt-in and gives up quietly when the camera is busy.
KEY_SAMPLE_STREAM = 'sample_the_stream'
KEY_SAMPLE_SECONDS = 'sample_seconds'
DEFAULT_SAMPLE_SECONDS = 300

# Where the brain's training frames come from. Two switches, because the two
# sources are not the same kind of thing and a camera that is held by the
# recorder can only offer the first of them.
#
#   train_from_pictures - train on the snapshot folder, which is what the light
#       meter has been filling all along: real frames of the real scene in the
#       real light, which is the whole reason this brain beats a general one.
#       This is the one to leave on.
#
#   train_from_camera - grab a short burst off the camera itself and train on
#       that. Only possible while the camera is free: a V4L2 device lets one
#       program stream at a time, so when the recording software holds it this
#       finds nothing and says who has it rather than failing quietly.
KEY_TRAIN_PICTURES = 'train_from_pictures'
KEY_TRAIN_CAMERA = 'train_from_camera'

# The burst taken off the camera when training direct from it: how many frames
# and how far apart. Spread over a few seconds rather than grabbed back to
# back, because twelve frames of one instant are one frame as far as training
# is concerned.
BURST_FRAMES = int(os.environ.get('WEBCAM_BURST_FRAMES', 12))
BURST_SPACING = 0.75

# How many consecutive stills to average, and how old the oldest of them may
# be. The window matters: stills from twenty minutes apart are two different
# scenes and averaging them smears one over the other. Three minutes at the
# scheduler's usual snapshot rate is a handful of frames of the same night.
STACK_FRAMES = int(os.environ.get('WEBCAM_STACK', 8))
STACK_WINDOW = 180

# Enhanced copies kept before the oldest are deleted, matching the 30 the
# light meter keeps of the originals.
ENH_KEEP = 30
ENH_DIR_NAME = 'enhanced'

# Where the last set of comparison frames was written. One line, one path,
# rewritten every pass - see save_comparison for why this exists.
COMPARE_POINTER = os.environ.get(
    'WEBCAM_COMPARE_POINTER',
    os.path.join(os.path.expanduser('~'), '.webcam-compare-dir'))


# What counts as night when there is no gain reading to judge by.
#
# Gain is the better test and is what the light meter already uses - a camera
# near the top of its gain range is working in the dark whatever the clock
# says. This is the fallback for when the gain could not be read: the average
# grey level of the frame itself, out of 255.
NIGHT_MEAN = 90

# The gain fraction above which this counts as night, when gain IS readable.
# The same knee the light meter uses for easing off sharpness, so that the two
# agree about when night has started.
NIGHT_GAIN_KNEE = 0.50

# CLAHE strength. The clip limit is how far one tile may stretch its own
# contrast; past about 4 the noise starts coming up as fast as the detail.
CLAHE_CLIP = float(os.environ.get('WEBCAM_CLAHE_CLIP', 2.5))
CLAHE_TILES = 8

# Brain G, the night vision brain: a frozen hand-weighted foundation with a
# small trained head on top, built and trained by train_webcam_brain.py and
# shipped with these scripts. See that file for what it is and how to retrain
# it on your own daytime stills, which is the version worth having.
#
# It is loaded read-only and never written to. Nothing is downloaded.
BRAIN_SMOOTH = 9        # box blur on the gain map, in pixels

# The brain predicts on a half size copy and the result is scaled back up.
# A gain map is smooth by nature - it says "this region needs lifting", not
# "this pixel" - so half resolution costs nothing visible and quarters the
# work. It also smooths the map, which is wanted anyway: a gain that varied
# pixel by pixel would amplify the very noise this is trying to see past.
BRAIN_SCALE = 0.5


def _detector():
    """The object detector, loaded once, or None.

    Kept behind a function for the same reason the brain is: it is an extra on
    top of a correction that has to keep working whether or not it is there.
    """
    if _detector.found is None:
        _detector.found = False
        try:
            import webcam_detect                              # noqa: PLC0415
        except ImportError:
            return None
        detector = webcam_detect.Detector()
        if detector.ready():
            _detector.found = detector
    return _detector.found or None


_detector.found = None


def _brain_module():
    """train_webcam_brain, which owns the feature definition, the foundation
    and the architecture. Importing it does NOT import TensorFlow - that
    happens only when a model is actually loaded."""
    if _brain_module.module is None:
        try:
            import train_webcam_brain                        # noqa: PLC0415
        except ImportError:
            _brain_module.module = False
        else:
            _brain_module.module = train_webcam_brain
    return _brain_module.module or None


_brain_module.module = None


# Sentinel: _brain(None) means "no preset recorded", which is a real answer,
# so it cannot double as "work it out yourself".
_UNSET = object()


def active_family():
    """The one brain that is active right now, or None.

    Read fresh on every call, from the file the preset engine writes at the
    moment it applies a preset. That is what makes the switch instant: applying
    night_mono makes the night brain the active one on the very next frame, and
    applying bright_sun makes the bright_sun brain the active one - each preset
    maps to exactly one brain and no other brain is consulted.

    Six presets, five brains: night and night_mono are the same light with and
    without colour and share one brain; the other four have one each. See
    BRAIN_PRESETS in train_webcam_brain.py.
    """
    module = _brain_module()
    if not module:
        return None
    if hasattr(module, 'active_brain'):
        try:
            family, _preset, _when = module.active_brain()
            return family
        except Exception:                                     # noqa: BLE001
            pass
    if not hasattr(module, 'brain_family'):
        return None
    try:
        return module.brain_family()
    except Exception:                                         # noqa: BLE001
        return None


def activate_brain(family):
    """Make this the only brain in memory, dropping every other one.

    A preset change does not merely stop using the other brains, it unloads
    them: the models are dropped from the cache and, when one has to be loaded
    fresh afterwards, TensorFlow's graph is cleared with it. So after a switch
    there is exactly one brain resident - the one for the preset that is on -
    and the memory the others held goes back.

    DISCONNECTED, NOT DELETED. Nothing on disk is touched here: every other
    brain stays trained and in its folder, and comes back the moment its own
    preset does. The only thing that deletes a brain is the delete-and-retrain
    button, and that one takes only the brain for the preset in force.

    Returns True when something was actually dropped.
    """
    stale = [key for key in _brain.cache if key != family]
    if not stale:
        return False
    for key in stale:
        _brain.cache.pop(key, None)
        _brain.reasons.pop(key, None)
    # Only safe when the brain now wanted is not already loaded: clearing the
    # session would invalidate it along with the ones being dropped.
    if family not in _brain.cache:
        try:
            import sys as _sys                                # noqa: PLC0415
            keras = getattr(_sys.modules.get('tensorflow'), 'keras', None)
            if keras is not None:
                keras.backend.clear_session()
        except Exception:                                     # noqa: BLE001
            pass
    return True


def brain_mandate(family=None, meta=None):
    """What the brain in force is allowed to do.

    The mandate recorded in the model's own meta.json wins, so a brain always
    decodes against the range it was trained against even if the table in
    train_webcam_brain.py changes afterwards. Failing that, the table; failing
    that, the all-day default. Never returns None and never returns a range
    that is the wrong way round.
    """
    module = _brain_module()
    mandate = {}
    if module is not None:
        try:
            mandate = module.brain_mandate(family)
        except Exception:                                     # noqa: BLE001
            mandate = {}
    if not mandate:
        mandate = {'gain_min': 1.0, 'gain_max': 12.0, 'sat_min': 0.25,
                   'sat_max': 4.0, 'dim_lift': 0.0, 'clahe_scale': 1.0,
                   'denoise_scale': 1.0, 'sharpen': 0.0,
                   'tint': (1.0, 1.0, 1.0), 'note': 'all-day default'}
    recorded = (meta or {}).get('mandate')
    if isinstance(recorded, dict):
        mandate = dict(mandate, **recorded)
    mandate['gain_min'] = max(0.05, float(mandate.get('gain_min', 1.0)))
    mandate['gain_max'] = max(mandate['gain_min'] * 1.0001,
                              float(mandate.get('gain_max', 12.0)))
    mandate['sat_min'] = max(0.01, float(mandate.get('sat_min', 0.25)))
    mandate['sat_max'] = max(mandate['sat_min'] * 1.0001,
                             float(mandate.get('sat_max', 4.0)))
    tint = mandate.get('tint') or (1.0, 1.0, 1.0)
    try:
        mandate['tint'] = tuple(float(v) for v in tint)[:3]
    except (TypeError, ValueError):
        mandate['tint'] = (1.0, 1.0, 1.0)
    if len(mandate['tint']) != 3:
        mandate['tint'] = (1.0, 1.0, 1.0)
    return mandate


def brain_dirs(family=None):
    """Where to look for the trained brain, in order.

    The environment first, then the folder the trainer writes to for THIS
    preset's brain, then a copy of that brain shipped beside this file.

    What is deliberately not in the list, once a family is known, is the old
    all-day brain: that model was trained on bright and dark frames together
    and learnt to pull the gain below one on frames that were already bright,
    which is what was darkening the picture in daylight. When a preset's own
    brain is missing it is better to fall back to the analytic lift than to
    run a brain trained on the wrong kind of light. The all-day folder is
    still read when no preset has been recorded at all, so an install that
    has not applied a preset yet is no worse off than before.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.join(os.path.expanduser('~'), '.webcam-models')
    found = []
    if os.environ.get('WEBCAM_BRAIN_DIR'):
        found.append(os.environ['WEBCAM_BRAIN_DIR'])
    if family:
        found.append(os.path.join(root, f'brain_{family}'))
        found.append(os.path.join(here, f'brain_{family}'))
        return found

    # No family. Two different situations, and only one of them may read the
    # old all-day brain:
    #
    #   nothing applied yet - no preset has been recorded at all, so there is
    #       nothing to match against and the old folder is no worse than it
    #       ever was.
    #   a preset with no brain of its own - cloudy, defaults, anything added
    #       later. Here the old brain is exactly the wrong answer: it was
    #       trained across mixed light and running it on light nobody trained
    #       for is the original bug. The analytic correct runs instead.
    module = _brain_module()
    recorded = ''
    if module is not None and hasattr(module, 'active_brain'):
        try:
            _family, recorded, _when = module.active_brain()
        except Exception:                                     # noqa: BLE001
            recorded = ''
    if recorded:
        return found
    found.append(os.path.join(root, 'brain_g_night_model'))
    found.append(os.path.join(here, 'brain_g_night_model'))
    return found


def _brain(family=_UNSET):
    """The brain for the preset in force, loaded once per family, or None.

    Three things are checked before it is used, and any of them failing means
    the analytic correct runs instead of a brain that cannot be trusted:

      * the feature count in meta.json has to match what this code builds,
        which is the stale-model check - a model trained against a different
        feature list would be reading its inputs in the wrong order
      * the sha256 in meta.json has to match the model file, so a half
        written or edited model is not run
      * it loads at all

    A fourth thing is checked when the brain came from a per-preset folder:
    the family recorded in meta.json has to be the family being asked for, so
    a night brain cannot be loaded for a daylight frame by a stale copy or a
    hand-moved folder.

    Models are cached per family, so a preset change picks up that preset's
    own brain on the next frame without reloading the others.
    """
    if family is _UNSET:
        family = active_family()
    # The preset changed since the last frame: the brain for the new preset
    # becomes the active one and every other brain is dropped here, before
    # anything is loaded or used.
    if family != _brain.family:
        activate_brain(family)
    if family in _brain.cache:
        _brain.family = family
        _brain.why = _brain.reasons.get(family, '')
        return _brain.cache[family] or None
    _brain.cache[family] = False
    _brain.family = family
    _brain.why = ('no trained brain found'
                  + (f' for the {family} preset' if family else ''))
    if not family and not brain_dirs(family):
        _brain.why = ('this preset has no brain of its own - the analytic '
                      'correct is doing the work')
    _brain.reasons[family] = _brain.why

    module = _brain_module()
    if not module:
        _brain.why = 'train_webcam_brain.py is not next to this file'
        _brain.reasons[family] = _brain.why
        return None

    for folder in brain_dirs(family):
        model_path = os.path.join(folder, 'model.keras')
        meta_path = os.path.join(folder, 'meta.json')
        if not os.path.isfile(model_path):
            continue
        meta = {}
        try:
            with open(meta_path, encoding='utf-8') as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            meta = {}

        # The brain in a per-preset folder has to say it is that preset's.
        # A model with no family recorded is an older all-day brain; it is
        # accepted only when no family is being asked for.
        if family:
            recorded_family = meta.get('brain_family')
            if recorded_family and recorded_family != family:
                _brain.why = (f'the brain in {folder} was trained for '
                              f'{recorded_family}, not {family}')
                _brain.reasons[family] = _brain.why
                continue

        wanted = module.BRAIN_G_FEATURES
        if meta.get('feature_count') not in (None, wanted):
            _brain.why = (f'the brain in {folder} is stale '
                          f'({meta["feature_count"]} features against {wanted}) '
                          '- retrain it with train_webcam_brain.py --force')
            _brain.reasons[family] = _brain.why
            continue

        recorded = meta.get('model_sha256')
        if recorded:
            try:
                actual = module.sha256_of(model_path)
            except OSError:
                actual = ''
            if actual and actual != recorded:
                _brain.why = f'the brain in {folder} does not match its sha256'
                _brain.reasons[family] = _brain.why
                continue

        try:
            os.environ.setdefault('TF_CPP_MIN_LOG_LEVEL', '2')
            from tensorflow import keras                     # noqa: PLC0415
            # Cap the VRAM and prove the card can run a convolution before
            # a brain is put on it. Hides the GPU and carries on if not.
            try:
                import webcam_gpu                            # noqa: PLC0415
                webcam_gpu.configure()
            except ImportError:
                pass
            model = keras.models.load_model(model_path, compile=False)
        except Exception as problem:                          # noqa: BLE001
            _brain.why = f'could not load the brain: {problem}'
            _brain.reasons[family] = _brain.why
            _oops('brain G.load', problem,
                  detail=f'from {folder} - TensorFlow may be the problem')
            continue

        _brain.cache[family] = (model, meta, folder)
        _brain.why = ''
        _brain.reasons[family] = ''
        return _brain.cache[family]
    return None


# One entry per family, so a preset change loads that preset's brain and
# leaves the others where they are. None is the key for "no preset recorded".
_brain.cache = {}
_brain.reasons = {}
_brain.family = None
_brain.why = ''


def clear_brain_cache():
    """Forget every loaded brain, so the next frame reloads from disk.

    Called by the window after a retrain, and worth calling after anything
    that writes a model, since a brain is otherwise held for the life of the
    process.
    """
    _brain.cache = {}
    _brain.reasons = {}
    _brain.family = None
    _brain.why = ''


def read_settings():
    """The switches, as they stand on disk, with their defaults underneath.

    The 24 hour correction starts **on**. Every preset across the day -
    night, night mono, golden AM, day, bright sun, golden PM - produces
    stills, and all of them are worth correcting: the daylight ones are not
    dark but they are flat, and the same stack-CLAHE-brain G chain is what
    the detector then has to work with. Correcting only at night left two
    thirds of the day untouched and made the corpus lopsided, because the
    brain only ever saw the frames it had already been used on.

    Object detection starts on with it, since boosting a car or a person is
    the point of correcting the frame at all, and it is wanted in daylight
    too and not only in the dark.

    Stream sampling starts **off**, deliberately. The stills are the default
    source and they cost nothing: the recorder is writing them anyway. Going
    to the camera directly means opening a device that something else is
    usually holding, so it stays opt-in.

    A conf file written before any of these keys existed falls back to these
    same defaults for the keys it does not mention, so an old file means
    "corrected" rather than silently meaning "never corrected".
    """
    settings = {KEY_NIGHT: False, KEY_ALL_DAY: True, KEY_DETECT: True,
                KEY_TRAIN_PICTURES: True, KEY_TRAIN_CAMERA: False,
                KEY_SAMPLE_STREAM: False,
                KEY_SAMPLE_SECONDS: DEFAULT_SAMPLE_SECONDS}
    try:
        with open(CONF_FILE, encoding='utf-8') as handle:
            for line in handle:
                if '=' not in line or line.lstrip().startswith('#'):
                    continue
                key, _, value = line.partition('=')
                key = key.strip()
                if key == KEY_SAMPLE_SECONDS:
                    # The one setting here that is a number rather than a
                    # switch. A bad value falls back to the default instead
                    # of stopping the correction over a typo.
                    try:
                        settings[key] = max(30, int(value.strip()))
                    except ValueError:
                        pass
                elif key in settings:
                    settings[key] = value.strip() in ('1', 'true', 'True', 'yes', 'on')
    except OSError:
        pass
    return settings


def write_settings(night=None, all_day=None, detect=None,
                   train_pictures=None, train_camera=None,
                   sample_stream=None, sample_seconds=None):
    """Change any of the switches, leaving the others as they were."""
    settings = read_settings()
    if night is not None:
        settings[KEY_NIGHT] = bool(night)
    if all_day is not None:
        settings[KEY_ALL_DAY] = bool(all_day)
    if detect is not None:
        settings[KEY_DETECT] = bool(detect)
    if train_pictures is not None:
        settings[KEY_TRAIN_PICTURES] = bool(train_pictures)
    if train_camera is not None:
        settings[KEY_TRAIN_CAMERA] = bool(train_camera)
    if sample_stream is not None:
        settings[KEY_SAMPLE_STREAM] = bool(sample_stream)
    if sample_seconds is not None:
        # Floored rather than rejected: a one-second interval would mean
        # fighting the recorder for the camera continuously.
        try:
            settings[KEY_SAMPLE_SECONDS] = max(30, int(sample_seconds))
        except (TypeError, ValueError):
            pass
    try:
        with open(CONF_FILE, 'w', encoding='utf-8') as handle:
            handle.write('# Written by the webcam settings window. Two switches:\n')
            handle.write('#   night image CLAHE and ML auto correct\n')
            handle.write('#   24 hour image CLAHE and ML auto correct\n')
            handle.write('#   find cars and people and enhance them\n')
            handle.write('# And where the brain trains from:\n')
            handle.write('#   train the brain from the picture folder\n')
            handle.write('#   train the brain direct from the detected webcamera\n')
            handle.write('# Optionally, as well as the stills:\n')
            handle.write('#   sample a frame off the camera stream, '
                         'and how often in seconds\n')
            for key, value in settings.items():
                if key == KEY_SAMPLE_SECONDS:
                    handle.write(f'{key}={int(value)}\n')
                else:
                    handle.write(f'{key}={1 if value else 0}\n')
    except OSError:
        return False
    return True


# ---- training direct from the camera ----
#
# The Perl side of these two repositories has the same three functions, writing
# the same conf file and the same burst folder, so a box ticked in either
# window means the same thing to both.

def camera_training_dir():
    """Where a burst off the camera is put.

    Its own folder, not the picture folder: the light meter measures the newest
    still in there and a burst of twelve would have it measuring frames nobody
    asked for.
    """
    folder = os.environ.get(
        'WEBCAM_CAMERA_TRAINING_DIR',
        os.path.join(os.path.expanduser('~'), 'Pictures',
                     'webcam-camera-training'))
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        return None
    return folder


def camera_holder(device):
    """Who holds the camera, if anyone, as a string - or None.

    This is the whole difficulty with training direct from the camera. A V4L2
    device lets one program stream at a time, and on a machine that is
    recording the recorder has it. Worse than failing would be succeeding at
    the wrong moment: if the recorder drops and re-opens its stream just as a
    grab lands, the grab wins and the recorder does not come back. So when
    anything else holds the device, nothing is sent to it.
    """
    if not device or not os.path.exists(device):
        return None
    mine = os.getpid()

    if shutil.which('fuser'):
        try:
            done = subprocess.run(['fuser', '-v', device], capture_output=True,
                                  text=True, timeout=5)
            names = set()
            for line in (done.stdout + done.stderr).splitlines():
                parts = line.split()
                # fuser -v lines look like:  USER  PID  ACCESS  COMMAND
                if len(parts) < 3:
                    continue
                for index, token in enumerate(parts):
                    if token.isdigit() and index + 2 < len(parts) + 1:
                        pid = int(token)
                        if pid == mine:
                            break
                        names.add(f'{parts[-1]} (pid {pid})')
                        break
            if names:
                return ', '.join(sorted(names))
        except (OSError, subprocess.SubprocessError):
            pass

    if shutil.which('lsof'):
        try:
            done = subprocess.run(['lsof', '-F', 'cp', device],
                                  capture_output=True, text=True, timeout=5)
            pid, names = None, set()
            for line in done.stdout.splitlines():
                if line.startswith('p') and line[1:].isdigit():
                    pid = int(line[1:])
                elif line.startswith('c') and pid is not None and pid != mine:
                    names.add(f'{line[1:]} (pid {pid})')
            if names:
                return ', '.join(sorted(names))
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def capture_training_burst(device='/dev/video0', frames=None):
    """Grab a short burst off the camera. Returns (count, folder, note)."""
    frames = BURST_FRAMES if not frames or frames < 1 else frames

    if not os.path.exists(device):
        return 0, None, f'{device} does not exist - is the camera plugged in?'
    if not shutil.which('ffmpeg'):
        return 0, None, 'ffmpeg is not installed, so no frames can be grabbed'

    holder = camera_holder(device)
    if holder:
        return 0, None, (f'{holder} has {device}, so the camera cannot be '
                         'grabbed from while that is running - training from '
                         'the picture folder instead is the way round it')

    folder = camera_training_dir()
    if folder is None:
        return 0, None, 'nowhere to put the frames'

    # Everything from a previous burst goes first. A burst is a set of frames
    # of one scene in one light, and mixing tonight's with last week's is how a
    # training folder quietly becomes a folder of two different places.
    for stale in glob.glob(os.path.join(folder, '*.jpg')):
        try:
            os.remove(stale)
        except OSError:
            pass

    stamp = time.strftime('%Y%m%d-%H%M%S')
    got = 0
    for number in range(1, frames + 1):
        path = os.path.join(folder, f'camera-{stamp}-{number:02d}.jpg')
        try:
            done = subprocess.run(
                ['ffmpeg', '-nostdin', '-loglevel', 'error', '-f', 'v4l2',
                 '-i', device, '-frames:v', '1', '-q:v', '2', '-y', path],
                capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            done = None
        wrote = (done is not None and done.returncode == 0
                 and os.path.exists(path) and os.path.getsize(path) > 0)
        if wrote:
            got += 1
        else:
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
            # The first frame failing is the camera refusing; the fifth failing
            # is a hiccup. Only the first is worth stopping for.
            if number == 1:
                break
        if number < frames:
            time.sleep(BURST_SPACING)

    if not got:
        return 0, folder, (f'ffmpeg could not grab a frame from {device} - '
                           'another program may have taken it, or this user '
                           'may not be in the video group')
    return got, folder, f'grabbed {got} frames from {device}'


def training_sources(device='/dev/video0', folder=None):
    """Which sources the switches have selected, as [(label, folder)].

    The camera one grabs its burst here, so a caller gets back only folders
    that actually have frames in them. Returns (sources, note).
    """
    settings = read_settings()
    sources, notes = [], []

    if settings.get(KEY_TRAIN_CAMERA):
        got, burst, note = capture_training_burst(device)
        notes.append(note)
        if got and burst:
            sources.append(('the camera', burst))
    if settings.get(KEY_TRAIN_PICTURES):
        if folder and os.path.isdir(folder):
            sources.append(('the picture folder', folder))
        else:
            notes.append('no picture folder to train from')
    if not settings.get(KEY_TRAIN_CAMERA) and not settings.get(KEY_TRAIN_PICTURES):
        notes.append('both training switches are off, so the brain is not '
                     'being trained')
    return sources, '; '.join(n for n in notes if n)


def _run(args):
    """A command, with its noise swallowed. Nothing in here is worth taking
    the scheduler down for."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


def _magick():
    """`magick` on ImageMagick 7, `convert` on 6, empty when neither."""
    if _magick.found is None:
        _magick.found = shutil.which('magick') or shutil.which('convert') or ''
    return _magick.found


_magick.found = None


def _magick_has_clahe():
    """Whether this ImageMagick knows -clahe, which arrived in version 7.

    On ImageMagick 6 the operator does not exist and the command fails, so the
    fallback is a limited contrast stretch - less good, and still better than
    nothing."""
    if _magick_has_clahe.answer is None:
        command = _magick()
        text = _run([command, '-version']) if command else ''
        _magick_has_clahe.answer = bool(re.search(r'ImageMagick 7', text))
    return _magick_has_clahe.answer


_magick_has_clahe.answer = None


# Every handler in here says what went wrong through this, so an OpenCV,
# TensorFlow or brain G failure shows up in the window's terminal pane rather
# than only as a frame that quietly did not get corrected.
try:
    import webcam_errors
except ImportError:                                               # noqa: BLE001
    webcam_errors = None


def _oops(where, problem=None, detail='', once_key=''):
    """Report a failure and carry on. A no-op if webcam_errors.py was
    deleted, because the correction working matters more than the log."""
    if webcam_errors is None:
        return
    if once_key:
        webcam_errors.once(once_key, where, detail or str(problem))
    else:
        webcam_errors.report(where, problem, detail=detail)


# The extra lift the locked-on target gets, over and above the ordinary
# detector boost every car and person gets. Modest on purpose: this is the one
# object the picture is being kept for, so it is worth lifting harder than the
# rest of the frame, and it is still a photograph of a car rather than a white
# silhouette of one.
LOCK_BOOST_GAIN = 1.35

try:
    import webcam_lock
except ImportError:
    webcam_lock = None       # no lock module: every box treated alike


def _cv2():
    """OpenCV, if it is installed. Everything here works without it, through
    ImageMagick; with it, the work is done in one process on arrays rather
    than by spawning a command per step, and the CLAHE is the real thing
    rather than an approximation of it."""
    if _cv2.module is None:
        try:
            import cv2                                        # noqa: PLC0415
            import numpy                                      # noqa: PLC0415
        except ImportError as problem:
            _cv2.module = False
            _oops('OpenCV.import', detail=(
                f'OpenCV is not usable ({problem}) - correcting with '
                'ImageMagick instead, which has no brain G and no detector. '
                'conda install -c conda-forge opencv'),
                once_key='no-cv2')
        else:
            _cv2.module = (cv2, numpy)
    return _cv2.module or None


_cv2.module = None


class Enhancer:
    """Frame averaging, CLAHE and an ML auto correct over the snapshot folder.

    Nothing here touches the camera device, so it is safe to run while the
    recording software holds it - that is the whole reason this works from
    stills rather than from the stream.
    """

    def __init__(self, folder=None):
        self.folder = folder
        self.note = ''
        self.detections = []
        self.last_output = ''
        self.tiers = ''

    # ---- when to run ----

    def active(self, gain_fraction=None, mean=None):
        """Whether to enhance this pass, and why.

        Returns (True/False, reason). The 24 hour switch wins outright. The
        night switch asks the gain how dark it is, and falls back to the
        picture's own average when the gain could not be read.
        """
        settings = read_settings()
        if settings[KEY_ALL_DAY]:
            return True, '24 hour'
        if not settings[KEY_NIGHT]:
            return False, 'off'
        if gain_fraction is not None:
            if gain_fraction > NIGHT_GAIN_KNEE:
                return True, f'night, gain at {gain_fraction * 100:.0f}%'
            return False, f'daylight, gain at {gain_fraction * 100:.0f}%'
        if mean is not None:
            if mean < NIGHT_MEAN:
                return True, f'night, picture averaging {mean:.0f}'
            return False, f'daylight, picture averaging {mean:.0f}'
        return False, 'cannot tell night from day yet'

    # ---- the folders ----

    def snap_dir(self, frame=None):
        """The folder the originals are in. A single configured file or an
        http URL has no folder of its own, so the frame's own directory is
        used."""
        if self.folder and os.path.isdir(self.folder):
            return self.folder
        if frame and os.path.isfile(frame):
            return os.path.dirname(os.path.abspath(frame))
        return None

    def out_dir(self, frame=None):
        """Where the enhanced copies go: an `enhanced` folder beside the
        originals, made if it is not there.

        A subfolder rather than the same folder, so that the light meter goes
        on measuring the camera's own unenhanced output. Measuring an
        enhanced still would close a loop on itself - the correct lifts the
        picture, the meter sees a brighter picture and winds the gain back
        down, and the camera ends up darker than it started."""
        base = self.snap_dir(frame)
        if not base:
            return None
        out = os.path.join(base, ENH_DIR_NAME)
        try:
            os.makedirs(out, exist_ok=True)
        except OSError:
            return None
        return out

    def stack_frames(self, frame):
        """The newest few stills, newest first, all within the time window.

        The configured frame is always the first of them, so a single file or
        an http snapshot still gets the rest of the treatment with a stack of
        one."""
        folder = self.snap_dir(frame)
        if not folder:
            return [frame] if frame else []
        shots = [p for p in glob.glob(os.path.join(folder, '*.jpg'))
                 + glob.glob(os.path.join(folder, '*.jpeg'))
                 if os.path.isfile(p)]
        if not shots:
            return [frame] if frame else []
        shots.sort(key=os.path.getmtime, reverse=True)
        newest = os.path.getmtime(shots[0])
        picked = [p for p in shots[:STACK_FRAMES]
                  if newest - os.path.getmtime(p) <= STACK_WINDOW]
        return picked or shots[:1]

    def prune(self, folder):
        """Keep the newest ENH_KEEP enhanced copies and delete the rest."""
        if not folder or not os.path.isdir(folder):
            return 0
        shots = glob.glob(os.path.join(folder, '*.jpg'))
        if len(shots) <= ENH_KEEP:
            return 0
        shots.sort(key=os.path.getmtime)
        gone = 0
        for path in shots[:len(shots) - ENH_KEEP]:
            try:
                os.remove(path)
                gone += 1
            except OSError:
                pass
        return gone

    # ---- the work, with OpenCV ----

    def _with_cv2(self, frames, out_path):
        cv2, numpy = _cv2()
        images = []
        shape = None
        for path in frames:
            image = cv2.imread(path, cv2.IMREAD_COLOR)
            if image is None:
                continue
            if shape is None:
                shape = image.shape
            # A camera that changed resolution mid-stack cannot be averaged.
            if image.shape == shape:
                images.append(image.astype(numpy.float32))
        if not images:
            return '', 'nothing readable to enhance'

        # The first readable frame, untouched, as the "before" of the top row.
        # The stack below is already a correction of a kind, so comparing
        # against it would understate what the correction did.
        first_frame = images[0].astype(numpy.uint8)

        # 1. the average
        try:
            stacked = images[0] if len(images) == 1 else \
                numpy.mean(numpy.stack(images, axis=0), axis=0)
            stacked = numpy.clip(stacked, 0, 255).astype(numpy.uint8)
            done = [f'stacked {len(images)}']
        except Exception as problem:                          # noqa: BLE001
            # Nearly always a frame that changed shape mid-stack, which the
            # filter above should have caught. Fall back to the single newest
            # frame rather than giving up on the correction entirely.
            _oops('night correction.stacking', problem,
                  detail=f'could not average {len(images)} frames - '
                         'using the newest one alone')
            stacked = numpy.clip(images[0], 0, 255).astype(numpy.uint8)
            done = ['stack failed, single frame']

        # 2. CLAHE on lightness only, so the colour is left where it is. Run
        # on L of LAB rather than on the three channels separately, which
        # would pull the colour balance about tile by tile.
        # How hard the local contrast is pushed is this brain's business too:
        # a night frame wants more of it than a daylight one, and the day
        # brain is left on the standard clip it always had.
        family = active_family()
        mandate = brain_mandate(family)
        clip = CLAHE_CLIP * float(mandate.get('clahe_scale') or 1.0)
        try:
            lab = cv2.cvtColor(stacked, cv2.COLOR_BGR2LAB)
            light, green_red, blue_yellow = cv2.split(lab)
            clahe = cv2.createCLAHE(clipLimit=clip,
                                    tileGridSize=(CLAHE_TILES, CLAHE_TILES))
            lab = cv2.merge((clahe.apply(light), green_red, blue_yellow))
            result = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
            done.append('CLAHE')
        except Exception as problem:                          # noqa: BLE001
            # The local contrast stage - the one that pulls detail out of the
            # shadows a tile at a time. Without it the frame still gets the
            # lift, it just stays flat, so say so loudly and carry on.
            _oops('night correction.CLAHE', problem,
                  detail=f'clip {clip}, {CLAHE_TILES}x{CLAHE_TILES} '
                         'tiles - frame left without local contrast')
            result = stacked
            done.append('CLAHE failed')

        # 3. the auto correct, by brain G when it is there
        brain = _brain(family)
        named = f'brain {family}' if family else 'brain G'
        if brain is not None:
            lifted = self._brain_lift(brain, result)
            if lifted is not None:
                result = lifted
                done.append(named)
            else:
                result = self._analytic_lift(result)
                done.append('auto correct (the brain would not run, analytic)')
        else:
            result = self._analytic_lift(result)
            done.append('auto correct (analytic)')

        # Denoise last, AFTER the lift rather than before it.
        #
        # This is the right order and it took looking at the output to see why.
        # Lifting a dark frame eighteen-fold lifts its noise with it, and the
        # worst of what comes up is chroma: single pixels that were a shade off
        # neutral in the dark frame become vivid red and green speckle in the
        # lifted one. Denoising before the lift cannot help, because at that
        # point the speckle is still below one count and invisible.
        #
        # The strength follows how hard the frame was lifted - a frame that
        # barely moved needs nothing - and the chroma is cleaned far harder
        # than the luma, because that is where the damage is and because luma
        # is where the detail is. Wiping the luma would undo the sharpening
        # and the CLAHE that came before it.
        lifted_by = float(numpy.mean(result.astype(numpy.float32) + 1.0) /
                          max(float(numpy.mean(stacked.astype(numpy.float32))), 1.0))
        denoise_scale = float(mandate.get('denoise_scale') or 1.0)
        if (lifted_by > 1.6 or len(images) < 4) and denoise_scale > 0:
            # h for luma, hColor for the colour channels, both scaled by what
            # this brain's light actually needs: a night frame lifted twelve
            # times is mostly grain, a sunlit one has almost none.
            luma_strength = max(1, int(round((3 if len(images) >= 4 else 5)
                                             * denoise_scale)))
            chroma_strength = max(1, int(round(min(18, 6 + 4 * lifted_by)
                                               * denoise_scale)))
            try:
                result = cv2.fastNlMeansDenoisingColored(
                    result, None, luma_strength, chroma_strength, 7, 21)
                done.append(f'denoise (chroma {chroma_strength})')
            except Exception as problem:                      # noqa: BLE001
                # Present in the main OpenCV build but not in every slimmed
                # down one, and it is the stage most worth naming when it
                # goes: without it a hard lift keeps its colour speckle.
                _oops('night correction.denoise', problem,
                      detail='frame kept undenoised - expect colour speckle '
                             'on a hard lift', once_key='denoise')
                done.append('denoise unavailable')

        # Put the edges back. Denoising a hard-lifted night frame takes the
        # grain out and some of the detail with it, so the brains whose light
        # needs denoising get an unsharp pass after it - the blurred copy
        # subtracted back off, which is the oldest trick there is and the one
        # that does not invent anything. Day has sharpen 0 and skips this
        # entirely.
        sharpen = float(mandate.get('sharpen') or 0.0)
        if sharpen > 0:
            try:
                blurred = cv2.GaussianBlur(result, (0, 0), 1.6)
                result = cv2.addWeighted(result, 1.0 + sharpen,
                                         blurred, -sharpen, 0)
                done.append(f'sharpened {sharpen:.2f}')
            except Exception as problem:                      # noqa: BLE001
                _oops('night correction.sharpen', problem,
                      detail='frame left unsharpened', once_key='sharpen')

        # 4. find the car or the person, and give them a second, stronger
        # lift of their own.
        #
        # This runs LAST, on the corrected frame, and that order is the whole
        # trick: a detector looking at the raw night frame sees a black
        # rectangle and finds nothing in it, while the same detector on the
        # corrected frame finds what is actually there. Measured on a night
        # frame: nothing at all before the correction, three people and two
        # vehicles after it.
        # The corrected frame as it stands before anything is picked out of
        # it. Kept because it is the "before" of the second comparison: the
        # window shows the correction on the top row and what the detector
        # then did on the row under it, and the two share this frame.
        pre_boost = result.copy()

        self.detections = []
        boxes = []
        detector = None
        if read_settings().get(KEY_DETECT):
            detector = _detector()
            if detector is None:
                done.append('detector not available')
            else:
                boxes = detector.detect(result)
                self.detections = boxes
                if boxes:
                    # The locked-on target first, when one is set and it is in
                    # this frame. It gets a lift of its own on top of the
                    # ordinary boost, because the whole reason for locking on
                    # is that this one object is what the picture is for - and
                    # it is put at the head of the list so that its crop is
                    # the first one written and the one the window shows.
                    if webcam_lock is not None and webcam_lock.locked():
                        try:
                            pick = webcam_lock.preferred_box(boxes, result)
                        except Exception as problem:          # noqa: BLE001
                            pick = None
                            _oops('night correction.lock', problem,
                                  detail='locked target not matched this frame',
                                  once_key='lock')
                        if pick is not None:
                            result = detector.boost(result, [pick],
                                                    gain=LOCK_BOOST_GAIN)
                            boxes = ([pick] +
                                     [b for b in boxes if b is not pick])
                            done.append('locked target given its own lift')
                        else:
                            done.append('locked target not in this frame')
                    result = detector.boost(result, boxes)
                    done.append(f'found and boosted {detector.summary(boxes)}')
                    folder = os.path.dirname(out_path)
                    detector.crops(result, boxes,
                                   os.path.join(folder, 'detections'))
                else:
                    done.append('nothing found to boost')

        cv2.imwrite(out_path, result, [int(cv2.IMWRITE_JPEG_QUALITY), 92])

        # The four frames the window compares, written last so that a failure
        # in here cannot cost us the corrected still itself.
        self.save_comparison(os.path.dirname(out_path), first_frame, pre_boost,
                             result, boxes, detector, ', '.join(done))
        return out_path, ', '.join(done)

    def save_comparison(self, folder, raw, corrected, boosted, boxes,
                        detector, note=''):
        """Write the four frames the tile at the bottom of the window shows.

        Top row is the correction - the frame as the camera gave it, then the
        same frame stacked, CLAHE'd and lifted by brain G. Bottom row is the
        enhancement that runs on top of that - the corrected frame again,
        then the frame with the car or the person found in it and given a
        second lift of their own.

        The boxes are drawn on this last copy and on nothing else. The
        corrected still that gets kept has no boxes on it, deliberately: a
        rectangle drawn across a number plate is worse than no rectangle, and
        this copy exists to be looked at rather than to be evidence.
        """
        if not folder:
            return ''
        cv2, _numpy = _cv2()
        target = os.path.join(folder, 'compare')
        try:
            os.makedirs(target, exist_ok=True)
        except OSError:
            return ''

        drawn = boosted
        if boxes and detector is not None:
            try:
                drawn = detector.draw(boosted, boxes)
            except Exception as problem:                      # noqa: BLE001
                _oops('compare.draw', problem,
                      detail='boxes not drawn on the comparison frame')
                drawn = boosted

        # The bottom row of the tile: the thing that was found, close up,
        # before and after its own lift. The whole-frame rows show that the
        # correction worked; this row is the one that answers whether the car
        # can actually be read, which is the question being asked at all.
        crop_before, crop_after = self._best_crop(corrected, boosted, boxes)

        quality = [int(cv2.IMWRITE_JPEG_QUALITY), 88]
        written = []
        for name, frame in (('raw.jpg', raw),
                            ('corrected.jpg', corrected),
                            ('boost-before.jpg', corrected),
                            ('boost-after.jpg', drawn),
                            ('crop-before.jpg', crop_before),
                            ('crop-after.jpg', crop_after)):
            if frame is None:
                continue
            try:
                if cv2.imwrite(os.path.join(target, name), frame, quality):
                    written.append(name)
            except Exception as problem:                      # noqa: BLE001
                _oops('compare.write', problem, detail=f'{name} not written')
                continue

        # Where these went, written to one fixed place.
        #
        # This is the only reliable way for the window to find them. The
        # enhancer takes its folder from the frame it is given - the light
        # meter constructs Enhancer() with no folder at all and passes the
        # path per frame - so asking a fresh Enhancer() where the output goes
        # returns nothing, and anything else is a guess at a folder the user
        # may well have moved. The producer knows; so the producer writes it
        # down and the window reads it.
        try:
            with open(COMPARE_POINTER, 'w', encoding='utf-8') as fh:
                fh.write(target + '\n')
        except OSError:
            pass

        # A line saying what was done and what was found, so the window has
        # something to print under the pictures without redoing the work.
        try:
            found = 'nothing found'
            if boxes and detector is not None:
                found = detector.summary(boxes)
            when = time.strftime('%Y-%m-%d %H:%M:%S')
            with open(os.path.join(target, 'compare.txt'), 'w',
                      encoding='utf-8') as fh:
                fh.write(f'{when}  {found}\n{note}\n')
        except OSError:
            pass
        return target if written else ''


    def _best_crop(self, corrected, boosted, boxes, margin=0.18):
        """The best thing found, cut out of both frames at the same place.

        The highest scoring box wins, and a margin is left around it because
        the detector's idea of where a car ends is tighter than a person's and
        a crop cut exactly on the box loses the number plate at the bottom
        edge. Both crops come from the same rectangle so the two sit square
        above one another in the window and can be compared by eye.

        Nothing found means nothing to show: the window keeps the last pair
        rather than being given a black rectangle, which looks like a failure
        and is not one.
        """
        if not boxes:
            return None, None
        try:
            best = max(boxes, key=lambda box: box.get('score', 0))
            height, width = corrected.shape[:2]
            pad_x = int(best['w'] * margin)
            pad_y = int(best['h'] * margin)
            x1 = max(0, best['x'] - pad_x)
            y1 = max(0, best['y'] - pad_y)
            x2 = min(width, best['x'] + best['w'] + pad_x)
            y2 = min(height, best['y'] + best['h'] + pad_y)
            if x2 - x1 < 8 or y2 - y1 < 8:
                return None, None
            return corrected[y1:y2, x1:x2].copy(), boosted[y1:y2, x1:x2].copy()
        except Exception as problem:                          # noqa: BLE001
            _oops('night correction', problem,
                  detail='could not cut the close crop for the window')
            return None, None

    def _brain_lift(self, brain, image):
        """Run brain G over the frame and apply what it asks for.

        Two numbers per pixel come back: how much to multiply the brightness
        by, and what to do with the colour afterwards. Both are predicted on a
        half size copy and scaled back up, then blurred - a gain map is meant
        to say "this region needs lifting", and one that varied pixel by pixel
        would amplify the noise instead of seeing past it.

        The luma is multiplied by the gain and the colour that sits around it
        is multiplied by gain times saturation, so a lifted frame keeps its
        colour instead of coming back grey. That is the "colour kept day and
        night, with a little less of it at night" part: the brain was trained
        against clear photographs, so what it asks for is whatever gets this
        frame closest to how the scene would have looked in good light.
        """
        cv2, numpy = _cv2()
        module = _brain_module()
        if not module:
            return None
        model, meta, _folder = brain
        mandate = brain_mandate(active_family(), meta)
        try:
            height, width = image.shape[:2]
            work = image.astype(numpy.float32) / 255.0
            if BRAIN_SCALE < 1.0:
                small = cv2.resize(work, None, fx=BRAIN_SCALE, fy=BRAIN_SCALE,
                                   interpolation=cv2.INTER_AREA)
            else:
                small = work

            planes = module.feature_planes(small)
            rows = planes.reshape(-1, planes.shape[-1])
            # One call on the whole frame. Keras would otherwise rebuild its
            # graph per batch and spend longer on that than on the arithmetic.
            answer = numpy.asarray(model(rows, training=False))

            short, wide = small.shape[:2]
            gain = answer[:, 0].reshape(short, wide)
            saturation = answer[:, 1].reshape(short, wide)
            # Back out of the 0 to 1 the brain works in, into this brain's own
            # range - the same mapping targets_for used on the way in. This is
            # what makes the mandate real rather than advisory: the night,
            # golden and day brains decode into a range that starts at 1.0, so
            # no output of theirs can darken a pixel, and bright_sun decodes
            # into a range that ends at 1.0, so no output of its can brighten
            # one.
            g_low, g_high = mandate['gain_min'], mandate['gain_max']
            s_low, s_high = mandate['sat_min'], mandate['sat_max']
            gain = g_low * numpy.power(g_high / g_low,
                                       numpy.clip(gain, 0.0, 1.0))
            saturation = s_low * numpy.power(s_high / s_low,
                                             numpy.clip(saturation, 0.0, 1.0))

            # The dim-light part of the lift, for the two golden hours and for
            # night. The light at sunrise and sunset moves the whole time, and
            # a brain cannot see the clock - but it can be told to lean on the
            # one number that tracks the light directly, which is how bright
            # the frame already is. A frame at or above mid grey gets nothing
            # extra; the dimmer it is below that, the more it gets, up to half
            # again at black. Day and bright sun have dim_lift 0 and so are
            # untouched by this.
            dim_lift = float(mandate.get('dim_lift') or 0.0)
            if dim_lift > 0:
                level = float(numpy.mean(small)) if small.size else 0.45
                shortfall = max(0.0, min(1.0, (0.45 - level) / 0.45))
                gain = gain * (1.0 + 0.5 * dim_lift * shortfall)
                gain = numpy.clip(gain, g_low, g_high)

            if BRAIN_SMOOTH > 1:
                gain = cv2.blur(gain, (BRAIN_SMOOTH, BRAIN_SMOOTH),
                                borderType=cv2.BORDER_REFLECT)
                saturation = cv2.blur(saturation, (BRAIN_SMOOTH, BRAIN_SMOOTH),
                                      borderType=cv2.BORDER_REFLECT)
            if (short, wide) != (height, width):
                gain = cv2.resize(gain, (width, height),
                                  interpolation=cv2.INTER_LINEAR)
                saturation = cv2.resize(saturation, (width, height),
                                        interpolation=cv2.INTER_LINEAR)

            luma = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(numpy.float32) / 255.0
            luma = luma[..., None]
            gain = gain[..., None]
            saturation = saturation[..., None]
            # The luma takes the gain; the colour around it takes the gain and
            # the saturation together.
            out = luma * gain + (work - luma) * gain * saturation

            # A fixed cast the gain cannot express - bright sun's touch of
            # blue and touch less red. (1, 1, 1) for every other brain, which
            # multiplies out to nothing.
            tint = mandate['tint']
            if tint != (1.0, 1.0, 1.0):
                out = out * numpy.asarray(tint, dtype=numpy.float32)
            return numpy.clip(out * 255.0, 0, 255).astype(numpy.uint8)
        except Exception as problem:                          # noqa: BLE001
            _oops('brain G.predict', problem, detail=(
                'the brain would not run on this frame - falling back to the '
                'analytic correct'))
            return None

    def _analytic_lift(self, image):
        """The correct that needs no model: an adaptive gamma worked out from
        the picture's own average, with the shadows lifted more than the
        highlights.

        This is what Retinex-style enhancement does in its simplest honest
        form. It is weaker than a trained model and it has one real virtue
        over one: it only redistributes light that the sensor actually
        recorded, so it does not invent anything.
        """
        cv2, numpy = _cv2()
        work = image.astype(numpy.float32) / 255.0
        grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(numpy.float32) / 255.0
        mean = float(numpy.mean(grey)) or 0.01
        # Aim the average at about 45% grey. A darker frame gets a stronger
        # gamma; one already bright enough is left alone.
        gamma = max(0.35, min(1.0, numpy.log(0.45) / numpy.log(mean)))
        lifted = numpy.power(work, gamma)
        # Blend by darkness, so the dark parts take the lift and whatever is
        # already bright - a security light, a window - keeps its own level
        # instead of being pushed into clipping.
        weight = (1.0 - grey)[..., None]
        out = work * (1 - weight) + lifted * weight
        return numpy.clip(out * 255.0, 0, 255).astype(numpy.uint8)

    # ---- the work, with ImageMagick only ----

    def _magick_mean(self, path):
        """The average grey level of a file, out of 255, or None."""
        command = _magick()
        if not command or not os.path.isfile(path):
            return None
        out = _run([command, path, '-colorspace', 'Gray', '-resize', '25%',
                    '-format', '%[fx:mean*255]', 'info:'])
        found = re.search(r'[\d.]+', out)
        return float(found.group()) if found else None

    def _with_magick(self, frames, out_path):
        """The same three steps through ImageMagick, for a machine without
        OpenCV installed.

        It runs in two passes rather than one long command, because the gamma
        for the last step has to be worked out from how dark the stacked and
        equalized picture actually turned out - a fixed gamma is far too
        little on a frame averaging 10 out of 255 and far too much on one
        averaging 90. The first pass writes the stack, the mean is measured
        off it, and the second pass does the lifting."""
        command = _magick()
        if not command:
            return '', 'neither OpenCV nor ImageMagick is installed'
        done = []

        stage = out_path + '.stack.jpg'
        args = [command] + list(frames)
        if len(frames) > 1:
            # The average of the stack, which is what -evaluate-sequence mean
            # is for.
            args += ['-evaluate-sequence', 'mean']
        done.append(f'stacked {len(frames)}')

        if _magick_has_clahe():
            # Width and height as a percentage of the frame, then the bin
            # count and the clip limit - the same tiling as the OpenCV path.
            args += ['-clahe',
                     f'{100 // CLAHE_TILES}x{100 // CLAHE_TILES}%+128+{CLAHE_CLIP:g}']
            done.append('CLAHE')
        else:
            # ImageMagick 6 has no -clahe. A contrast stretch that throws away
            # a fraction of each end is the nearest thing it has, and it is
            # global rather than per tile, so it is weaker on exactly the
            # scenes that need it most. Worth saying so in the status line.
            args += ['-contrast-stretch', '0.5%x0.05%']
            done.append('contrast stretch (ImageMagick 6 has no CLAHE)')
        args += ['-quality', '95', stage]
        _run(args)

        if not os.path.isfile(stage) or not os.path.getsize(stage):
            return '', 'ImageMagick could not stack the snapshots'

        # The auto correct: a gamma worked out from this picture's own average,
        # aimed at the same 45% grey the OpenCV path aims at, then a sigmoidal
        # curve that puts some contrast back into the midtones the gamma
        # flattened. -gamma above 1 brightens, and the darker the frame the
        # larger the number has to be, so this is clamped at both ends rather
        # than trusted blindly on a frame that measured almost zero.
        mean = self._magick_mean(stage)
        if mean and mean > 0.5:
            import math                                       # noqa: PLC0415
            gamma = math.log(mean / 255.0) / math.log(0.45)
            gamma = max(1.0, min(4.0, gamma))
        else:
            gamma = 2.5
        _run([command, stage, '-gamma', f'{gamma:.2f}',
              '-sigmoidal-contrast', '3,45%', '-quality', '92', out_path])
        done.append(f'auto correct (analytic, gamma {gamma:.2f})')
        try:
            os.remove(stage)
        except OSError:
            pass

        if not os.path.isfile(out_path) or not os.path.getsize(out_path):
            return '', 'ImageMagick could not write the enhanced copy'
        return out_path, ', '.join(done)

    # ---- the two calls the rest of the programs make ----

    def new_path(self, folder):
        """A name for this correction that is not already taken.

        The stills are half a minute apart so a plain timestamp is normally
        unique, but the button in the window can be pressed twice in a second
        and the second press should not quietly overwrite the first."""
        stamp = time.strftime('enhanced-%Y%m%d-%H%M%S')
        path = os.path.join(folder, stamp + '.jpg')
        extra = 2
        while os.path.exists(path) and extra < 100:
            path = os.path.join(folder, f'{stamp}-{extra}.jpg')
            extra += 1
        return path

    def enhance_now(self, frame):
        """Correct this still whatever the switches say, and return
        (path, how).

        This is what the button in the window calls. Asking for it by hand is
        itself the instruction, so the switches are not consulted - they
        decide what happens automatically, not what may be done on request.
        """
        if not frame or not os.path.isfile(frame):
            return '', 'there is no snapshot to correct'
        folder = self.out_dir(frame)
        if not folder:
            return '', 'nowhere to write the corrected copy'
        out_path = self.new_path(folder)
        frames = self.stack_frames(frame)
        if _cv2():
            written, how = self._with_cv2(frames, out_path)
        else:
            written, how = self._with_magick(frames, out_path)
            how += ', OpenCV not installed'
        if written:
            self.last_output = written
            self.tiers = how
            self.prune(folder)
        return written, how


    def run(self, frame, gain_fraction=None, mean=None):
        """Enhance the current snapshot, when the switches say to.

        Returns a status line, or None when there was nothing to do. The
        enhanced copy's path is left in self.last_output.
        """
        on, why = self.active(gain_fraction=gain_fraction, mean=mean)
        if not on:
            self.note = why
            return None
        if not frame or not os.path.isfile(frame):
            self.note = 'no snapshot to enhance'
            return None

        out_folder = self.out_dir(frame)
        if not out_folder:
            self.note = 'nowhere to write the enhanced copy'
            return f'enhance: {self.note}'

        written, how = self.enhance_now(frame)
        if not written:
            self.note = how
            return f'enhance: {how}'
        self.note = f'{why} - {how}'
        return f'enhance: {os.path.basename(written)} [{why}] {how}'

    def report(self):
        """One line for the window saying which library is doing what, so a
        missing piece shows before it is needed rather than as a silent lack
        of improvement.

        The two do different jobs and both are wanted. OpenCV does the
        foundational work - decoding the stills, averaging the stack, the LAB
        conversion and the CLAHE, the box blurs behind the twelve feature
        planes, the resizing and the denoise. All of it is fixed, rule-based
        and fast, and none of it is learnt. TensorFlow does only the one thing
        that is learnt: running brain G over those features. So brain G needs
        both - the features are OpenCV's work and the brain cannot see without
        them - which is why a machine with TensorFlow but no OpenCV falls back
        to the analytic correct rather than half-running the brain.
        """
        if _cv2():
            parts = ['OpenCV doing the stacking, CLAHE and the features']
        else:
            parts = ['ImageMagick doing the stacking and the stretch '
                     '(no OpenCV, so no brain either)']
        family = active_family()
        brain = _brain(family)
        named = f'brain {family}' if family else 'brain G'
        if brain is not None:
            _model, meta, folder = brain
            trained = meta.get('trained_at', 'unknown date')
            source = meta.get('trained_from', 'general photos')
            note = brain_mandate(family, meta).get('note') or ''
            parts.append(f'TensorFlow running {named} from {folder} '
                         f'(trained on {source}, {trained})'
                         + (f' - {note}' if note else ''))
        else:
            parts.append(f'analytic correct instead of a brain - '
                         f'{_brain.why or "no brain"}')
        detector = _detector()
        if detector is not None:
            parts.append('OpenCV finding cars and people')
        return 'enhancement: ' + ', '.join(parts)


def main():
    """Enhance the newest snapshot in the folder and say what happened.

    This ignores the two switches, because running it by hand IS the
    instruction to run it. Takes an optional folder or file to work from.
    """
    import sys                                                # noqa: PLC0415

    args = [a for a in sys.argv[1:] if a != '--test']
    where = args[0] if args else os.environ.get(
        'WEBCAM_SNAPSHOT',
        os.path.join(os.path.expanduser('~'), 'Pictures', 'webcam-sample-images'))

    enhancer = Enhancer(folder=where if os.path.isdir(where) else None)
    print(enhancer.report())

    frame = where
    if os.path.isdir(where):
        shots = glob.glob(os.path.join(where, '*.jpg')) + \
                glob.glob(os.path.join(where, '*.jpeg'))
        if not shots:
            print(f'no snapshots in {where}')
            return 1
        frame = max(shots, key=os.path.getmtime)
    if not os.path.isfile(frame):
        print(f'{frame} is not there')
        return 1

    print(f'from {len(enhancer.stack_frames(frame))} frame(s), '
          f'newest {os.path.basename(frame)}')
    written, how = enhancer.enhance_now(frame)
    if written:
        print(f'wrote {written}\n  {how}')
        return 0
    print(f'could not enhance: {how}')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
