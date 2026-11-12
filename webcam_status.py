#!/usr/bin/env python3
"""Is OpenCV working, is TensorFlow working, and the four comparison frames.

Two jobs, both for the tile at the bottom of the window.

1. Say whether OpenCV and TensorFlow are *working*, which is not the same
   question as whether they are installed. An import that succeeds and then
   throws on the first real call is the usual way a mixed conda/pip install
   fails, so each check here imports the library and then does one small real
   piece of work with it - a CLAHE on a tiny array, an add on a tensor. A
   library only counts as working when that returns.

2. Turn the frames the enhancer saved into thumbnails the window can show
   side by side. Written as binary PPM, which is the one image format that
   both python's tkinter and perl's Tk read with no extra module installed.

Run it on its own to see the answers, or with --json for the perl window,
which cannot import this and reads what it prints instead:

    python3 webcam_status.py
    python3 webcam_status.py --json --thumb-width 760
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The four frames the enhancer leaves behind, in the order they come out of
# the pipeline, and the caption each one gets in the window. The first pair is
# the correction - the frame as the camera gave it, and the same frame after
# the stack, the CLAHE and brain G. The second pair is the enhancement that
# runs on top of that: the corrected frame again, and then the frame with the
# car or the person found in it and given a lift of their own, with the box
# drawn on so it is clear what was picked out.
COMPARE_DIR_NAME = 'compare'
STAGES = [
    ('raw.ppm', 'before - as the camera gave it'),
    ('corrected.ppm', 'after - stacked, CLAHE, brain G'),
    ('boost-before.ppm', 'before - the corrected frame'),
    ('boost-after.ppm', 'after - car/person found and lifted'),
]

# Rectangle colours, named here only so the window can print a legend that
# matches what webcam_detect.py actually draws.
LEGEND = [('person', '#5adc5a', (90, 220, 90)),
          ('car / vehicle', '#5ab4fa', (250, 180, 90))]


def _enhance():
    try:
        import webcam_enhance
    except Exception:                                         # noqa: BLE001
        return None
    return webcam_enhance


def compare_dir():
    """Where the enhancer puts the comparison frames: beside the corrected
    stills, so they are pruned and backed up with everything else rather than
    living in /tmp where a reboot takes them."""
    enhance = _enhance()
    if enhance is not None:
        try:
            return os.path.join(enhance.Enhancer().out_dir(), COMPARE_DIR_NAME)
        except Exception:                                     # noqa: BLE001
            pass
    return os.path.join(os.path.expanduser('~'), 'Pictures', 'webcam',
                        'enhanced', COMPARE_DIR_NAME)


# ---------------------------------------------------------------- the checks


def check_opencv():
    """Import it, then actually use it. The second half is the point: a cv2
    that imports and then cannot build a CLAHE is the failure this tile exists
    to show, and it looks identical to a working one from the import alone."""
    try:
        import cv2
    except Exception as problem:                              # noqa: BLE001
        return {'name': 'OpenCV', 'ok': False, 'version': '',
                'detail': f'not installed ({type(problem).__name__})',
                'hint': 'conda install -c conda-forge opencv'}
    version = getattr(cv2, '__version__', '?')
    try:
        import numpy
        frame = numpy.zeros((16, 16, 3), numpy.uint8)
        lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
        light = cv2.split(lab)[0]
        cv2.createCLAHE(clipLimit=2.0, tileGridSize=(2, 2)).apply(light)
        cv2.GaussianBlur(light, (3, 3), 0)
    except Exception as problem:                              # noqa: BLE001
        return {'name': 'OpenCV', 'ok': False, 'version': version,
                'detail': f'installed but will not run: {problem}',
                'hint': 'a mixed conda and pip opencv - keep one of them'}
    build = ''
    try:
        # Whether the ONNX reader the detector needs is in this build.
        build = 'dnn ok' if hasattr(cv2, 'dnn') and \
            hasattr(cv2.dnn, 'readNetFromONNX') else 'no dnn module'
    except Exception:                                         # noqa: BLE001
        build = ''
    return {'name': 'OpenCV', 'ok': True, 'version': version,
            'detail': f'working{", " + build if build else ""}', 'hint': ''}


def check_tensorflow():
    """Same shape of check. This one is slow - importing TensorFlow takes a
    few seconds and it prints its own warnings on the way in - so the windows
    run it off the main thread and this quiets it down first."""
    os.environ.setdefault('TF_CPP_MIN_LOG_LEVEL', '2')
    try:
        import tensorflow as tf
    except Exception as problem:                              # noqa: BLE001
        return {'name': 'TensorFlow', 'ok': False, 'version': '',
                'detail': f'not installed ({type(problem).__name__})',
                'hint': 'pip install tensorflow'}
    version = getattr(tf, '__version__', '?')
    try:
        total = tf.constant([1.0, 2.0]) + tf.constant([3.0, 4.0])
        if float(tf.reduce_sum(total).numpy()) != 10.0:
            raise ValueError('arithmetic came back wrong')
    except Exception as problem:                              # noqa: BLE001
        return {'name': 'TensorFlow', 'ok': False, 'version': version,
                'detail': f'installed but will not run: {problem}',
                'hint': 'usually a numpy version clash'}
    where = 'CPU'
    try:
        if tf.config.list_physical_devices('GPU'):
            where = 'GPU'
    except Exception:                                         # noqa: BLE001
        pass
    return {'name': 'TensorFlow', 'ok': True, 'version': version,
            'detail': f'working on {where}', 'hint': ''}


def check_detector():
    """The ONNX net that finds the car and the person. Reported separately
    because it needs OpenCV *and* the model file, and a missing model file is
    the likelier of the two and says nothing about OpenCV."""
    try:
        import webcam_detect
    except Exception as problem:                              # noqa: BLE001
        return {'name': 'Object detector', 'ok': False, 'version': '',
                'detail': f'webcam_detect.py not importable ({problem})',
                'hint': ''}
    path = ''
    try:
        path = webcam_detect.model_path() or ''
    except Exception:                                         # noqa: BLE001
        pass
    if not path or not os.path.exists(path):
        return {'name': 'Object detector', 'ok': False, 'version': '',
                'detail': 'the nanodet model file is not here',
                'hint': f'expected {webcam_detect.MODEL_NAME} in detector/'}
    try:
        detector = webcam_detect.Detector()
        if not detector.ready():
            raise RuntimeError('the net would not load')
    except Exception as problem:                              # noqa: BLE001
        return {'name': 'Object detector', 'ok': False, 'version': '',
                'detail': f'model is here but will not load: {problem}',
                'hint': ''}
    return {'name': 'Object detector', 'ok': True, 'version': 'nanodet',
            'detail': 'working, green = person, blue = car', 'hint': ''}


def check_brain():
    """Brain G itself - whether there is a trained brain to correct with, and
    whether it is the one trained here or the one that shipped. Asked through
    webcam_enhance rather than by looking for files, so the answer is the one
    the corrector will actually get when it runs."""
    enhance = _enhance()
    if enhance is None:
        return {'name': 'Brain G', 'ok': False, 'version': '',
                'detail': 'webcam_enhance.py not importable', 'hint': ''}
    folders = []
    try:
        folders = [path for path in enhance.brain_dirs() if os.path.isdir(path)]
    except Exception:                                         # noqa: BLE001
        pass
    try:
        # Drop any cached load first, so a brain retrained since this window
        # opened is the one reported.
        enhance._brain.loaded = None
    except Exception:                                         # noqa: BLE001
        pass
    try:
        brain = enhance._brain()
    except Exception as problem:                              # noqa: BLE001
        return {'name': 'Brain G', 'ok': False, 'version': '',
                'detail': f'a brain is here but will not load: {problem}',
                'hint': 'use the delete-and-retrain button in the tile above'}
    if brain is None:
        return {'name': 'Brain G', 'ok': False, 'version': '',
                'detail': 'no brain loaded - correcting analytically instead',
                'hint': 'train one with the button in the tile above'}
    where = os.path.basename(folders[0].rstrip('/')) if folders else ''
    own = 'self-trained' if 'night_model' in where or 'brain_g' in where else where
    return {'name': 'Brain G', 'ok': True, 'version': own,
            'detail': f'loaded{" from " + where if where else ""}', 'hint': ''}


def all_checks(include_slow=True):
    """Every row of the status bar. TensorFlow is the slow one and can be left
    out when something wants the quick answers only."""
    rows = [check_opencv()]
    if include_slow:
        rows.append(check_tensorflow())
    rows.append(check_detector())
    rows.append(check_brain())
    return rows


# ------------------------------------------------------- the four thumbnails


def _write_thumb(source, target, width):
    """One frame, scaled to `width` and written as a binary PPM.

    PPM because it is the only format both tkinter's PhotoImage and perl's
    Tk::Photo read without an extra module installed, and these windows
    install their own dependencies - asking for Tk::PNG or Pillow on top of
    that is a dependency to go wrong at three in the morning for no gain. The
    files are a few hundred kB and are overwritten each pass.
    """
    try:
        import cv2
    except Exception:                                         # noqa: BLE001
        return 0, 0
    image = cv2.imread(source, cv2.IMREAD_COLOR)
    if image is None:
        return 0, 0
    height = max(1, int(round(image.shape[0] * (width / image.shape[1]))))
    scaled = cv2.resize(image, (int(width), height),
                        interpolation=cv2.INTER_AREA)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    return (int(width), height) if cv2.imwrite(target, scaled) else (0, 0)


def thumbnails(total_width=760, folder=None):
    """The four frames as thumbnails, two to a row.

    `total_width` is how wide the pair is together - the width of the terminal
    above them - so each one is half of it less a gap, and the two rows line
    up with each other and with the terminal.
    """
    folder = folder or compare_dir()
    each = max(80, int((total_width - 18) // 2))
    out = []
    for name, caption in STAGES:
        source = os.path.join(folder, name.replace('.ppm', '.jpg'))
        target = os.path.join(folder, 'thumbs', f'{each}-{name}')
        width, height = (0, 0)
        if os.path.exists(source):
            width, height = _write_thumb(source, target, each)
        out.append({'name': name, 'caption': caption,
                    'source': source if os.path.exists(source) else '',
                    'path': target if width else '',
                    'width': width, 'height': height})
    return out


def compare_note(folder=None):
    """The line the enhancer left saying what it did and what it found."""
    folder = folder or compare_dir()
    try:
        with open(os.path.join(folder, 'compare.txt'), encoding='utf-8') as fh:
            return fh.read().strip()
    except OSError:
        return ''


def plain(report):
    """The same answers as --json, in tab separated lines.

    For the perl window, which reads this instead of importing it. Plain text
    rather than JSON so that window needs no JSON module: these two programs
    install their own dependencies and a core-module-only reader is one less
    thing to go wrong at three in the morning.

        CHECK   name    1|0   version   detail    hint
        THUMB   name    path  width     height    caption
        NOTE    text
        DIR     path
    """
    lines = []
    for row in report['checks']:
        lines.append('\t'.join(('CHECK', row['name'], '1' if row['ok'] else '0',
                                row['version'], row['detail'], row['hint'])))
    for thumb in report.get('thumbs', []):
        lines.append('\t'.join(('THUMB', thumb['name'], thumb['path'],
                                str(thumb['width']), str(thumb['height']),
                                thumb['caption'])))
    lines.append('\t'.join(('DIR', report['compare_dir'])))
    note = (report.get('note') or '').replace('\n', ' | ')
    if note:
        lines.append('\t'.join(('NOTE', note)))
    return '\n'.join(lines)


def main():
    args = sys.argv[1:]
    width = 760
    if '--thumb-width' in args:
        try:
            width = int(args[args.index('--thumb-width') + 1])
        except (IndexError, ValueError):
            pass
    quick = '--quick' in args
    report = {'checks': all_checks(include_slow=not quick),
              'compare_dir': compare_dir(),
              'note': compare_note()}
    if '--no-thumbs' not in args:
        report['thumbs'] = thumbnails(width)
    if '--json' in args:
        print(json.dumps(report))
        return
    if '--lines' in args:
        # A flat pipe-separated form for the perl window, which cannot import
        # this. One record per line, so it parses with a split and needs no
        # JSON module installed on top of Tk.
        for row in report['checks']:
            print('CHECK|{}|{}|{}|{}|{}'.format(
                row['name'], 1 if row['ok'] else 0, row['version'],
                row['detail'].replace('|', '/'),
                row['hint'].replace('|', '/')))
        for thumb in report.get('thumbs', []):
            print('THUMB|{}|{}|{}|{}|{}'.format(
                thumb['name'], thumb['caption'].replace('|', '/'),
                thumb['path'], thumb['width'], thumb['height']))
        print(f"DIR|{report['compare_dir']}")
        for line in (report['note'] or '').splitlines():
            print(f'NOTE|{line}')
        return
    if '--plain' in args:
        print(plain(report))
        return
    for row in report['checks']:
        mark = 'working' if row['ok'] else 'NOT working'
        version = f" {row['version']}" if row['version'] else ''
        print(f"{row['name']}{version}: {mark} - {row['detail']}")
        if row['hint'] and not row['ok']:
            print(f"    try: {row['hint']}")
    print(f"\ncomparison frames in {report['compare_dir']}")
    if report['note']:
        print(report['note'])
    for thumb in report.get('thumbs', []):
        state = f"{thumb['width']}x{thumb['height']}" if thumb['path'] else 'missing'
        print(f"  {thumb['name']:<18} {state}")


if __name__ == '__main__':
    main()
