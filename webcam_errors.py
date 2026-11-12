#!/usr/bin/env python3
"""One place every OpenCV, TensorFlow, detector and correction failure goes.

The correction pipeline is deliberately forgiving: when brain G will not load
it corrects analytically instead, when the detector will not run it skips the
boost, and when OpenCV is missing ImageMagick does the work. That is the right
behaviour at three in the morning - a camera that keeps recording a slightly
worse picture beats a camera that stopped - but it was silent, and a silent
fallback looks exactly like a working pipeline from the outside. The picture
is a bit flat and there is nothing anywhere saying why.

So every one of those fallbacks reports here, with the full traceback, and
both windows tail this file into the terminal pane at the bottom. Nothing
changes about what the pipeline *does*; it stops being quiet about it.

Written to a file rather than only printed, because the thing that failed is
usually the scheduler or a cron run with no terminal of its own, and the
window gets opened hours later to find out what happened.

    python3 webcam_errors.py            # show the log
    python3 webcam_errors.py --clear
"""

import os
import sys
import time
import traceback

LOG_FILE = os.environ.get(
    'WEBCAM_ERROR_LOG',
    os.path.join(os.path.expanduser('~'), '.local', 'share', 'webcam',
                 'errors.log'))

# Trim at this many lines. A traceback is a dozen lines and a bad night can
# produce one a minute, so this is days of a misbehaving camera and far more
# than anyone will read.
MAX_LINES = 4000

# Also print to stderr, so a run started from a terminal shows the failure
# there as it happens rather than only in the file.
TO_STDERR = os.environ.get('WEBCAM_ERRORS_QUIET', '') != '1'

# The stages, named here so the windows can colour a vision failure
# differently from a learning one. The callers pass these or a dotted name of
# their own ('brain G.predict', 'detector.load').
VISION = 'OpenCV'
LEARNING = 'TensorFlow'
BRAIN = 'brain G'
DETECTOR = 'detector'
CORRECTION = 'night correction'

# Which stage a line belongs to, for the windows: the first tag whose word
# appears in the `where` wins. Kept here rather than in each window so both
# agree about what counts as a vision error.
TAGS = [
    ('opencv', VISION), ('cv2', VISION), ('magick', VISION),
    ('tensorflow', LEARNING), ('keras', LEARNING), ('brain', BRAIN),
    ('detector', DETECTOR), ('detect', DETECTOR),
    ('stack', CORRECTION), ('clahe', CORRECTION), ('enhance', CORRECTION),
    ('correct', CORRECTION), ('denoise', CORRECTION), ('compare', CORRECTION),
]

# once() keys already reported in this process. A missing TensorFlow fails on
# every frame and the first line said everything the hundredth would.
_seen = set()


def stage_of(where):
    """Which stage a logged line came from, or '' when nothing matches."""
    low = (where or '').lower()
    for word, tag in TAGS:
        if word in low:
            return tag
    return ''


def _trim():
    try:
        with open(LOG_FILE, encoding='utf-8', errors='replace') as fh:
            lines = fh.readlines()
    except OSError:
        return
    if len(lines) <= MAX_LINES:
        return
    try:
        with open(LOG_FILE, 'w', encoding='utf-8') as fh:
            fh.writelines(lines[-MAX_LINES:])
    except OSError:
        pass


def report(where, problem=None, detail='', hint=''):
    """Record one failure, verbosely, and carry on.

    `where` is the stage that failed, `problem` the exception that was caught
    when there was one - its traceback is written out in full, because the
    useful half of an OpenCV error is usually the call underneath it rather
    than the message itself. `detail` is what the caller wants said about what
    it did instead.

    Never raises. A logger that threw inside an except block would turn a
    recovered fallback into a crash, which is the one thing it must not do.
    """
    try:
        stamp = time.strftime('%Y-%m-%d %H:%M:%S')
        who = os.path.basename(sys.argv[0] or '') or 'webcam'
        head = f'[{stamp}] {where}: '
        if problem is not None:
            head += f'{type(problem).__name__}: {problem}'
        if detail:
            head += (' - ' if problem is not None else '') + detail
        body = [f'{head}   (in {who})']
        if hint:
            body.append(f'    try: {hint}')
        if problem is not None:
            for chunk in traceback.format_exception(
                    type(problem), problem, problem.__traceback__):
                for line in chunk.rstrip('\n').split('\n'):
                    body.append('    ' + line)
        text = '\n'.join(body) + '\n'

        if TO_STDERR:
            try:
                sys.stderr.write(text)
                sys.stderr.flush()
            except (OSError, ValueError):
                pass
        try:
            folder = os.path.dirname(LOG_FILE)
            if folder:
                os.makedirs(folder, exist_ok=True)
            with open(LOG_FILE, 'a', encoding='utf-8') as fh:
                fh.write(text)
            _trim()
        except OSError:
            pass
        return text
    except Exception:                                         # noqa: BLE001
        return ''


def once(key, where, detail='', problem=None, hint=''):
    """Report something only the first time it happens in this process.

    For the failures that repeat every frame: TensorFlow not installed, the
    detector model missing. The first line says everything, and a log that
    repeats it forty times a night buries the one line that was different.
    """
    try:
        if key in _seen:
            return ''
        _seen.add(key)
    except Exception:                                         # noqa: BLE001
        pass
    return report(where, problem, detail, hint)


def note(where, detail, hint=''):
    """A failure with no exception behind it - a missing model file, a library
    simply not installed. Same destination, no traceback."""
    return report(where, None, detail, hint)


def install_excepthook(name='webcam'):
    """Send an uncaught crash here too, before it reaches the terminal.

    A training run that dies on its own is the one failure that was already
    visible - but only to whoever was watching the terminal it died in. This
    puts it in the log as well, so the window shows it afterwards.
    """
    previous = sys.excepthook

    def hook(kind, value, trace):
        try:
            value.__traceback__ = trace
            report(f'{name}.crash', value,
                   detail='the run stopped here rather than falling back')
        except Exception:                                     # noqa: BLE001
            pass
        previous(kind, value, trace)

    sys.excepthook = hook
    return hook


def size():
    """How long the log is now, for a window that wants to read only what is
    appended after this moment."""
    try:
        return os.path.getsize(LOG_FILE)
    except OSError:
        return 0


def read_from(offset):
    """Whatever was appended since `offset`, and the new offset.

    This is what the windows poll. A log trimmed or deleted under us comes
    back shorter than the offset, so that case restarts from the top rather
    than reading out of the middle of a line.
    """
    try:
        now = os.path.getsize(LOG_FILE)
    except OSError:
        return '', 0
    if now < offset:
        offset = 0
    if now == offset:
        return '', offset
    try:
        with open(LOG_FILE, encoding='utf-8', errors='replace') as fh:
            fh.seek(offset)
            return fh.read(), now
    except OSError:
        return '', offset


def tail(lines=60):
    try:
        with open(LOG_FILE, encoding='utf-8', errors='replace') as fh:
            return ''.join(fh.readlines()[-lines:])
    except OSError:
        return ''


def last_line():
    """The newest reported failure, one line, for the status bar."""
    for line in reversed(tail(400).split('\n')):
        if line.startswith('['):
            return line.strip()
    return ''


def clear():
    try:
        os.remove(LOG_FILE)
        _seen.clear()
        return True
    except OSError:
        return False


def main():
    if '--clear' in sys.argv[1:]:
        print('cleared' if clear() else f'nothing to clear ({LOG_FILE})')
        return
    text = tail(400)
    print(text if text else f'no failures logged ({LOG_FILE})')


if __name__ == '__main__':
    main()
