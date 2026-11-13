"""Automatic light adjustment: a closed loop on the picture the camera is
actually producing.

This runs alongside the sunrise and sunset schedule rather than replacing it.
The presets - night, day, post-sunrise, pre-sunset, bright sun, mono - stay
exactly as they are, and the clock still decides which one is in force. What
this adds is a correction on top of whichever one is active, so the picture is
neither too bright nor too dark whatever the light outside is actually doing:
a cloudy afternoon, a security light coming on at 2am, snow glare at noon.

It is a correction and not a replacement because a preset is more than light.
The golden hour presets have their warm automatic white balance, bright sun has
its cold white balance and tight focus. Anything that imposed its own absolute
numbers would flatten all of that into one look. So this measures the picture,
decides "a bit brighter than this" or "a bit darker than this", and moves five
light controls away from whatever the preset set them to, by a trim of -5 to +5
steps. The preset's character survives; only its exposure gets corrected.

It re-learns its baseline on its own. Every pass it reads the five controls
back off the camera, and if they are not what it last wrote then a preset fired
or a hand moved a slider, so that becomes the new baseline and the trim returns
to zero. Nothing has to tell it a preset ran.

The picture has to come from the external camera software, because a V4L2
camera lets only one program stream from it and that software holds the device.
Measuring a still it has already written is not the same as opening the camera.
Set WEBCAM_SNAPSHOT in the environment, or pass snapshot= to LightMeter, to:

  * a directory - the newest .jpg in it is measured, which is what a "save
    snapshots to folder" module in the camera software produces
  * a single file - for software that overwrites the same still each time
  * an http:// URL - a snapshot URL from the camera software's web server

With none of those set, nothing is corrected and the presets stand as they
are, which is how the scheduler behaved before this existed.

Needs ImageMagick for the measuring:  sudo apt install imagemagick
"""

import glob
import os
import shutil
import re
import subprocess
import time

# The night enhancement - frame averaging, CLAHE and the ML auto correct - is
# a separate module because it is a separate kind of work: it never touches
# the camera, it only reads the stills this loop has already measured. With
# the file missing, everything here carries on exactly as before.
try:
    from webcam_enhance import Enhancer
    # The stream sampling switch lives with the other correction settings,
    # so that one file is the whole of what the correction is doing.
    from webcam_enhance import (read_settings, KEY_SAMPLE_STREAM,
                                KEY_SAMPLE_SECONDS, DEFAULT_SAMPLE_SECONDS)
except ImportError:                                            # noqa: BLE001
    Enhancer = None
    read_settings = None
    KEY_SAMPLE_STREAM = 'sample_the_stream'
    KEY_SAMPLE_SECONDS = 'sample_seconds'
    DEFAULT_SAMPLE_SECONDS = 300

# Reads the controls back, so every adjustment can be recorded with the
# camera's own values either side of it.
try:
    import webcam_lock
except ImportError:
    webcam_lock = None       # no lock module: meter the whole frame, as before

try:
    import webcam_verify as _verify
except ImportError:                                            # noqa: BLE001
    _verify = None

# The brain's own module: it owns the harvest that builds a training set out of
# this camera's frames, and the retraining that uses it. Importing it does not
# import TensorFlow.
try:
    import train_webcam_brain
except ImportError:                                            # noqa: BLE001
    train_webcam_brain = None

# How often to file a still into the training set. Every pass would fill the
# quotas with thirty near-identical frames a minute; two minutes apart gives
# the light time to have actually changed. A preset or light change overrides
# this and files one immediately, which is the point of the whole arrangement.
HARVEST_EVERY = 120

# Measure every 5 seconds, then wait two samples after a correction.
#
# Each change takes a moment to reach the next still the camera software
# writes, and a loop that acts faster than its own feedback arrives will
# oscillate - the picture breathes in and out all night instead of settling.
# Ten seconds of patience is what buys the settling.
# How often the picture is looked at: every 10 seconds. The light at dusk
# moves faster than a half-minute check can follow, and the correction is
# cheap - a mean and two clipping figures off a quarter-size copy of a still
# the recorder already wrote. Nothing is captured for this and the camera is
# not touched, so the cost of looking more often is a few milliseconds.
#
# The trim itself is still rate-limited by METER_SETTLE and the dead band
# below, so looking every 10 seconds does not mean moving the controls every
# 10 seconds - it means noticing sooner when they need to move.
METER_EVERY = 10
METER_SETTLE = 1

# What a good picture looks like, as numbers.
#
# MEAN_TARGET is the average grey level wanted out of 255. 100 is a little
# under middle grey, which suits a dark scene with a few bright lights in it
# far better than a true middle grey does: aim at 128 and the security light
# blows out before the car under it is visible.
#
# The two clipping limits are the "not too much white light and not too much
# darkness" check. The mean cannot see either problem on its own - a frame that
# is half blown-out floodlight and half pitch black averages to a perfectly
# respectable middle grey while showing nothing at all. So the fraction of the
# frame gone to pure white and the fraction gone to pure black are measured
# separately, and either one can override the mean.
MEAN_TARGET = int(os.environ.get('WEBCAM_TARGET', 100))
MEAN_TOL = 14          # dead band, so it is not forever nudging
MAX_HOT = 0.020        # at most 2% of the frame blown to white
MAX_COLD = 0.300       # at most 30% of the frame crushed to black

# The five controls this loop may touch, and how far each moves per step of
# trim, as a fraction of that control's own range.
#
# A fraction rather than a fixed number because these scripts drive more than
# one camera: brightness is -64 to 64 on one and 0 to 255 on another, so the
# same raw number would be a nudge on one and a lurch on the other. The real
# range comes from v4l2-ctl -L at startup.
#
# Gain carries most of the correction, because it is the control that actually
# amplifies signal. Brightness moves least: on these webcams it is a black
# level lift rather than exposure, so it buys apparent brightness at the cost
# of the black end, which is the mistake the old maximum-brightness night
# preset made. Contrast rises with the trim to keep the range spread out once
# gain has lifted everything.
#
# Saturation used to be in here, coming down as the trim went up. It is out
# now, deliberately: the colour level a preset asks for is the colour level
# that is wanted, at every hour, and a meter quietly walking it down five
# steps into the night is the meter overruling the preset about how the
# picture should look. The colour that is wanted at night is set in the night
# preset itself, where it can be seen and changed. See LOCKED_CONTROLS below.
TRIM_RULE = {
    'gain': 0.120,
    'brightness': 0.025,
    'contrast': 0.030,
    'backlight_compensation': 0.200,
}

# The controls this loop is not allowed to move, whatever the picture looks
# like.
#
# Saturation and hue belong to the preset outright: they decide what the
# colours *are*, not how bright the frame is, and no amount of metering the
# grey level is evidence about either. Gamma is the awkward one and gets a
# rule of its own below - it does change brightness, which is exactly why it
# is tempting, and raising it is the cheapest-looking way to brighten a night
# frame. It is also the worst: gamma lifts the noise floor along with the
# shadows and flattens the midtones the number plate lives in, and it does it
# to the stream the recorder is writing rather than to a copy. So gamma may be
# left where the preset put it or pulled back DOWN from there, and never
# raised above it. The software correction, which works on copies of the
# stills and can be deleted and redone, is where a gamma lift belongs.
LOCKED_CONTROLS = ('saturation', 'hue')
GAMMA_CTRL = 'gamma'

TRIM_MAX = 5           # steps either side of whatever the preset set

# Snapshots to keep in the folder. The oldest beyond this are deleted.
SNAP_KEEP = 30

# Sharpness, as a fraction of the control's range: full sharpness in daylight
# and through the golden hours, backed off to 80-90% at night.
#
# This is set outright rather than trimmed, and it overrides whatever sharpness
# the preset asked for - including the night preset's deliberately low value.
# That is on purpose and on request: the picture is wanted sharp.
#
# What decides night from day here is gain, not the clock. Gain is the only
# control that amplifies the sensor's noise along with the signal, so a camera
# running near the top of its gain range is by definition working in the dark
# whatever time the clock says - and the golden hour presets, which sit under
# half of the gain range, come out at full sharpness as they should.
# Sharpening is contrast enhancement at the pixel level and it sharpens noise
# as happily as it sharpens the edge of a number plate, which is the one
# reason to ease off at all once the gain is high.
SHARP_DAY = 1.00       # at or below half gain: full sharpness
SHARP_NIGHT = 0.90     # just into high gain
SHARP_DARK = 0.70      # at the top of the gain range
SHARP_KNEE = 0.50      # the gain fraction where the easing off starts

# And the two ends sharpness is never allowed outside of, whatever the curve
# above works out and whatever a preset asked for: half sharpness at the very
# least, full sharpness at the very most.
#
# The floor is the useful half of this. Sharpening amplifies grain, so the
# honest night answer looks like "turn it down", and turned down far enough
# the picture is a smooth grey blur with no edge left to read a number plate
# off - grain-free and useless. Half is the point below which this stops
# buying anything. The ceiling only says there is no such thing as more than
# all of a control.
SHARP_FLOOR = 0.50
SHARP_CEIL = 1.00

# The ranges to use when the camera does not report one.
#
# The hardware is asked first, at startup, and whatever it says is what gets
# used - that is the only way to be right about a camera that has not been seen
# before. These two tables are the fallback for anything it does not answer
# for, and they are not invented: they are the ranges these scripts have been
# using all along, the modern set from the ONN webcam's own sliders and the
# legacy set from the older camera's 0-255 controls.
#
# Which table applies is decided by the same test the preset engine uses: a
# camera with exposure_time_absolute is the modern kind, one with
# exposure_absolute is the older kind.
FALLBACK_MODERN = {
    'brightness': (-64, 64),
    'contrast': (0, 95),
    'saturation': (0, 120),
    'gain': (0, 100),
    'backlight_compensation': (0, 12),
    'sharpness': (0, 7),
}
FALLBACK_LEGACY = {
    'brightness': (0, 255),
    'contrast': (0, 255),
    'saturation': (0, 255),
    'gain': (0, 255),
    'backlight_compensation': (0, 16),
    'sharpness': (0, 255),
}

# The two names continuous autofocus goes by: the current one, and the
# pre-5.10 kernel name on the older webcam.
FOCUS_CTRL = 'focus_automatic_continuous'
FOCUS_CTRL_LEGACY = 'focus_auto'

# The manual focus control, read so that the preset's focus distance can be
# kept and gone back to.
FOCUS_ABS_CTRL = 'focus_absolute'

# How far the focus may wander from where the preset left it before it is put
# back, as a fraction of the control's range. Twelve per cent is about a foot
# at the distance these cameras are aimed at: enough slack for the autofocus
# to find the edge of a car properly, not enough for it to give up and go
# hunting the fence behind it or the moth in front of it.
FOCUS_WINDOW = 0.12

# How blurry the picture has to be before the autofocus is allowed to re-run.
#
# This is the variance of the Laplacian - the standard "is it in focus" number
# - taken on a quarter size grey copy. A sharp daylight frame from this camera
# runs in the hundreds; a frame that has lost focus runs in the low tens. The
# night threshold is lower because a correctly focused night frame is
# genuinely less detailed than a correctly focused day one, and judging night
# by the day number would have the autofocus hunting all night, which is the
# exact behaviour that puts a soft first few seconds into every recording.
#
# Below the threshold: refocus, which is the one case it is wanted. At or
# above: leave the focus alone and, if it has drifted outside FOCUS_WINDOW of
# the preset, put it back where the preset had it.
BLUR_DAY = 55.0
BLUR_NIGHT = 22.0

_RANGE_RE = re.compile(r'^\s*(\w+)\s+0x[0-9a-f]+\s+\((?:int|bool|menu|intmenu)\)', re.I)
_VALUE_RE = re.compile(r'(\w+):\s*(-?\d+)')

# Directories worth looking in when nothing is configured.
# The folder the recording software writes its stills into. WEBCAM_SNAPSHOT
# overrides it; this is the default so that nothing needs setting for the
# camera these scripts were written for.
# os.path.expanduser falls back to the password database when HOME is not set,
# which is what happens under some cron and systemd setups - so this answers
# for whichever user is actually running the script, with no user name written
# into it. The folder is created at startup if it is not there.
SNAPSHOT_DEFAULT = os.path.expanduser('~/Pictures/webcam-sample-images')

SNAPSHOT_GUESSES = (
    SNAPSHOT_DEFAULT,
    # Where this used to put them. Kept so that an installation that has been
    # filling that folder for months is still found rather than starting over
    # with nothing to measure.
    os.path.expanduser('~/Pictures/onn-webcam/program-snapshots-for-auto-adjust-picture'),
    os.path.expanduser('~/webcam-snapshots'),
    os.path.expanduser('~/.xeoma/snapshots'),
    '/opt/Xeoma/snapshots',
    '/tmp/webcam-snapshots',
)


# ---- the error log ----
#
# Everything that goes wrong is written here with a timestamp, so a failure at
# three in the morning can be read back in the morning. A status line only ever
# shows the latest thing; this keeps the history.
#
# What gets logged: the startup probe, every snapshot failure with ffmpeg's own
# words, every trim the meter applies, every time it re-learns a preset, and
# anything that stops it working. What does not: the routine pass where the
# picture is already where it should be, which would be twelve lines a minute
# and would bury everything worth reading.
#
# Repeats are collapsed. A busy camera says so every five seconds, and without
# this the log would be nothing else; an error identical to the last one is
# written again only after ten minutes.
LOG_FILE = os.environ.get('WEBCAM_LOG',
                          os.path.join(os.path.expanduser('~'), '.webcam-light-meter.log'))
LOG_MAX = 1024 * 1024          # trim the file once it passes a megabyte


def log(message, always=False):
    """Append one timestamped line to the log."""
    if not message:
        return False
    now = time.time()
    if not always and message == log.last_message and now - log.last_time < 600:
        return False
    log.last_message, log.last_time = message, now

    try:
        # Keep the file from growing without end: past a megabyte, the newest
        # 500 lines are kept and the rest dropped.
        if os.path.isfile(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX:
            with open(LOG_FILE, encoding='utf-8', errors='replace') as handle:
                lines = handle.readlines()[-500:]
            with open(LOG_FILE, 'w', encoding='utf-8') as handle:
                handle.writelines(lines)

        with open(LOG_FILE, 'a', encoding='utf-8') as handle:
            handle.write(time.strftime('%Y-%m-%d %H:%M:%S  ') + message + '\n')
    except OSError:
        return False
    return True


log.last_message = ''
log.last_time = 0.0


def _magick():
    """Which ImageMagick is installed.

    ImageMagick 7, which is what Ubuntu 26.04 ships, dropped the `convert`
    command in favour of `magick`; ImageMagick 6, on 24.04 and earlier, has
    `convert` and no `magick`. The arguments are the same either way, so only
    the command name has to be worked out. Without this the measuring silently
    finds nothing on a newer Ubuntu and the meter sits there reporting no
    snapshot."""
    if _magick.found is None:
        _magick.found = shutil.which('magick') or shutil.which('convert') or ''
    return _magick.found


_magick.found = None


def _run(args):
    """v4l2-ctl and ImageMagick, with their noise swallowed and failures
    turned into an empty string rather than an exception. Nothing here is
    worth taking the scheduler down for."""
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return out.stdout
    except (OSError, subprocess.SubprocessError):
        return ''


class LightMeter:
    def __init__(self, device='/dev/video0', snapshot=None, target=MEAN_TARGET):
        self.device = device or os.environ.get('WEBCAM_DEVICE', '/dev/video0')
        self.snapshot = snapshot if snapshot is not None else \
            os.environ.get('WEBCAM_SNAPSHOT', SNAPSHOT_DEFAULT)
        self.target = target
        self.ranges = {}        # control -> (min, max), read from the camera
        self.sources = {}       # control -> 'camera' or 'fallback', for reporting
        self.camera_kind = 'unknown'
        self.focus_ctrl = None      # whichever autofocus control this camera has
        self.focus_abs = None       # the manual focus control, if there is one
        self.focus_range = None     # its range, for the drift window
        self.focus_base = None      # where the preset left the focus
        self.locked_have = ()       # which hands-off controls this camera has
        self.locked_base = {}       # and the values the preset gave them
        self.blur = None            # the last blur reading, for the status line
        self.lock_note = ''         # what the locked-on target did this pass
        self.base = {}          # the preset's own values, re-learnt automatically
        self.wrote = {}         # what this loop last sent, to spot outside changes
        self.trim = 0
        self.hold = 0
        self.means = []
        self.last_dir = 0
        self.last_pass = 0.0
        self.tmp = '/tmp/.webcam-meter.jpg'
        self.capture_note = ''     # set once, so a busy camera is not shouted about
        self.capture_checked = False
        self.busy_until = 0.0      # when the camera is held, stop asking for a while
        # The enhancer, which writes enhanced copies of the stills into an
        # `enhanced` folder beside them when either of its two switches is on.
        # It is given no folder of its own: it takes each frame's own folder,
        # so it follows the snapshot setting wherever that is pointed.
        self.enhancer = Enhancer() if Enhancer else None
        self.enhance_note = ''
        self.last_harvest = 0.0
        self.harvest_now = True     # file one on the first pass
        self.harvest_note = ''
        self.train_note = ''
        # The startup probe. Done here so that simply creating the meter asks
        # the hardware what it can do, before any correction is attempted.
        self.learn_ranges()

    # ---- the camera ----

    def learn_ranges(self):
        """Ask the camera, at startup, for the real range of every control
        this loop uses, and fall back to the ranges these scripts already used
        for anything it does not answer for.

        This matters more than it looks. A trim step is a fraction of a
        control's range, so a wrong range means a step of the wrong size: guess
        0-64 for a contrast control that really runs 0-95 and every correction
        lands a third short. Asking the hardware is the only way to be right
        about a camera that has not been seen before, and the fallback means an
        unanswered control still gets a sensible step instead of being
        dropped."""
        self.ranges = {}
        self.sources = {}
        self.camera_kind = 'unknown'

        seen = set()
        for line in _run(['v4l2-ctl', '-d', self.device, '-L']).splitlines():
            found = _RANGE_RE.match(line)
            if not found:
                continue
            name = found.group(1)
            seen.add(name)
            if name not in TRIM_RULE and name != 'sharpness':
                continue
            low = re.search(r'min=(-?\d+)', line)
            high = re.search(r'max=(-?\d+)', line)
            if low and high and int(high.group(1)) > int(low.group(1)):
                self.ranges[name] = (int(low.group(1)), int(high.group(1)))
                self.sources[name] = 'camera'

        # Which name this camera's continuous autofocus goes by, if it has one.
        self.focus_ctrl = (FOCUS_CTRL if FOCUS_CTRL in seen
                           else FOCUS_CTRL_LEGACY if FOCUS_CTRL_LEGACY in seen
                           else None)

        # The manual focus control, if the camera has one. Only read, and
        # only ever written back to the value the preset asked for.
        self.focus_abs = FOCUS_ABS_CTRL if FOCUS_ABS_CTRL in seen else None
        if self.focus_abs:
            for line in _run(['v4l2-ctl', '-d', self.device, '-L']).splitlines():
                if not line.strip().startswith(FOCUS_ABS_CTRL):
                    continue
                low = re.search(r'min=(-?\d+)', line)
                high = re.search(r'max=(-?\d+)', line)
                if low and high and int(high.group(1)) > int(low.group(1)):
                    self.focus_range = (int(low.group(1)), int(high.group(1)))

        # Which of the hands-off controls this camera actually has, so that
        # the guard below reads back only what exists.
        self.locked_have = tuple(name for name in
                                 tuple(LOCKED_CONTROLS) + (GAMMA_CTRL,)
                                 if name in seen)
        self.learn_locked()

        # The same test the preset engine uses to tell the two cameras apart.
        if 'exposure_time_absolute' in seen:
            self.camera_kind = 'modern'
        elif 'exposure_absolute' in seen:
            self.camera_kind = 'legacy'

        # Anything the camera did not answer for gets the settings already in
        # use. With no answer at all - camera unplugged, or v4l2-ctl missing -
        # the modern table is the one to assume, since that is the camera these
        # scripts are run against.
        fallback = FALLBACK_LEGACY if self.camera_kind == 'legacy' else FALLBACK_MODERN
        for name in list(TRIM_RULE) + ['sharpness']:
            if name in self.ranges or name not in fallback:
                continue
            self.ranges[name] = fallback[name]
            self.sources[name] = 'fallback'
        log(self.range_report(), always=True)
        return len(self.ranges)

    def range_report(self):
        """One line saying what the startup probe found, for the log."""
        from_camera = sorted(k for k, v in self.sources.items() if v == 'camera')
        assumed = sorted(k for k, v in self.sources.items() if v == 'fallback')
        text = f'light meter: {self.camera_kind} camera'
        if from_camera:
            text += ', ranges read from the camera for ' + ', '.join(from_camera)
        if assumed:
            text += ', using the settings already in use for ' + ', '.join(assumed)
        return text

    def read_controls(self):
        """Read the controls back in one call. v4l2-ctl takes a comma
        separated list, which is one process instead of five every five
        seconds."""
        names = sorted(k for k in self.ranges if k in TRIM_RULE)
        if not names:
            return {}
        out = _run(['v4l2-ctl', '-d', self.device, '--get-ctrl', ','.join(names)])
        return {k: int(v) for k, v in _VALUE_RE.findall(out) if k in self.ranges}

    def refocus(self):
        """Re-run the autofocus after a light change.

        Changing gain, brightness or contrast changes what the autofocus is
        looking at, and the camera does not always re-run it on its own: the
        first seconds after a change come out soft, which on a recording is the
        part you most want sharp. Switching continuous autofocus off and
        straight back on makes it start a fresh search.

        It ends switched ON every time. Nothing in these scripts ever leaves
        the camera in manual focus.

        The pause between off and on is there because the driver has to see the
        control actually change state; without it the two writes can land in
        the same instant and nothing is re-triggered. A third of a second is
        enough and is not long enough to be felt."""
        # Falls back to the current control name when the probe found nothing,
        # so autofocus still ends up on; a write to a control the camera lacks
        # fails harmlessly.
        ctrl = self.focus_ctrl or FOCUS_CTRL
        _run(['v4l2-ctl', '-d', self.device, '--set-ctrl', f'{ctrl}=0'])
        time.sleep(0.33)
        _run(['v4l2-ctl', '-d', self.device, '--set-ctrl', f'{ctrl}=1'])
        return True

    def gain_fraction(self, gain_now=None):
        """How far up its own range the gain is sitting, 0 to 1, or None.

        This is the one honest test of how dark it actually is. The clock says
        what time it is; the gain says whether the camera is struggling, which
        is what the enhancement needs to know - a floodlit yard at midnight
        needs none of it and an overcast dusk may need all of it.
        """
        if 'gain' not in self.ranges:
            return None
        if gain_now is None:
            gain_now = self.wrote.get('gain')
        if gain_now is None:
            gain_now = self.get_gain()
        if gain_now is None:
            return None
        low, high = self.ranges['gain']
        if high <= low:
            return None
        return max(0.0, min(1.0, (gain_now - low) / (high - low)))

    def get_gain(self):
        """Read the gain off the camera, for when nothing has been written
        yet."""
        out = _run(['v4l2-ctl', '-d', self.device, '--get-ctrl', 'gain'])
        found = re.search(r':\s*(-?\d+)', out)
        return int(found.group(1)) if found else None

    def enhance(self, frame, mean=None):
        """Hand the current still to the enhancer, if there is one.

        This is deliberately the last thing a pass does and it is kept well
        away from everything above it: it reads a file and writes a file, and
        it sends nothing to the camera. A failure in here cannot affect the
        correction loop, and the enhanced copies go in their own subfolder so
        that the next pass still measures the camera's own output rather than
        measuring its own enhancement and chasing it.
        """
        if not self.enhancer:
            self.enhance_note = ''
            return None
        try:
            line = self.enhancer.run(frame, gain_fraction=self.gain_fraction(),
                                     mean=mean)
        except Exception as problem:                           # noqa: BLE001
            self.enhance_note = f'enhancement failed: {problem}'
            log(self.enhance_note)
            return self.enhance_note
        self.enhance_note = self.enhancer.note
        if line:
            log(line)
        return line

    def harvest(self, frame, mean=None):
        """File this still into the brain's training set, and retrain daily.

        Only well exposed frames become training targets - a dark night frame
        cannot be its own answer sheet, so one is filed only when a well
        exposed frame of the same scene turns up within a couple of minutes to
        be its answer. That happens at dusk and dawn and when a light comes
        on, which is where the real training pairs come from.
        """
        if not train_webcam_brain:
            return None
        now = time.time()
        if not self.harvest_now and now - self.last_harvest < HARVEST_EVERY:
            return None
        self.harvest_now = False
        self.last_harvest = now

        try:
            kept, why = train_webcam_brain.harvest(
                frame,
                preset=train_webcam_brain.current_preset(),
                gain_fraction=self.gain_fraction(),
            )
            self.harvest_note = why
            if kept:
                log(f'harvest: {why}')
        except Exception as problem:                           # noqa: BLE001
            self.harvest_note = f'harvest failed: {problem}'
            log(self.harvest_note)
            return None

        # Retrain on this camera once there is enough, and then once a day.
        # self_train_if_ready does nothing and returns quickly when it is not
        # time, so calling it here costs nothing.
        try:
            did, line = train_webcam_brain.self_train_if_ready(quiet=True)
        except Exception as problem:                           # noqa: BLE001
            self.train_note = f'retrain failed: {problem}'
            log(self.train_note)
            return None
        self.train_note = line
        if did:
            log(line, always=True)
            # The brain on disk has just been replaced, so make the enhancer
            # drop the one it has loaded and pick up the new one.
            try:
                import webcam_enhance                          # noqa: PLC0415
                webcam_enhance._brain.loaded = None
            except ImportError:
                pass
            return line
        return None

    # ---- the controls this loop keeps its hands off ----

    def learn_locked(self):
        """Read saturation, hue and gamma as the preset left them.

        These are not trimmed, so the only reason to read them is to be able
        to tell later whether they are still where the preset put them - and,
        for gamma, what the ceiling is. Re-read whenever the baseline is
        re-learnt, because a preset firing is exactly when they legitimately
        change.
        """
        names = list(self.locked_have)
        if self.focus_abs:
            names.append(self.focus_abs)
        if not names:
            return {}
        out = _run(['v4l2-ctl', '-d', self.device, '--get-ctrl',
                    ','.join(names)])
        found = {k: int(v) for k, v in _VALUE_RE.findall(out)}
        if self.focus_abs and self.focus_abs in found:
            self.focus_base = found.pop(self.focus_abs)
        self.locked_base = {k: v for k, v in found.items()
                            if k in self.locked_have}
        return self.locked_base

    def guard_locked(self):
        """Put saturation, hue and gamma back if they have moved off the
        preset - and, for gamma, only ever downwards.

        Nothing in this loop writes them, so in normal running this finds
        nothing and costs one read. It is here for the case that is not normal
        running: another script, a half-finished preset, or a driver that
        resets a control when the exposure mode changes. Without it a gamma
        that got raised once would stay raised for the rest of the night with
        the correction dutifully metering around it.

        Returns a note naming what it had to undo, or ''.
        """
        if not self.locked_have or not self.locked_base:
            return ''
        out = _run(['v4l2-ctl', '-d', self.device, '--get-ctrl',
                    ','.join(self.locked_have)])
        have = {k: int(v) for k, v in _VALUE_RE.findall(out)}
        fixed = []
        for name, want in self.locked_base.items():
            now = have.get(name)
            if now is None or now == want:
                continue
            if name == GAMMA_CTRL and now < want:
                # Pulled down, not up. Allowed, and worth keeping: a lower
                # gamma holds the midtones together. Take it as the new
                # ceiling rather than undoing it.
                self.locked_base[name] = now
                continue
            _run(['v4l2-ctl', '-d', self.device, '--set-ctrl',
                  f'{name}={want}'])
            fixed.append(f'{name} {now} -> {want}')
        if fixed:
            log('put the hands-off controls back: ' + ', '.join(fixed),
                always=True)
            return 'restored ' + ', '.join(fixed)
        return ''

    # ---- focus ----

    def blur_score(self, path):
        """How sharp the picture actually is: the variance of its Laplacian.

        A big number is a detailed picture, a small one is a soft picture, and
        the useful property is that it does not care how bright the frame is -
        which is what makes it the right test here, where the frame is dark
        half the time by design.

        OpenCV when it is there, and ImageMagick's own standard deviation of
        an edge-detected copy when it is not, scaled to roughly the same
        numbers. None when neither is available, which means "no opinion" and
        leaves the focus alone.
        """
        if not path or not os.path.exists(path):
            return None
        try:
            import cv2                                        # noqa: PLC0415
            image = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
            if image is not None and image.size:
                small = cv2.resize(image, (0, 0), fx=0.5, fy=0.5,
                                   interpolation=cv2.INTER_AREA)
                return float(cv2.Laplacian(small, cv2.CV_64F).var())
        except Exception:                                     # noqa: BLE001
            pass
        magick = _magick()
        if not magick:
            return None
        out = _run([magick, path, '-colorspace', 'Gray', '-resize', '50%',
                    '-define', 'convolve:scale=!', '-morphology', 'Convolve',
                    'Laplacian:0', '-format', '%[fx:standard_deviation*255]',
                    'info:'])
        found = re.search(r'[\d.]+', out)
        if not found:
            return None
        # ImageMagick gives a standard deviation; the Laplacian variance is
        # its square, which is what the thresholds are written in.
        return float(found.group()) ** 2

    def blur_limit(self, gain_now=None):
        """The blur threshold for how dark it is now."""
        fraction = self.gain_fraction(gain_now)
        if fraction is None:
            return BLUR_DAY
        if fraction <= SHARP_KNEE:
            return BLUR_DAY
        into_dark = (fraction - SHARP_KNEE) / (1 - SHARP_KNEE)
        return BLUR_DAY - (BLUR_DAY - BLUR_NIGHT) * into_dark

    def hold_focus(self):
        """Put the focus back near where the preset left it.

        Only when it has drifted further than FOCUS_WINDOW, and only to the
        preset's own value - this never picks a focus distance of its own.
        Continuous autofocus is switched back on afterwards, because nothing
        in these scripts leaves the camera in manual focus.
        """
        if not self.focus_abs or self.focus_base is None or not self.focus_range:
            return ''
        low, high = self.focus_range
        window = max(1, int(round(FOCUS_WINDOW * (high - low))))
        out = _run(['v4l2-ctl', '-d', self.device, '--get-ctrl',
                    self.focus_abs])
        found = _VALUE_RE.search(out)
        if not found:
            return ''
        now = int(found.group(2))
        if abs(now - self.focus_base) <= window:
            return ''
        ctrl = self.focus_ctrl or FOCUS_CTRL
        _run(['v4l2-ctl', '-d', self.device, '--set-ctrl', f'{ctrl}=0'])
        _run(['v4l2-ctl', '-d', self.device, '--set-ctrl',
              f'{self.focus_abs}={self.focus_base}'])
        time.sleep(0.2)
        _run(['v4l2-ctl', '-d', self.device, '--set-ctrl', f'{ctrl}=1'])
        log(f'focus had drifted to {now}, put back to the preset\'s '
            f'{self.focus_base}', always=True)
        return f'focus put back to {self.focus_base}'

    def maybe_refocus(self, path=None, gain_now=None):
        """Refocus only when the picture is actually blurry.

        The old behaviour was to re-run the autofocus after every light
        change, on the grounds that the first seconds after a change come out
        soft. That is true and it is also how the focus ends up somewhere the
        preset never asked for: every trim sent the autofocus hunting, and a
        night scene with nothing well-lit to lock on to is precisely the scene
        it hunts worst in. Hunting is worse than a slightly stale focus.

        So the picture gets measured first. Blurry - refocus, which is the
        case the refocus was put in for. Sharp enough - leave the focus
        alone and only pull it back if it has wandered out of the window
        around the preset.
        """
        self.blur = self.blur_score(path) if path else None
        limit = self.blur_limit(gain_now)
        if self.blur is None:
            # Nothing to judge by. Hold the preset's focus rather than
            # starting a search in the dark.
            return self.hold_focus()
        if self.blur < limit:
            log(f'picture is blurry (sharpness score {self.blur:.0f}, '
                f'wanted {limit:.0f}) - refocusing')
            self.refocus()
            return f'blurry ({self.blur:.0f} < {limit:.0f}) - refocused'
        return self.hold_focus()

    def sharpness_for(self, gain_now):
        """The sharpness this much gain calls for, in the control's own
        units. None when the camera has no sharpness control or no gain to
        judge by."""
        if 'sharpness' not in self.ranges or 'gain' not in self.ranges or gain_now is None:
            return None
        gain_low, gain_high = self.ranges['gain']
        sharp_low, sharp_high = self.ranges['sharpness']
        fraction = (gain_now - gain_low) / (gain_high - gain_low)
        fraction = max(0.0, min(1.0, fraction))
        if fraction <= SHARP_KNEE:
            want = SHARP_DAY
        else:
            # Straight line from 90% at the knee down to 80% at full gain.
            into_dark = (fraction - SHARP_KNEE) / (1 - SHARP_KNEE)
            want = SHARP_NIGHT - (SHARP_NIGHT - SHARP_DARK) * into_dark
        # Clamped to the 50-100% band before it is turned into the
        # control's own units, so the curve above can be changed freely
        # without any edit of it being able to put the picture outside the
        # band that is wanted.
        want = max(SHARP_FLOOR, min(SHARP_CEIL, want))
        value = int(round(sharp_low + (sharp_high - sharp_low) * want))
        return max(sharp_low, min(sharp_high, value))

    def set_sharpness(self, gain_now):
        """Send the wanted sharpness for this much gain. Returns what was
        sent, or None."""
        value = self.sharpness_for(gain_now)
        if value is not None:
            _run(['v4l2-ctl', '-d', self.device, '--set-ctrl', f'sharpness={value}'])
        return value

    def apply_trim(self, trim):
        """Send the given trim, worked out from the preset's own baseline.

        Returns the number of controls actually written. That return value
        matters: without a baseline there is nothing to trim *from*, every
        control is skipped, and this used to go round the loop writing
        nothing and still report "trimmed to +5". The picture stayed exactly
        as dark as it was while the log said it was being corrected, which is
        the worst kind of failure here - a silent one that reads as success.

        The baseline is normally learnt on the first pass. It can be missing
        when the camera could not be read at all: unplugged, or v4l2-ctl not
        installed. So try once more to learn it here, and if there is still
        nothing, say so rather than pretending.
        """
        self.trim = max(-TRIM_MAX, min(TRIM_MAX, trim))
        if not self.base:
            if not self.ranges:
                self.learn_ranges()
            self.base = dict(self.read_controls())
            if self.base:
                log('learnt the baseline late, from the camera, because the '
                    'first pass could not read it', always=True)
        # Every control the camera has, as it stands before anything is
        # sent. Read in full rather than just the five being trimmed: the
        # exposure time and the focus move on their own when the light
        # changes, and seeing that happen beside the values that were asked
        # for is most of the use of keeping the record at all.
        before = _verify.read_controls(self.device) if _verify else {}

        self.wrote = {}
        for ctrl, fraction in TRIM_RULE.items():
            if ctrl not in self.base or ctrl not in self.ranges:
                continue
            low, high = self.ranges[ctrl]
            per = int(round(fraction * (high - low)))
            if per == 0:                      # never a no-op step
                per = 1 if fraction > 0 else -1
            value = max(low, min(high, self.base[ctrl] + per * self.trim))
            _run(['v4l2-ctl', '-d', self.device, '--set-ctrl', f'{ctrl}={value}'])
            self.wrote[ctrl] = value
        # Sharpness follows the gain just sent. It is deliberately kept out of
        # self.wrote: that is how an outside change is spotted, and sharpness
        # is a value this loop owns outright rather than a trim away from the
        # preset's own.
        if not self.wrote:
            # Nothing reached the camera. Loud, because the alternative is a
            # log full of corrections that never happened.
            log('TRIM NOT APPLIED: no control could be written - the camera '
                'could not be read, so there is no baseline to trim from. '
                'Check that the camera is plugged in and v4l2-ctl works.',
                always=True)
            return 0
        sharp = self.set_sharpness(self.wrote.get('gain'))
        # Saturation, hue and gamma are not trimmed; check nothing else moved
        # them, and pull gamma back down if it was raised.
        self.guard_locked()
        # The light just moved. Whether that costs any sharpness is a question
        # about the picture, so it is answered from the picture rather than
        # assumed: refocus if it has actually gone soft, otherwise hold the
        # preset's focus distance.
        self.maybe_refocus(self.find_snapshot(), self.wrote.get('gain'))

        # And the same controls again, afterwards. What was asked for and
        # what the camera reports a moment later are different facts, and
        # only the second one is what the picture is being taken with.
        if _verify and before:
            asked = dict(self.wrote)
            if sharp is not None:
                asked['sharpness'] = sharp
            try:
                # A moment first: a driver does not always report a new
                # value the instant it is written, and reading too early
                # would record a change that took as one that did not.
                time.sleep(0.4)
                after = _verify.read_controls(self.device)
                _verify.track(f'light meter trim {self.trim:+d}',
                              before, after, asked, self.device)
            except Exception as problem:                       # noqa: BLE001
                log(f'could not record the adjustment: {problem}')
        return len(self.wrote)

    # ---- the picture ----

    # ---- making the snapshots, and keeping only the last 30 ----

    def snap_dir(self):
        """The snapshot folder, when the source is a folder at all. A single
        file or an http URL is somebody else's to manage, so neither is
        written to or pruned."""
        source = self.snapshot
        if not source or source.startswith('http://') or source.startswith('https://'):
            return None
        return None if os.path.isfile(source) else source

    def prune_snapshots(self):
        """Delete the oldest until only SNAP_KEEP are left.

        Everything with a .jpg name in the folder counts, whoever wrote it,
        because the folder is there for these snapshots and nothing else.
        Point the snapshot setting at a folder holding pictures worth keeping
        and they will be pruned too."""
        folder = self.snap_dir()
        if not folder or not os.path.isdir(folder):
            return 0
        shots = glob.glob(os.path.join(folder, '*.jpg')) + \
                glob.glob(os.path.join(folder, '*.jpeg'))
        if len(shots) <= SNAP_KEEP:
            return 0
        shots.sort(key=os.path.getmtime)            # oldest first
        doomed = shots[:len(shots) - SNAP_KEEP]
        for path in doomed:
            try:
                os.remove(path)
            except OSError:
                pass
        return len(doomed)

    def camera_holder(self):
        """Who has the camera open, if anyone.

        This exists because of how the programs actually get used: the
        snapshot grab works when this is started BEFORE the recording
        software, and stops working the moment that software opens the device
        - a V4L2 camera lets one program stream at a time.

        Knowing that is worth more than retrying blindly. If the recorder has
        the camera, running ffmpeg at it every five seconds achieves nothing,
        and there is a real risk in it: if the recorder drops and re-opens its
        stream at the instant our grab lands, our grab wins and the recorder
        fails to come back. So when something else holds the device, nothing
        is sent to it at all.
        """
        if not os.path.exists(self.device):
            return None
        mine = os.getpid()

        if shutil.which('fuser'):
            try:
                out = subprocess.run(['fuser', '-v', self.device], capture_output=True,
                                     text=True, timeout=10)
                text = (out.stdout or '') + (out.stderr or '')
            except (OSError, subprocess.SubprocessError):
                text = ''
            names = set()
            for line in text.splitlines():
                # fuser -v lines look like:  USER  PID  ACCESS  COMMAND
                found = re.match(r'^\s*\S*\s+(\d+)\s+\S+\s+(\S+)\s*$', line)
                if found and int(found.group(1)) != mine:
                    names.add(f'{found.group(2)} (pid {found.group(1)})')
            if names:
                return ', '.join(sorted(names))

        if shutil.which('lsof'):
            out = _run(['lsof', '-F', 'cp', self.device])
            pid, names = None, set()
            for line in out.splitlines():
                if line.startswith('p'):
                    pid = line[1:]
                elif line.startswith('c') and pid and int(pid) != mine:
                    names.add(f'{line[1:]} (pid {pid})')
            if names:
                return ', '.join(sorted(names))
        return None

    def sampling_wanted(self):
        """Whether to open the camera this pass.

        Off unless the stream sampling switch is on, and then no more often
        than its own interval - which is minutes, not the twenty seconds the
        meter runs at, because a frame off the stream is a bonus sample and
        not the thing the correction depends on.
        """
        if read_settings is None:
            return False
        try:
            settings = read_settings()
        except Exception:                                      # noqa: BLE001
            return False
        if not settings.get(KEY_SAMPLE_STREAM):
            return False
        try:
            every = int(settings.get(KEY_SAMPLE_SECONDS,
                                     DEFAULT_SAMPLE_SECONDS))
        except (TypeError, ValueError):
            every = DEFAULT_SAMPLE_SECONDS
        return time.time() - getattr(self, 'last_sample', 0) >= max(30, every)

    def capture_snapshot(self):
        """Grab one frame into the snapshot folder with ffmpeg. Returns the
        path, or None when the camera is busy, ffmpeg is missing, or there is
        no folder to write to.

        The grab only works while the camera is free. A V4L2 camera lets one
        program stream at a time, so if the recording software has the device
        this fails with a busy error and there is nothing to be done about it:
        the loop then measures whatever stills that software writes, which is
        the arrangement this was built for. The failure is noted once and then
        left alone, because a warning every five seconds all night is worse
        than the problem."""
        folder = self.snap_dir()
        if not folder:
            return None
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            return None

        if not self.capture_checked:
            self.capture_checked = True
            if shutil.which('ffmpeg') is None:
                self.capture_note = 'ffmpeg is not installed, so no snapshots can be taken here'
                log(self.capture_note)
                return None
        # The note is cleared each attempt, so a camera that was busy a minute
        # ago is tried again rather than written off for the rest of the
        # session. ffmpeg missing is the one thing that will not fix itself.
        if 'not installed' in self.capture_note:
            return None

        # Something else has the camera. Say who, leave the device alone, and
        # look again in a minute rather than every five seconds.
        holder = self.camera_holder()
        if holder:
            self.capture_note = (f'{holder} has {self.device} - snapshots come from the '
                                 'recording software while it runs')
            log(f'camera held by {holder}, not grabbing')
            self.busy_until = time.time() + 60
            return None
        if time.time() < self.busy_until:
            return None

        self.capture_note = ''

        name = os.path.join(folder, time.strftime('snapshot-%Y%m%d-%H%M%S.jpg'))
        # ffmpeg's own error is kept rather than thrown away. It used to be
        # swallowed and every failure was reported as a busy camera, which is
        # only one of the things that can go wrong here - the device can be
        # missing, the user can be outside the video group, the driver can
        # refuse the pixel format. Guessing at which one wastes an evening;
        # the actual line says.
        try:
            done = subprocess.run(
                ['ffmpeg', '-nostdin', '-loglevel', 'error', '-f', 'v4l2',
                 '-i', self.device, '-frames:v', '1', '-q:v', '2', '-y', name],
                capture_output=True, text=True, timeout=20)
            problem = done.stderr or ''
        except (OSError, subprocess.SubprocessError) as oops:      # noqa: BLE001
            problem = str(oops)

        if not os.path.isfile(name) or not os.path.getsize(name):
            if os.path.exists(name):
                try:
                    os.remove(name)
                except OSError:
                    pass
            # The last line is the one that says what actually failed.
            why = ''
            for line in reversed(problem.splitlines()):
                if line.strip():
                    why = line.strip()
                    break
            low = why.lower()
            if 'busy' in low:
                self.capture_note = (f'camera is busy ({self.device} is held by something '
                                     'else), so stills come from the recording software')
            elif 'permission' in low or 'denied' in low:
                self.capture_note = (f'no permission to open {self.device} - add this user '
                                     'to the video group')
            elif 'no such file' in low:
                self.capture_note = f'{self.device} does not exist - is the camera plugged in?'
            elif why:
                self.capture_note = f'ffmpeg could not take a snapshot: {why}'
            else:
                self.capture_note = f'ffmpeg could not take a snapshot from {self.device}'
            log(f'snapshot failed: {self.capture_note}' + (f' [ffmpeg: {why}]' if why else ''))
            return None
        gone = self.prune_snapshots()
        if gone:
            log(f'deleted {gone} snapshots over the {SNAP_KEEP} kept')
        if self.capture_note:
            log(f'snapshots are being taken again: {name}')
        self.capture_note = ''
        return name

    def prepare_snapshots(self):
        """Make the snapshot folder if it is not there and bring it down to
        size, so the first run starts from a clean folder of at most 30."""
        folder = self.snap_dir()
        if not folder:
            return 'light meter: no snapshot folder set'
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            log(f'cannot create the snapshot folder {folder}')
            return f'light meter: cannot create {folder}'
        gone = self.prune_snapshots()
        line = (f'light meter: snapshots in {folder}, keeping the newest {SNAP_KEEP}'
                + (f' (deleted {gone} older)' if gone else ''))
        log(line, always=True)
        return line

    def find_snapshot(self):
        """Find a still to measure. Returns a path, or None if there is
        nothing usable."""
        source = self.snapshot
        if not source:
            source = next((d for d in SNAPSHOT_GUESSES if os.path.isdir(d)), '')
        if not source:
            return None

        if source.startswith('http://') or source.startswith('https://'):
            _run(['curl', '-s', '-m', '4', '-o', self.tmp, source])
            return self.tmp if os.path.isfile(self.tmp) and os.path.getsize(self.tmp) else None

        if os.path.isdir(source):
            files = glob.glob(os.path.join(source, '*.jpg')) + \
                    glob.glob(os.path.join(source, '*.jpeg'))
            if not files:
                return None
            newest = max(files, key=os.path.getmtime)
            # A still from last week says nothing about the light right now.
            if time.time() - os.path.getmtime(newest) > 600:
                return None
            return newest

        return source if os.path.isfile(source) else None

    def measure(self, path):
        """Average grey level out of 255, and how much of the frame has gone
        to pure white and to pure black.

        The mean is taken on a quarter size copy, which is both faster and
        steadier - averaging is exactly what shrinking an image does. The
        clipping figures are taken at full size, where a count of individual
        blown pixels still means something."""
        if not path:
            return None
        magick = _magick()
        if not magick:
            return None
        # The locked-on target, when there is one. Everything below is
        # unchanged - it just runs on a crop of the target and its
        # surroundings instead of on the whole frame, which is the whole
        # point of locking on: the exposure is then decided by the car, and
        # the floodlight at the far end of the yard gets a vote proportional
        # to how much of the car it lights rather than to how bright it is.
        self.lock_note = ''
        if webcam_lock is not None and webcam_lock.locked():
            try:
                found = webcam_lock.region_from_file(path)
            except Exception as problem:                      # noqa: BLE001
                found = None
                log(f'could not find the locked target: {problem}')
            if found and found.get('path'):
                path = found['path']
                self.lock_note = (f"metering the locked {found['label']} "
                                  f"({found['how']})")
            else:
                self.lock_note = ('locked target not found in this frame - '
                                  'metering the whole frame')
        mean = _run([magick, path, '-colorspace', 'Gray', '-resize', '25%',
                     '-format', '%[fx:mean*255]', 'info:'])
        found = re.search(r'[\d.]+', mean)
        if not found:
            return None
        hot = _run([magick, path, '-colorspace', 'Gray', '-threshold', '97%',
                    '-format', '%[fx:mean]', 'info:'])
        warm = _run([magick, path, '-colorspace', 'Gray', '-threshold', '3%',
                     '-format', '%[fx:mean]', 'info:'])
        hot_found = re.search(r'[\d.]+', hot)
        warm_found = re.search(r'[\d.]+', warm)
        hot_fraction = float(hot_found.group()) if hot_found else 0.0
        # -threshold keeps what is ABOVE the level, so the black fraction is
        # what is left once the pixels above 3% are taken out.
        cold_fraction = 1 - (float(warm_found.group()) if warm_found else 1.0)
        return float(found.group()), hot_fraction, cold_fraction

    # ---- the loop ----

    def pass_once(self):
        """One pass. Call it as often as you like; it does its work every
        METER_EVERY seconds and returns None the rest of the time. Returns a
        status line when it has something to say."""
        now = time.time()
        if now - self.last_pass < METER_EVERY:
            return None
        self.last_pass = now

        # Normally already done at startup; this covers a camera that was
        # not plugged in then.
        if not self.ranges:
            self.learn_ranges()

        # Re-learn the baseline if anything other than this loop moved the
        # controls - a preset firing, or a hand on a slider. This is how the
        # correction stays a correction to whatever is in force rather than
        # drifting off on its own.
        #
        # Two units of slack, because a driver may round a control to its own
        # step size. Without the slack, a value that comes back as 99 after 100
        # was sent would look like an outside change, the baseline would be
        # re-learnt with the trim already inside it, and the next correction
        # would trim again from there: a ratchet that walks the picture off in
        # one direction a step at a time.
        have = self.read_controls()
        if have:
            outside = any(ctrl not in self.wrote or abs(self.wrote[ctrl] - value) > 2
                          for ctrl, value in have.items())
            if outside:
                self.base = dict(have)
                self.wrote = dict(have)
                self.trim = 0
                self.last_dir = 0
                self.means = []
                self.hold = 1      # let the new preset show up before correcting
                # The preset just overwrote sharpness with its own value, so
                # put the wanted sharpness back for the gain it runs at.
                sharp = self.set_sharpness(have.get('gain'))
                # The preset owns saturation, hue, gamma and the focus
                # distance, so whatever it just set them to is the new
                # reference the guard holds them at.
                self.learn_locked()
                # A preset just changed the light. Measure the picture before
                # deciding whether the focus needs anything: a preset change
                # does not make a sharp picture blurry, and the old
                # refocus-on-every-change is what used to walk the focus off
                # the distance the preset was set up for.
                self.maybe_refocus(self.find_snapshot(), have.get('gain'))
                # A preset or a hand on a slider just changed the light. This
                # is exactly the moment worth filing: the scene is the same and
                # the exposure is different, which is how the training set gets
                # the camera's own before and after instead of a guess at one.
                self.harvest_now = True
                log('learnt new baseline: '
                    + ' '.join(f'{k}={v}' for k, v in sorted(have.items())), always=True)
                return ('light meter: learnt the current preset as the baseline'
                        + (f', sharpness set to {sharp}' if sharp is not None else ''))
            # Rounding, not an outside change: keep the baseline and the trim,
            # and accept what the driver actually holds as what was written.
            self.wrote.update(have)

        if self.hold > 0:
            self.hold -= 1
            return f'light meter: trim {self.trim:+d}, waiting for the picture to catch up'

        # The stills the recorder writes are the default source, and they
        # cost nothing: something else is writing them anyway. Going to the
        # camera ourselves is the option on top, off unless asked for.
        #
        # It used to grab every pass, unasked. On this setup that meant
        # opening a device the recorder holds, every twenty seconds, all day,
        # to be told it was busy - noise, and contention for a camera that
        # only one program can stream at a time. Where nothing else holds the
        # camera the grab is useful, so it stays available; it is just not
        # the default any more, and it has an interval of its own rather than
        # running at the meter's rate.
        if self.sampling_wanted():
            self.capture_snapshot()
            self.last_sample = time.time()

        frame = self.find_snapshot()
        measured = self.measure(frame)
        if measured is None:
            if not _magick():
                log('ImageMagick is not installed, so the picture cannot be measured')
                return 'light meter: ImageMagick is not installed, so the picture cannot be measured'
            return ('light meter: no snapshot to measure, the preset stands as it is'
                    + (f' - {self.capture_note}' if self.capture_note else ''))
        mean, hot, cold = measured

        # The enhanced copy of this still, when either switch is on. This is
        # not part of the correction - it changes no control and the recording
        # is untouched by it - so it happens here, once the frame is in hand
        # and measured, and whatever it says is added to the status line.
        enhanced = self.enhance(frame, mean=mean)

        # File this still into the brain's training set, and retrain on it once
        # a day. Both are deliberately last and wrapped: they read and write
        # files and send nothing to the camera, so neither can affect the
        # correction loop above.
        self.harvest(frame, mean=mean)

        # Median of the last three keeps a passing pair of headlights from
        # dragging the picture darker for the next minute.
        self.means.append(mean)
        self.means = self.means[-3:]
        middle = sorted(self.means)[len(self.means) // 2]

        # Which way to go. The clipping checks come first: they are the too
        # much white light and too much darkness tests, and either can override
        # a mean that looks perfectly reasonable on its own.
        direction = 0
        why = 'picture is where it should be'
        if hot > MAX_HOT and middle > self.target - MEAN_TOL:
            direction, why = -1, f'{hot * 100:.1f}% of the frame blown to white'
        elif cold > MAX_COLD and middle < self.target + MEAN_TOL:
            direction, why = 1, f'{cold * 100:.0f}% of the frame crushed to black'
        elif middle > self.target + MEAN_TOL:
            direction, why = -1, 'too bright'
        elif middle < self.target - MEAN_TOL:
            direction, why = 1, 'too dark'

        # A security light over black tarmac holds more range than the sensor
        # can take, and then both checks fail at once: brightening blows the
        # light further, darkening loses the car. Rather than run off to one end
        # of the trim chasing an impossible picture, it stops and says so.
        if direction > 0 and hot > MAX_HOT * 3:
            direction = 0
            why = 'high contrast scene - cannot lift the dark without blowing the light'
        elif direction < 0 and cold > MAX_COLD * 2:
            direction = 0
            why = 'high contrast scene - cannot tame the light without losing the dark'

        # Two samples in a row have to agree before anything moves.
        if direction != 0 and direction == self.last_dir:
            at_limit = (direction > 0 and self.trim >= TRIM_MAX) or \
                       (direction < 0 and self.trim <= -TRIM_MAX)
            if at_limit:
                why += ' - trim at its limit, the preset itself needs changing'
            else:
                sent = self.apply_trim(self.trim + direction)
                self.hold = METER_SETTLE
                self.means = []
                if sent:
                    why += f' - trimmed to {self.trim:+d}'
                    log(f'trim {self.trim:+d}: mean {middle:.0f} of target '
                        f'{self.target}, white {hot * 100:.1f}%, black '
                        f'{cold * 100:.0f}% ({why})', always=True)
                else:
                    # Say it here too. The status line is what is on the
                    # screen, and it should never claim a correction that
                    # did not reach the camera.
                    why += (' - COULD NOT TRIM: nothing could be written to '
                            'the camera')
        self.last_dir = direction

        return (f'light meter: trim {self.trim:+d}, mean {middle:.0f} of target '
                f'{self.target}, white {hot * 100:.1f}%, black {cold * 100:.0f}% - {why}'
                + (f' | sharpness score {self.blur:.0f}' if self.blur else '')
                + (f' | {self.lock_note}' if self.lock_note else '')
                + (f' | {enhanced}' if enhanced else ''))

    def sleep_with_metering(self, seconds, report=print):
        """Sleep, metering the picture every METER_EVERY seconds along the way.

        The scheduler's own loop sleeps in one long stretch, which is fine for
        watching a clock and useless for watching the light. This hands the
        same wait back as a series of short ones with a measurement in each."""
        deadline = time.time() + seconds
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            time.sleep(min(METER_EVERY, remaining))
            line = self.pass_once()
            if line and report:
                report(line)
