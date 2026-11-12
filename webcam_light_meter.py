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
except ImportError:                                            # noqa: BLE001
    Enhancer = None

# Measure every 5 seconds, then wait two samples after a correction.
#
# Each change takes a moment to reach the next still the camera software
# writes, and a loop that acts faster than its own feedback arrives will
# oscillate - the picture breathes in and out all night instead of settling.
# Ten seconds of patience is what buys the settling.
METER_EVERY = 30
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
# gain has lifted everything. Saturation comes DOWN as the trim goes up, which
# is the "a little bit less colour at night" part - colour is never switched
# off at any trim, there is just less of it when the gain is high and most of
# what is left is noise.
TRIM_RULE = {
    'gain': 0.120,
    'brightness': 0.025,
    'contrast': 0.030,
    'saturation': -0.025,
    'backlight_compensation': 0.200,
}

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
SHARP_DARK = 0.80      # at the top of the gain range
SHARP_KNEE = 0.50      # the gain fraction where the easing off starts

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
        """Send the given trim, worked out from the preset's own baseline."""
        self.trim = max(-TRIM_MAX, min(TRIM_MAX, trim))
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
        self.set_sharpness(self.wrote.get('gain'))
        # The light just moved, so give the autofocus a fresh start on it.
        self.refocus()

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
                # A preset just changed the light, so refocus for it as well.
                self.refocus()
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

        # Take one of our own first. When the recording software has the
        # camera this does nothing and the still it wrote is used instead.
        self.capture_snapshot()

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
                self.apply_trim(self.trim + direction)
                self.hold = METER_SETTLE
                self.means = []
                why += f' - trimmed to {self.trim:+d}'
                log(f'trim {self.trim:+d}: mean {middle:.0f} of target {self.target}, '
                    f'white {hot * 100:.1f}%, black {cold * 100:.0f}% ({why})', always=True)
        self.last_dir = direction

        return (f'light meter: trim {self.trim:+d}, mean {middle:.0f} of target '
                f'{self.target}, white {hot * 100:.1f}%, black {cold * 100:.0f}% - {why}'
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
