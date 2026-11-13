#!/usr/bin/env python3
"""The recorder: one ffmpeg, one camera, three outputs.

This is the piece that makes the webcam program do what a security recorder
does - keep the video. Up to four cameras, each with one ffmpeg of its own
holding that camera, because a camera is one device and everything else in
this program wants a frame from it:

  * the archive itself - 1280x720 at 30fps, cut into segments on disk,
  * a still a minute into the snapshot folder, which is what the light meter,
    the enhancer and the brain already read and train from,
  * a preview frame ten times a second, which is what the live video window in
    webcam_gui.py draws.

One ffmpeg with three outputs off one input gives all three without ever
opening a camera twice. Four cameras means four of those, each writing into
its own cam1..cam4 folder under the archive, with one janitor across the lot -
the loop cap is a disk limit, and there is one disk. Nothing else in this program needs to grab a frame
while the recorder is up, and the light meter already treats a busy camera as
"use the newest snapshot instead", so it keeps working untouched.

Camera *controls* are a different matter and still work while recording:
v4l2-ctl sets controls on a streaming device, so every preset and every
automatic light adjustment keeps applying live, mid-recording.

IMPORTANT, the one thing that will bite: only one program at a time can
stream a USB camera. Nothing else may be capturing this device when the
recorder starts, or it will fail to open the camera and say so.

Disk: the archive loops. Two limits hold it, and the janitor deletes
oldest-first until both are satisfied:

  * the archive's own size cap - 250 GB by default, or 100 or 50 - which is
    what makes this a loop recorder: once it is full, the oldest hour goes to
    make room for the newest,
  * and a floor of free space left on the filesystem, 25 GB by default, so
    that a disk shared with everything else never fills up.

Neither ever touches the segment currently being written, or anything in the
clips folder - exported clips live outside the archive precisely so that the
loop cannot reach them.

    python3 webcam_record.py            # record every enabled camera
    python3 webcam_record.py preview    # live preview only, nothing kept
    python3 webcam_record.py status     # what is running, what is on disk
    python3 webcam_record.py janitor    # recycle the oldest once, then exit
    python3 webcam_record.py devices    # every camera node that can be taped
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

try:
    import webcam_bootstrap
except ImportError:
    pass
else:
    webcam_bootstrap.ensure('ffmpeg')

try:
    from webcam_errors import log_error
except Exception:                                                 # noqa: BLE001
    def log_error(text):
        print(text, file=sys.stderr)


# ---- where everything lives ----
#
# No user name is written into any of these: expanduser answers for whoever is
# actually running the program, which is what makes this work the same from a
# terminal, from cron and from a systemd unit.

HOME = os.path.expanduser('~')
ARCHIVE_DIR = os.environ.get(
    'WEBCAM_ARCHIVE', os.path.join(HOME, 'Videos', 'webcam-archive'))
CLIPS_DIR = os.environ.get(
    'WEBCAM_CLIPS', os.path.join(HOME, 'Videos', 'webcam-clips'))
SNAPSHOT_DIR = os.environ.get(
    'WEBCAM_SNAPSHOTS', os.path.join(HOME, 'Pictures', 'webcam-sample-images'))
STATE_DIR = os.environ.get(
    'WEBCAM_STATE', os.path.join(HOME, '.cache', 'webcam'))
CONF_DIR = os.path.join(HOME, '.config', 'webcam')
CONF_FILE = os.path.join(CONF_DIR, 'recorder.conf')

PREVIEW_FILE = os.path.join(STATE_DIR, 'preview.jpg')
PID_FILE = os.path.join(STATE_DIR, 'recorder.pid')
STATUS_FILE = os.path.join(STATE_DIR, 'recorder.json')

DEVICE = os.environ.get('WEBCAM_DEVICE', '/dev/video0')

# The standard asked for, and the standard that gets written. 1280x720 at 30
# frames a second, in h264 inside mp4 - which plays in anything, trims without
# re-encoding, and is what the archive index expects to find.
WIDTH = 1280
HEIGHT = 720
FPS = 30
SEGMENT_SECONDS = 300          # five minutes a file: small enough to delete
                               # one without losing much, large enough that a
                               # day is 288 files rather than thousands
KEEP_FREE_GB = 25.0            # free space left on the disk after recording
MAX_ARCHIVE_GB = 250.0         # the loop: how much the archive may hold
                               # before the oldest segments are recycled
ARCHIVE_CAPS = (50.0, 100.0, 250.0)
MAX_CAMERAS = 4                # four is the ceiling on purpose: four 720p30
                               # h264 encodes is already a real load on a CPU,
                               # and four of them on one USB controller is
                               # about where the bandwidth runs out too
STILL_EVERY_SECONDS = 60       # one still a minute into the snapshot folder
PREVIEW_FPS = 4                # what the live window draws. Four a second
                               # reads as live to an eye watching a driveway,
                               # and each frame skipped is a decode and a jpeg
                               # encode not paid for.
PREVIEW_WIDTH = 640            # and at 640 wide, not the camera's full size:
                               # the panes are smaller than that anyway, and
                               # scaling down before the jpeg is cheaper than
                               # encoding a full-size one

SEGMENT_NAME = '%Y-%m-%d_%H-%M-%S.mp4'
SEGMENT_RE = re.compile(r'^(\d{4})-(\d\d)-(\d\d)_(\d\d)-(\d\d)-(\d\d)\.mp4$')


DEFAULTS = {
    # The cameras, in order, as device paths. The first is the one everything
    # that predates multiple cameras means by "the camera", so it stays
    # /dev/video0 by default and keeps its own settings where they were.
    'cameras': [DEVICE],
    'device': DEVICE,          # camera 1, kept for anything that reads it
    'width': WIDTH,
    'height': HEIGHT,
    'fps': FPS,
    'segment_seconds': SEGMENT_SECONDS,
    'keep_free_gb': KEEP_FREE_GB,
    'max_archive_gb': MAX_ARCHIVE_GB,
    'archive_dir': ARCHIVE_DIR,
    'encoder': 'copy',         # the default, and the reason the recorder is
                               # cheap: 'copy' writes the camera's own mjpeg
                               # frames straight to disk, so nothing is
                               # encoded at all. 'h264' re-encodes to roughly
                               # a quarter the disk, and costs a core per
                               # camera to do it.
    'crf': 23,
    'still_every_seconds': STILL_EVERY_SECONDS,
    'preview_fps': PREVIEW_FPS,
    'preview_width': PREVIEW_WIDTH,
    'defaults_version': 2,     # bumped when a default changes in a way that
                               # should reach a conf file already on disk
}


def read_settings():
    """What the recorder was last told to do. Missing keys fall back to the
    defaults above, so a conf file written before an option existed still
    reads cleanly."""
    settings = dict(DEFAULTS)
    try:
        with open(CONF_FILE, 'r', encoding='utf-8') as handle:
            saved = json.load(handle)
    except (OSError, ValueError):
        return settings
    if isinstance(saved, dict):
        for key, value in saved.items():
            if key in settings:
                settings[key] = value
        # The cheap-recording defaults arrived after this conf format did, so
        # a conf written before them still says h264 and a ten-a-second
        # preview - which is the expensive combination the new defaults exist
        # to avoid. A conf from before that change is brought forward once,
        # and marked, so a later deliberate choice of h264 is left alone.
        if int(saved.get('defaults_version', 0)) < 2:
            settings['encoder'] = DEFAULTS['encoder']
            settings['preview_fps'] = DEFAULTS['preview_fps']
            settings['preview_width'] = DEFAULTS['preview_width']
    settings['cameras'] = clean_cameras(settings.get('cameras'),
                                        settings.get('device'))
    settings['device'] = settings['cameras'][0]
    return settings


def clean_cameras(cameras, fallback=None):
    """A sane list of one to four device paths, whatever was in the file.

    Duplicates are dropped rather than accepted: two ffmpegs on one device is
    the "resource busy" failure, and it is better to refuse the duplicate here
    than to show it failing in the window.
    """
    if isinstance(cameras, str):
        cameras = [cameras]
    found = []
    for device in (cameras or []):
        device = str(device).strip()
        if device and device not in found:
            found.append(device)
    if not found:
        found = [fallback or DEVICE]
    return found[:MAX_CAMERAS]


def camera_folder(settings, index):
    """Where camera `index` (1-based) writes its segments.

    Camera 1 writes into the archive root when it is the only camera, so an
    archive recorded before this existed is still exactly where it was and
    still plays; add a second camera and everything moves into cam1..cam4
    folders, with the scan treating loose files in the root as camera 1.
    """
    if len(settings.get('cameras', [])) <= 1 and index == 1:
        return settings['archive_dir']
    return os.path.join(settings['archive_dir'], f'cam{int(index)}')


def preview_file(index):
    return os.path.join(STATE_DIR, f'preview-cam{int(index)}.jpg')


def write_settings(**changes):
    settings = read_settings()
    settings.update({k: v for k, v in changes.items() if k in DEFAULTS})
    settings['cameras'] = clean_cameras(settings.get('cameras'),
                                        settings.get('device'))
    settings['device'] = settings['cameras'][0]
    os.makedirs(CONF_DIR, exist_ok=True)
    try:
        with open(CONF_FILE, 'w', encoding='utf-8') as handle:
            json.dump(settings, handle, indent=2, sort_keys=True)
    except OSError as problem:
        log_error(f'recorder settings not saved: {problem}')
    return settings


def ensure_dirs(settings=None):
    settings = settings or read_settings()
    folders = [settings['archive_dir'], CLIPS_DIR, SNAPSHOT_DIR, STATE_DIR,
               CONF_DIR]
    folders += [camera_folder(settings, n + 1)
                for n in range(len(settings.get('cameras', [])))]
    for folder in folders:
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as problem:
            log_error(f'cannot create {folder}: {problem}')


# ---- what the camera can actually do ----

def list_devices():
    """Every /dev/video* that reports a capture format, with its name.

    A USB webcam shows up as two or three /dev/video nodes and only the first
    of them actually hands over frames; the others are metadata nodes that
    look identical until you try to stream them. Anything that lists no
    capture format is left out of the list, so the device picker in the window
    only offers nodes that can really be recorded.
    """
    found = []
    for name in sorted(os.listdir('/dev')):
        if not name.startswith('video'):
            continue
        path = os.path.join('/dev', name)
        formats = _run(['v4l2-ctl', '-d', path, '--list-formats'])
        if '[0]' not in formats and 'Pixel Format' not in formats:
            continue
        label = path
        card = _run(['v4l2-ctl', '-d', path, '--info'])
        match = re.search(r'Card type\s*:\s*(.+)', card)
        if match:
            label = f'{path}  -  {match.group(1).strip()}'
        found.append((path, label))
    return found or [(DEVICE, DEVICE)]


def _run(args, timeout=10):
    try:
        done = subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout)
        return (done.stdout or '') + (done.stderr or '')
    except (OSError, subprocess.SubprocessError):
        return ''


def probe_input(device, width, height, fps):
    """Pick the input format to ask the camera for.

    An ONN-class USB webcam gives 1280x720 at 30fps over MJPEG and only
    640x480 or so at 30fps over raw YUYV - ask for raw at 720p and the driver
    quietly drops you to 10 or 5 frames a second. So MJPEG is asked for first
    whenever the camera lists it at the wanted size, and raw is the fallback.
    """
    listing = _run(['v4l2-ctl', '-d', device, '--list-formats-ext'])
    wants = f'{width}x{height}'
    if not listing:
        return 'mjpeg'
    blocks = re.split(r"\[\d+\]:\s*'", listing)
    mjpeg_ok = raw_ok = False
    for block in blocks[1:]:
        fourcc = block[:4].strip().lower()
        if wants not in block:
            continue
        if fourcc in ('mjpg', 'jpeg'):
            mjpeg_ok = True
        elif fourcc in ('yuyv', 'nv12', 'yu12'):
            raw_ok = True
    if mjpeg_ok:
        return 'mjpeg'
    if raw_ok:
        return 'yuyv422'
    return 'mjpeg'


def _image2_atomic():
    """ffmpeg's image2 muxer can write to a temporary name and rename into
    place, which is the difference between the live window reading a finished
    frame and reading half of one. Older builds do not have the option, so it
    is only used when this build lists it."""
    if 'atomic_writing' in _run(['ffmpeg', '-hide_banner', '-h',
                                 'muxer=image2']):
        return ['-atomic_writing', '1']
    return []


# ---- the command ----

def build_command(settings, record=True, index=1):
    """One input, up to three outputs.

    The segment muxer does the cutting, with -strftime so that each file is
    named for the moment it starts - which is what makes the archive index
    able to read a file's start time off its name without opening it, and what
    makes a date search cheap.
    """
    cameras = clean_cameras(settings.get('cameras'), settings.get('device'))
    index = max(1, min(len(cameras), int(index)))
    device = cameras[index - 1]
    width, height, fps = int(settings['width']), int(settings['height']), int(settings['fps'])
    input_format = probe_input(device, width, height, fps)
    cmd = [
        'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error',
        '-use_wallclock_as_timestamps', '1',
        '-f', 'v4l2',
        '-input_format', input_format,
        '-framerate', str(fps),
        '-video_size', f'{width}x{height}',
        '-i', device,
    ]

    if record:
        folder = camera_folder(settings, index)
        os.makedirs(folder, exist_ok=True)
        if settings.get('encoder') == 'copy' and input_format == 'mjpeg':
            # No re-encode at all: the camera's own mjpeg frames land in the
            # file. Costs almost no CPU and roughly four times the disk.
            video = ['-c:v', 'copy']
        else:
            # A raw-only camera cannot be copied - there is nothing in the
            # stream worth keeping as-is - so it is encoded even when copy
            # was asked for. In that case ultrafast, because the point of
            # asking for copy was to not spend a core on this.
            preset = ('ultrafast' if settings.get('encoder') == 'copy'
                      else 'veryfast')
            video = [
                '-c:v', 'libx264', '-preset', preset,
                '-crf', str(int(settings.get('crf', 23))),
                '-pix_fmt', 'yuv420p',
                '-g', str(fps * 2),
            ]
        cmd += ['-map', '0:v'] + video + [
            '-an',
            '-f', 'segment',
            '-segment_time', str(int(settings['segment_seconds'])),
            '-segment_format', 'mp4',
            '-reset_timestamps', '1',
            '-strftime', '1',
            os.path.join(folder, SEGMENT_NAME),
        ]

        # A still a minute, named for its moment, into the folder the light
        # meter and the brain already read. This is what keeps automatic light
        # adjustment and the enhancer working while the camera is held open.
        #
        # Camera 1 only. The brain is trained on one camera's view of one
        # scene, and mixing four views into one corpus would teach it the
        # average of four scenes - which is not a correction for any of them.
        # The other cameras still get their own stills, in their own folders,
        # for anything that wants to look at them.
        every = max(5, int(settings.get('still_every_seconds',
                                        STILL_EVERY_SECONDS)))
        stills = (SNAPSHOT_DIR if index == 1
                  else os.path.join(SNAPSHOT_DIR, f'cam{index}'))
        os.makedirs(stills, exist_ok=True)
        cmd += [
            '-map', '0:v', '-vf', f'fps=1/{every}',
            '-q:v', '3', '-strftime', '1', '-f', 'image2',
        ] + _image2_atomic() + [
            os.path.join(stills, 'webcam-%Y%m%d-%H%M%S.jpg'),
        ]

    # The preview the live window draws, overwritten in place.
    os.makedirs(STATE_DIR, exist_ok=True)
    #
    # Scaled down and slowed first, then encoded. The order matters: fps
    # before scale throws frames away before any work is done on them, and
    # scale before the jpeg encoder means the encoder is handed a 640-wide
    # picture rather than a 1280-wide one. On a copy archive this branch is
    # the only real work the process does.
    preview_w = max(160, int(settings.get('preview_width', PREVIEW_WIDTH)))
    cmd += [
        '-map', '0:v',
        '-vf', (f"fps={int(settings.get('preview_fps', PREVIEW_FPS))},"
                f"scale={preview_w}:-2"),
        '-q:v', '7', '-update', '1', '-f', 'image2',
    ] + _image2_atomic() + [preview_file(index)]
    return cmd


# ---- the disk ----

def free_gb(path):
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return 0.0
    return usage.free / (1024.0 ** 3)


def segments(folder=None):
    """Every segment in the archive, oldest first, as (path, start, size).

    The start time comes off the file name, which is why -strftime is worth
    having: a thousand files sort by date without a thousand ffprobe calls.
    """
    folder = folder or read_settings()['archive_dir']
    found = []
    for root, _dirs, files in os.walk(folder):
        for name in files:
            match = SEGMENT_RE.match(name)
            path = os.path.join(root, name)
            if not match:
                continue
            try:
                stamp = time.mktime((
                    int(match.group(1)), int(match.group(2)), int(match.group(3)),
                    int(match.group(4)), int(match.group(5)), int(match.group(6)),
                    0, 0, -1))
                size = os.path.getsize(path)
            except (OSError, ValueError, OverflowError):
                continue
            found.append((path, stamp, size))
    found.sort(key=lambda row: row[1])
    return found


def archive_size_gb(folder=None):
    return sum(row[2] for row in segments(folder)) / (1024.0 ** 3)


def free_space(settings=None, verbose=False):
    """Recycle the oldest segments until both disk limits are satisfied.

    This is what makes the archive a loop rather than something that grows
    until the disk is gone. Two independent limits, either of which can be the
    one that bites:

      * the archive may hold at most max_archive_gb (250 by default),
      * and at least keep_free_gb must stay free on the filesystem (25).

    Deleting stops at the newest segment whatever the limits say, because
    ffmpeg is still writing that one; deleting it would not free meaningful
    space and would take the recording down with it. Nothing outside the
    archive folder is ever touched, which is why exported clips are written
    somewhere else.
    """
    settings = settings or read_settings()
    folder = settings['archive_dir']
    want_free = float(settings.get('keep_free_gb', KEEP_FREE_GB))
    cap = float(settings.get('max_archive_gb', MAX_ARCHIVE_GB))
    if not os.path.isdir(folder):
        return 0, 0.0
    removed = 0
    freed = 0.0
    rows = segments(folder)

    def over_limit():
        held = sum(row[2] for row in rows) / (1024.0 ** 3)
        if cap > 0 and held > cap:
            return f'archive is {held:.1f} GB, over its {cap:.0f} GB cap'
        if free_gb(folder) < want_free:
            return (f'only {free_gb(folder):.1f} GB free, under the '
                    f'{want_free:.0f} GB floor')
        return ''

    while rows:
        reason = over_limit()
        if not reason:
            break
        if len(rows) <= 1:
            log_error(
                f'archive janitor: {reason}, and nothing old enough to '
                f'delete in {folder} - the disk needs attention, or lower '
                'the free-space floor')
            break
        path, _stamp, size = rows.pop(0)
        try:
            os.remove(path)
        except OSError as problem:
            log_error(f'archive janitor could not delete {path}: {problem}')
            continue
        removed += 1
        freed += size / (1024.0 ** 3)
        if verbose:
            print(f'deleted {path}')
    # Empty day folders, if the archive is laid out in them.
    for root, dirs, files in os.walk(folder, topdown=False):
        if root != folder and not dirs and not files:
            try:
                os.rmdir(root)
            except OSError:
                pass
    if removed:
        log_error(f'archive janitor recycled {removed} segment(s), '
                  f'{freed:.1f} GB, to stay under a {cap:.0f} GB archive '
                  f'with {want_free:.0f} GB free')
    return removed, freed


# ---- the recorder ----

class Recorder:
    """ffmpeg, kept alive, with the janitor beside it.

    The supervisor thread matters more than it looks: a USB camera that is
    unplugged, or re-enumerated by the kernel, or grabbed by something else
    takes ffmpeg down with it, and a recorder that stays down until somebody
    notices is not a recorder. So an exit is logged and retried with a
    backoff, and the window shows the retry rather than an empty picture with
    no explanation.
    """

    RETRY_START = 3
    RETRY_MAX = 60

    def __init__(self, settings=None, index=1, own_janitor=True):
        self.settings = settings or read_settings()
        self.index = int(index)
        # With several cameras the janitor belongs to the set, not to each
        # recorder: the loop cap is a limit on one disk, and four janitors
        # deciding independently what to delete off one disk would race.
        self.own_janitor = bool(own_janitor)
        self.child = None
        self.recording = False
        self.want_up = False
        self.last_error = ''
        self.started_at = 0.0
        self.retry_in = 0
        self._lock = threading.Lock()
        self._threads = []

    # -- control --

    def start(self, record=True, settings=None):
        self.stop()
        self.settings = settings or read_settings()
        ensure_dirs(self.settings)
        if record and self.own_janitor:
            free_space(self.settings)
        self.recording = bool(record)
        self.want_up = True
        self.last_error = ''
        self._spawn()
        supervisor = threading.Thread(target=self._supervise, daemon=True)
        supervisor.start()
        self._threads = [supervisor]
        if record and self.own_janitor:
            janitor = threading.Thread(target=self._janitor, daemon=True)
            janitor.start()
            self._threads.append(janitor)
        return self.is_running()

    def stop(self):
        self.want_up = False
        with self._lock:
            child = self.child
            self.child = None
        if child and child.poll() is None:
            # ffmpeg finishes the mp4 it is writing when asked politely; kill
            # it outright and the last segment has no moov atom and will not
            # play. So: ask, wait, and only then insist.
            try:
                child.send_signal(signal.SIGINT)
                child.wait(timeout=8)
            except (OSError, subprocess.SubprocessError):
                try:
                    child.kill()
                except OSError:
                    pass
        self._write_status()

    def _spawn(self):
        cmd = build_command(self.settings, record=self.recording,
                            index=self.index)
        try:
            child = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        except OSError as problem:
            self.last_error = f'ffmpeg would not start: {problem}'
            log_error(self.last_error)
            return
        with self._lock:
            self.child = child
        self.started_at = time.time()
        self._write_status()
        threading.Thread(target=self._read_errors, args=(child,),
                         daemon=True).start()

    def _read_errors(self, child):
        """ffmpeg's own words, kept. "Device or resource busy" is the one that
        actually happens - it means another program, or a second copy of this
        one, is holding the camera - and it is worth saying so plainly rather
        than showing a blank window."""
        try:
            for line in iter(child.stderr.readline, ''):
                line = line.strip()
                if not line:
                    continue
                if 'Device or resource busy' in line or 'Permission denied' in line:
                    line += ('  <- another program is holding this camera; '
                             'close it, or pick a different device')
                self.last_error = line
                log_error(f'recorder camera {self.index}: {line}')
        except (OSError, ValueError):
            pass

    def _supervise(self):
        delay = self.RETRY_START
        while self.want_up:
            with self._lock:
                child = self.child
            if child is None:
                time.sleep(1)
                continue
            code = child.poll()
            if code is None:
                delay = self.RETRY_START
                self.retry_in = 0
                time.sleep(1)
                continue
            if not self.want_up:
                return
            log_error(f'camera {self.index} stopped with code {code}; '
                      f'restarting in {delay}s')
            for left in range(delay, 0, -1):
                if not self.want_up:
                    return
                self.retry_in = left
                time.sleep(1)
            self.retry_in = 0
            if self.recording and self.own_janitor:
                free_space(self.settings)
            self._spawn()
            delay = min(self.RETRY_MAX, delay * 2)

    def _janitor(self):
        while self.want_up:
            try:
                free_space(self.settings)
            except Exception as problem:                          # noqa: BLE001
                log_error(f'archive janitor: {problem}')
            self._write_status()
            for _ in range(60):
                if not self.want_up:
                    return
                time.sleep(1)

    # -- what it is doing --

    def is_running(self):
        with self._lock:
            child = self.child
        return bool(child and child.poll() is None)

    def status(self):
        folder = self.settings['archive_dir']
        rows = segments(folder)
        mine = segments(camera_folder(self.settings, self.index))
        cameras = clean_cameras(self.settings.get('cameras'),
                                self.settings.get('device'))
        return {
            'running': self.is_running(),
            'recording': bool(self.recording and self.is_running()),
            'index': self.index,
            'camera_segments': len(mine),
            'camera_gb': round(sum(r[2] for r in mine) / (1024.0 ** 3), 2),
            'preview': preview_file(self.index),
            'device': cameras[min(len(cameras), self.index) - 1],
            'size': f"{self.settings['width']}x{self.settings['height']}",
            'fps': self.settings['fps'],
            'segments': len(rows),
            'archive_gb': round(sum(r[2] for r in rows) / (1024.0 ** 3), 2),
            'free_gb': round(free_gb(folder), 1),
            'keep_free_gb': float(self.settings.get('keep_free_gb',
                                                    KEEP_FREE_GB)),
            'max_archive_gb': float(self.settings.get('max_archive_gb',
                                                      MAX_ARCHIVE_GB)),
            'newest': os.path.basename(rows[-1][0]) if rows else '',
            'oldest': os.path.basename(rows[0][0]) if rows else '',
            'uptime': int(time.time() - self.started_at) if self.started_at else 0,
            'retry_in': self.retry_in,
            'error': self.last_error,
        }

    def _write_status(self):
        """Left on disk so that something that is not this process - a status
        tile, a cron job, the next run of the window - can read what the
        recorder is doing without starting one of its own."""
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = self.status_path() + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as handle:
                json.dump(self.status(), handle, indent=2)
            os.replace(tmp, self.status_path())
        except (OSError, ValueError):
            pass

    def status_path(self):
        if self.index == 1:
            return STATUS_FILE
        return os.path.join(STATE_DIR, f'recorder-cam{self.index}.json')


def read_status(index=1):
    path = (STATUS_FILE if int(index) == 1
            else os.path.join(STATE_DIR, f'recorder-cam{int(index)}.json'))
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


class RecorderSet:
    """One recorder per camera, and one janitor across all of them.

    The set is what the window talks to. It reads the camera list fresh on
    every start, so adding a fourth camera and pressing Start is all that
    adding a fourth camera takes.
    """

    def __init__(self, settings=None):
        self.settings = settings or read_settings()
        self.recorders = []
        self.recording = False
        self.want_up = False

    def cameras(self):
        return clean_cameras(self.settings.get('cameras'),
                             self.settings.get('device'))

    def start(self, record=True):
        self.stop()
        self.settings = read_settings()
        ensure_dirs(self.settings)
        if record:
            free_space(self.settings)
        self.recording = bool(record)
        self.want_up = True
        self.recorders = []
        for number in range(1, len(self.cameras()) + 1):
            recorder = Recorder(self.settings, index=number,
                                own_janitor=False)
            recorder.start(record=record, settings=self.settings)
            self.recorders.append(recorder)
        if record:
            threading.Thread(target=self._janitor, daemon=True).start()
        return any(r.is_running() for r in self.recorders)

    def stop(self):
        self.want_up = False
        for recorder in self.recorders:
            recorder.stop()
        self.recorders = []

    def is_running(self):
        return any(r.is_running() for r in self.recorders)

    def _janitor(self):
        """One janitor for the whole archive, however many cameras feed it.
        free_space walks the archive folder, so cam1..cam4 are all in scope
        and the oldest segment anywhere is the one that goes."""
        while self.want_up:
            try:
                free_space(self.settings)
            except Exception as problem:                          # noqa: BLE001
                log_error(f'archive janitor: {problem}')
            for _ in range(60):
                if not self.want_up:
                    return
                time.sleep(1)

    def statuses(self):
        return [recorder.status() for recorder in self.recorders]

    def status(self):
        """The whole set as one line's worth of facts, for the disk readout
        and anything that does not care which camera is which."""
        rows = self.statuses()
        folder = self.settings['archive_dir']
        held = segments(folder)
        return {
            'running': self.is_running(),
            'recording': bool(self.recording and self.is_running()),
            'cameras': len(self.cameras()),
            'live': sum(1 for row in rows if row['running']),
            'segments': len(held),
            'archive_gb': round(sum(r[2] for r in held) / (1024.0 ** 3), 2),
            'free_gb': round(free_gb(folder), 1),
            'keep_free_gb': float(self.settings.get('keep_free_gb',
                                                    KEEP_FREE_GB)),
            'max_archive_gb': float(self.settings.get('max_archive_gb',
                                                      MAX_ARCHIVE_GB)),
            'oldest': os.path.basename(held[0][0]) if held else '',
            'size': f"{self.settings['width']}x{self.settings['height']}",
            'fps': self.settings['fps'],
            'per_camera': rows,
        }


def main(argv):
    action = (argv[1] if len(argv) > 1 else 'record').lower()
    settings = read_settings()
    ensure_dirs(settings)

    if action == 'janitor':
        removed, freed = free_space(settings, verbose=True)
        print(f'{removed} segment(s) deleted, {freed:.1f} GB freed, '
              f"{free_gb(settings['archive_dir']):.1f} GB free now")
        return 0

    if action == 'status':
        rows = segments(settings['archive_dir'])
        print(f"cameras   {', '.join(settings['cameras'])}")
        print(f"archive   {settings['archive_dir']}")
        print(f'segments  {len(rows)}')
        print(f"on disk   {sum(r[2] for r in rows) / (1024.0 ** 3):.1f} GB")
        print(f"cap       {settings.get('max_archive_gb')} GB "
              '(the loop recycles the oldest past this)')
        print(f"free      {free_gb(settings['archive_dir']):.1f} GB "
              f"(floor {settings.get('keep_free_gb')} GB)")
        for number in range(1, MAX_CAMERAS + 1):
            live = read_status(number)
            if not live:
                continue
            print(f"camera {number}  last seen running={live.get('running')} "
                  f"recording={live.get('recording')} "
                  f"device={live.get('device')} "
                  f"segments={live.get('camera_segments')}")
            if live.get('error'):
                print(f"          last error {live['error']}")
        return 0

    if action == 'devices':
        for path, label in list_devices():
            print(label)
        return 0

    record = action != 'preview'
    recorder = RecorderSet(settings)
    print('recording' if record else 'preview only',
          f"{settings['width']}x{settings['height']} @ {settings['fps']}fps "
          f"from {len(recorder.cameras())} camera(s): "
          + ', '.join(recorder.cameras()))
    if record:
        print(f"segments into {settings['archive_dir']}, looping at "
              f"{settings.get('max_archive_gb')} GB and keeping "
              f"{settings.get('keep_free_gb')} GB free")
    recorder.start(record=record)
    try:
        while True:
            time.sleep(5)
            if recorder.last_error:
                pass
    except KeyboardInterrupt:
        print('\nstopping')
    finally:
        recorder.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
