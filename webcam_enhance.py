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


def brain_dirs():
    """Where to look for the trained brain, in order.

    The environment first, then beside this file - which is where the copy
    that ships with these scripts lives - then the folder the trainer writes
    to, so a brain retrained on this camera's own photos is picked up without
    anything having to be told about it.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    found = []
    if os.environ.get('WEBCAM_BRAIN_DIR'):
        found.append(os.environ['WEBCAM_BRAIN_DIR'])
    found.append(os.path.join(os.path.expanduser('~'), '.webcam-models',
                              'brain_g_night_model'))
    found.append(os.path.join(here, 'brain_g_night_model'))
    return found


def _brain():
    """The brain, loaded once, or None.

    Three things are checked before it is used, and any of them failing means
    the analytic correct runs instead of a brain that cannot be trusted:

      * the feature count in meta.json has to match what this code builds,
        which is the stale-model check - a model trained against a different
        feature list would be reading its inputs in the wrong order
      * the sha256 in meta.json has to match the model file, so a half
        written or edited model is not run
      * it loads at all
    """
    if _brain.loaded is not None:
        return _brain.loaded or None
    _brain.loaded = False
    _brain.why = 'no trained brain found'

    module = _brain_module()
    if not module:
        _brain.why = 'train_webcam_brain.py is not next to this file'
        return None

    for folder in brain_dirs():
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

        wanted = module.BRAIN_G_FEATURES
        if meta.get('feature_count') not in (None, wanted):
            _brain.why = (f'the brain in {folder} is stale '
                          f'({meta["feature_count"]} features against {wanted}) '
                          '- retrain it with train_webcam_brain.py --force')
            continue

        recorded = meta.get('model_sha256')
        if recorded:
            try:
                actual = module.sha256_of(model_path)
            except OSError:
                actual = ''
            if actual and actual != recorded:
                _brain.why = f'the brain in {folder} does not match its sha256'
                continue

        try:
            os.environ.setdefault('TF_CPP_MIN_LOG_LEVEL', '2')
            from tensorflow import keras                     # noqa: PLC0415
            model = keras.models.load_model(model_path, compile=False)
        except Exception as problem:                          # noqa: BLE001
            _brain.why = f'could not load the brain: {problem}'
            continue

        _brain.loaded = (model, meta, folder)
        _brain.why = ''
        return _brain.loaded
    return None


_brain.loaded = None
_brain.why = ''


def read_settings():
    """The two switches, as they stand on disk. Both off when there is no
    file - which is how this behaved before it existed."""
    settings = {KEY_NIGHT: False, KEY_ALL_DAY: False, KEY_DETECT: False}
    try:
        with open(CONF_FILE, encoding='utf-8') as handle:
            for line in handle:
                if '=' not in line or line.lstrip().startswith('#'):
                    continue
                key, _, value = line.partition('=')
                key = key.strip()
                if key in settings:
                    settings[key] = value.strip() in ('1', 'true', 'True', 'yes', 'on')
    except OSError:
        pass
    return settings


def write_settings(night=None, all_day=None, detect=None):
    """Change any of the switches, leaving the others as they were."""
    settings = read_settings()
    if night is not None:
        settings[KEY_NIGHT] = bool(night)
    if all_day is not None:
        settings[KEY_ALL_DAY] = bool(all_day)
    if detect is not None:
        settings[KEY_DETECT] = bool(detect)
    try:
        with open(CONF_FILE, 'w', encoding='utf-8') as handle:
            handle.write('# Written by the webcam settings window. Two switches:\n')
            handle.write('#   night image CLAHE and ML auto correct\n')
            handle.write('#   24 hour image CLAHE and ML auto correct\n')
            handle.write('#   find cars and people and enhance them\n')
            for key, value in settings.items():
                handle.write(f'{key}={1 if value else 0}\n')
    except OSError:
        return False
    return True


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


def _cv2():
    """OpenCV, if it is installed. Everything here works without it, through
    ImageMagick; with it, the work is done in one process on arrays rather
    than by spawning a command per step, and the CLAHE is the real thing
    rather than an approximation of it."""
    if _cv2.module is None:
        try:
            import cv2                                        # noqa: PLC0415
            import numpy                                      # noqa: PLC0415
        except ImportError:
            _cv2.module = False
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

        # 1. the average
        stacked = images[0] if len(images) == 1 else \
            numpy.mean(numpy.stack(images, axis=0), axis=0)
        stacked = numpy.clip(stacked, 0, 255).astype(numpy.uint8)
        done = [f'stacked {len(images)}']

        # 2. CLAHE on lightness only, so the colour is left where it is. Run
        # on L of LAB rather than on the three channels separately, which
        # would pull the colour balance about tile by tile.
        lab = cv2.cvtColor(stacked, cv2.COLOR_BGR2LAB)
        light, green_red, blue_yellow = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=CLAHE_CLIP,
                                tileGridSize=(CLAHE_TILES, CLAHE_TILES))
        lab = cv2.merge((clahe.apply(light), green_red, blue_yellow))
        result = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
        done.append('CLAHE')

        # 3. the auto correct, by brain G when it is there
        brain = _brain()
        if brain is not None:
            lifted = self._brain_lift(brain, result)
            if lifted is not None:
                result = lifted
                done.append('brain G')
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
        if lifted_by > 1.6 or len(images) < 4:
            # h for luma, hColor for the colour channels.
            luma_strength = 3 if len(images) >= 4 else 5
            chroma_strength = int(min(18, 6 + 4 * lifted_by))
            result = cv2.fastNlMeansDenoisingColored(
                result, None, luma_strength, chroma_strength, 7, 21)
            done.append(f'denoise (chroma {chroma_strength})')

        # 4. find the car or the person, and give them a second, stronger
        # lift of their own.
        #
        # This runs LAST, on the corrected frame, and that order is the whole
        # trick: a detector looking at the raw night frame sees a black
        # rectangle and finds nothing in it, while the same detector on the
        # corrected frame finds what is actually there. Measured on a night
        # frame: nothing at all before the correction, three people and two
        # vehicles after it.
        self.detections = []
        if read_settings().get(KEY_DETECT):
            detector = _detector()
            if detector is None:
                done.append('detector not available')
            else:
                boxes = detector.detect(result)
                self.detections = boxes
                if boxes:
                    result = detector.boost(result, boxes)
                    done.append(f'found and boosted {detector.summary(boxes)}')
                    folder = os.path.dirname(out_path)
                    detector.crops(result, boxes,
                                   os.path.join(folder, 'detections'))
                else:
                    done.append('nothing found to boost')

        cv2.imwrite(out_path, result, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        return out_path, ', '.join(done)

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
        model, _meta, _folder = brain
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
            # Back out of the 0 to 1 the brain works in.
            gain = numpy.exp(gain * numpy.log(module.GAIN_MAX))
            saturation = numpy.exp((saturation * 2.0 - 1.0) * numpy.log(4.0))

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
            return numpy.clip(out * 255.0, 0, 255).astype(numpy.uint8)
        except Exception:                                     # noqa: BLE001
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
        brain = _brain()
        if brain is not None:
            _model, meta, folder = brain
            trained = meta.get('trained_at', 'unknown date')
            source = meta.get('trained_from', 'general photos')
            parts.append(f'TensorFlow running brain G from {folder} '
                         f'(trained on {source}, {trained})')
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
