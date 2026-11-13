#!/usr/bin/env python3
"""webcam_lock.py - the locked-on target.

One thing picked out of the picture - a particular car on the kerb, a
particular doorway - and kept as *the* thing the rest of these scripts are
trying to get a good picture of, twenty-four hours a day.

Why this exists. Everything else in this program meters the whole frame: the
light meter averages it, the CLAHE spreads it, the detector boosts whatever it
happens to find. That is the right default and it is also why the car across
the road comes out badly - it is two per cent of the frame, so a security
light at the other end of the yard wins every argument about exposure. Lock
on to the car and the arguments are settled in the car's favour instead: the
light meter aims its target mean at that region, and the enhancer gives that
box the strongest lift and keeps the crop of it.

What is stored, in ~/.webcam-object-lock.json:
  path    - the detection crop that was picked, copied aside so that the
            pruning of the detections folder cannot take the lock with it
  label   - 'car', 'person' or whatever the detector called it
  box     - where it was in the frame it came from, as a fraction of the
            frame, which is how it survives a resolution change
  locked  - when it was locked, as a unix time

How the target is found again in a later frame, in order:
  1. template match of the stored crop, multi-scale, over the whole frame;
  2. failing that, the detector box of the same label nearest to where the
     target was last seen;
  3. failing that, the stored fractional box itself - a parked car does not
     move, and the last known place is a better answer than the whole frame.

Nothing here is required. With no lock set every function answers None and
the callers carry on metering the whole frame exactly as before, and with no
OpenCV installed the lock degrades to its stored box rather than failing.
"""

import json
import os
import shutil
import time

LOCK_FILE = os.environ.get(
    'WEBCAM_LOCK', os.path.join(os.path.expanduser('~'),
                                '.webcam-object-lock.json'))
# The crop is copied here so the detections folder can be pruned freely.
LOCK_IMAGE = os.path.join(os.path.expanduser('~'), '.webcam-object-lock.jpg')

# How many detection crops the window offers to pick from.
RECENT_KEEP = 20

# A template match has to be at least this good to be believed. Below it the
# answer is "I cannot see the target in this frame", which is a useful answer
# - at night it is often the true one - and is better than pointing the
# exposure at a patch of fence that happened to score 0.3.
MATCH_MIN = 0.42

# The scales the template is tried at. A car at 60 feet changes apparent size
# with nothing more than a lens refocus, and a single-scale match misses it.
MATCH_SCALES = (0.70, 0.85, 1.00, 1.20, 1.45)

# How much of the frame around the box the metering should take in. A box with
# nothing around it meters the car's own paint, which is a fine way to expose
# a black car as a grey one; a little context is what makes the number
# plate readable.
ROI_MARGIN = 0.25


def _cv2():
    try:
        import cv2
        import numpy
        return cv2, numpy
    except Exception:                                         # noqa: BLE001
        return None


# ------------------------------------------------------------ the lock itself


def read_lock():
    """What is locked, or None. Never raises - a corrupt file is no lock."""
    try:
        with open(LOCK_FILE, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get('path'):
        return None
    if not os.path.exists(data['path']):
        return None
    return data


def locked():
    """True when there is a target to watch."""
    return read_lock() is not None


def set_lock(crop_path, label='', box=None):
    """Lock on to the object in `crop_path`.

    The crop is copied aside first. Without that the lock would last only
    until the detections folder next pruned itself down to its forty newest
    files, which on a busy evening is under an hour - the lock would quietly
    unlock itself overnight and the reason would be invisible.
    """
    if not crop_path or not os.path.exists(crop_path):
        return None
    if not label:
        label = guess_label(crop_path)
    try:
        shutil.copyfile(crop_path, LOCK_IMAGE)
    except OSError:
        return None
    data = {'path': LOCK_IMAGE,
            'source': crop_path,
            'label': label,
            'box': box or {},
            'locked': time.time(),
            'seen': 0.0,
            'seen_box': box or {}}
    try:
        with open(LOCK_FILE, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, indent=1)
    except OSError:
        return None
    return data


def clear_lock():
    """Stop watching anything in particular. Back to metering the whole
    frame, which is where this started."""
    for path in (LOCK_FILE, LOCK_IMAGE):
        try:
            os.remove(path)
        except OSError:
            pass
    return True


def guess_label(path):
    """The label out of a crop's own file name.

    webcam_detect writes them as <stamp>-<index>-<label>-<score>.jpg, so the
    label is already in the name and does not need the detector re-run to get
    it back.
    """
    name = os.path.basename(path)
    parts = os.path.splitext(name)[0].split('-')
    for part in parts:
        if part.lower() in ('car', 'vehicle', 'truck', 'bus', 'person',
                            'motorcycle', 'bicycle'):
            return part.lower()
    return 'object'


def describe():
    """One line for a status bar: what is locked and when it was last seen."""
    data = read_lock()
    if not data:
        return 'no object locked - metering the whole frame'
    label = data.get('label') or 'object'
    when = time.strftime('%d %b %H:%M', time.localtime(data.get('locked', 0)))
    seen = data.get('seen') or 0
    if seen:
        ago = max(0, int(time.time() - seen))
        found = (f'last found {ago}s ago' if ago < 300
                 else f'last found {ago // 60}m ago')
    else:
        found = 'not found in a frame yet'
    return f'locked on: {label}, picked {when} - {found}'


# ------------------------------------------------- the crops to choose from


def detections_dirs(extra=None):
    """Where the detection crops may be. The enhancer writes them into a
    `detections` folder beside its corrected stills, so the folder follows
    wherever the snapshots are pointed."""
    out = []
    if extra:
        out.append(extra)
    try:
        import webcam_enhance
        base = webcam_enhance.Enhancer().out_dir()
        if base:
            out.append(os.path.join(base, 'detections'))
    except Exception:                                         # noqa: BLE001
        pass
    sample = os.environ.get(
        'WEBCAM_SNAPSHOT',
        os.path.join(os.path.expanduser('~'), 'Pictures',
                     'webcam-sample-images'))
    out.append(os.path.join(sample, 'enhanced', 'detections'))
    out.append(os.path.join(os.path.expanduser('~'), 'Pictures', 'webcam',
                            'enhanced', 'detections'))
    seen, unique = set(), []
    for path in out:
        if path and path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def recent_crops(count=RECENT_KEEP, folder=None):
    """The newest detection crops, newest first, at most `count` of them.

    These are what the window puts in its twenty picture boxes. Each entry
    carries the path, the label out of the file name, and the time, which is
    all the window needs to draw a box and a caption under it.
    """
    found = []
    for directory in detections_dirs(folder):
        if not os.path.isdir(directory):
            continue
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith(('.jpg', '.jpeg', '.png')):
                continue
            path = os.path.join(directory, name)
            try:
                when = os.path.getmtime(path)
            except OSError:
                continue
            found.append({'path': path, 'label': guess_label(path),
                          'when': when})
        if found:
            break           # the first folder with anything in it wins
    found.sort(key=lambda item: item['when'], reverse=True)
    current = read_lock()
    current_source = (current or {}).get('source', '')
    for item in found[:count]:
        item['is_locked'] = bool(current_source and
                                 item['path'] == current_source)
        item['stamp'] = time.strftime('%d %b %H:%M:%S',
                                      time.localtime(item['when']))
    return found[:count]


# ------------------------------------------------------- finding it again


def _note_seen(data, box, score):
    """Remember where the target was, so the next frame can start there."""
    data = dict(data)
    data['seen'] = time.time()
    data['seen_box'] = box
    data['score'] = round(float(score), 3)
    try:
        with open(LOCK_FILE, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, indent=1)
    except OSError:
        pass


def _as_fraction(box, width, height):
    return {'x': box['x'] / width, 'y': box['y'] / height,
            'w': box['w'] / width, 'h': box['h'] / height}


def _as_pixels(box, width, height):
    return {'x': int(box.get('x', 0) * width),
            'y': int(box.get('y', 0) * height),
            'w': max(1, int(box.get('w', 0) * width)),
            'h': max(1, int(box.get('h', 0) * height))}


def find(image, boxes=None):
    """Where the locked target is in this frame.

    `image` is a BGR array; `boxes` is the detector's own output for the same
    frame when there is some, which saves a template match on the common case
    of the detector having already found the thing.

    Returns a dict with the pixel box, how it was found and how good the
    match was, or None when there is no lock or nothing credible.
    """
    data = read_lock()
    if not data or image is None:
        return None
    tools = _cv2()
    if tools is None:
        return _fallback(data, image)
    cv2, numpy = tools
    try:
        height, width = image.shape[:2]
    except Exception:                                         # noqa: BLE001
        return None

    # 1. the template match.
    best = None
    template = cv2.imread(data['path'], cv2.IMREAD_GRAYSCALE)
    grey = (cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            if len(image.shape) == 3 else image)
    if template is not None and template.size:
        for scale in MATCH_SCALES:
            tile_w = int(template.shape[1] * scale)
            tile_h = int(template.shape[0] * scale)
            if tile_w < 12 or tile_h < 12 or tile_w >= width or tile_h >= height:
                continue
            try:
                tile = cv2.resize(template, (tile_w, tile_h),
                                  interpolation=cv2.INTER_AREA)
                found = cv2.matchTemplate(grey, tile, cv2.TM_CCOEFF_NORMED)
                _low, high, _lowat, highat = cv2.minMaxLoc(found)
            except Exception:                                 # noqa: BLE001
                continue
            if best is None or high > best[0]:
                best = (high, {'x': int(highat[0]), 'y': int(highat[1]),
                               'w': tile_w, 'h': tile_h})
    if best and best[0] >= MATCH_MIN:
        box = best[1]
        _note_seen(data, _as_fraction(box, width, height), best[0])
        return {'box': box, 'how': 'template match', 'score': best[0],
                'label': data.get('label', 'object')}

    # 2. the detector's own boxes, same label, nearest to where it was.
    if boxes:
        want = (data.get('label') or '').lower()
        last = data.get('seen_box') or data.get('box') or {}
        anchor = _as_pixels(last, width, height) if last else None
        same = [b for b in boxes if str(b.get('label', '')).lower() == want] or boxes
        if same:
            if anchor:
                def distance(b):
                    return ((b['x'] - anchor['x']) ** 2 +
                            (b['y'] - anchor['y']) ** 2)
                pick = min(same, key=distance)
            else:
                pick = max(same, key=lambda b: b.get('score', 0))
            box = {'x': int(pick['x']), 'y': int(pick['y']),
                   'w': int(pick['w']), 'h': int(pick['h'])}
            _note_seen(data, _as_fraction(box, width, height),
                       pick.get('score', 0.0))
            return {'box': box, 'how': 'detector box', 'label': want or 'object',
                    'score': float(pick.get('score', 0.0))}

    # 3. the last place it was, or the place it was locked from.
    return _fallback(data, image)


def _fallback(data, image):
    """The stored box, in this frame's pixels. The honest answer for a parked
    car on a night too dark to match anything in."""
    last = data.get('seen_box') or data.get('box') or {}
    if not last.get('w'):
        return None
    try:
        height, width = image.shape[:2]
    except Exception:                                         # noqa: BLE001
        return None
    return {'box': _as_pixels(last, width, height),
            'how': 'last known place', 'score': 0.0,
            'label': data.get('label', 'object')}


def region(image, boxes=None, margin=ROI_MARGIN):
    """The locked target with a margin round it, as a cropped array, or None.

    This is what the light meter measures when a lock is set: expose for the
    car and its bit of kerb, and let the floodlight at the far end of the
    yard clip if it wants to.
    """
    found = find(image, boxes)
    if not found:
        return None
    box = found['box']
    height, width = image.shape[:2]
    pad_x = int(box['w'] * margin)
    pad_y = int(box['h'] * margin)
    x = max(0, box['x'] - pad_x)
    y = max(0, box['y'] - pad_y)
    x2 = min(width, box['x'] + box['w'] + pad_x)
    y2 = min(height, box['y'] + box['h'] + pad_y)
    if x2 - x < 8 or y2 - y < 8:
        return None
    crop = image[y:y2, x:x2]
    return {'image': crop, 'box': box, 'how': found['how'],
            'label': found.get('label', 'object'),
            'score': found.get('score', 0.0)}


def region_from_file(path, boxes=None, margin=ROI_MARGIN):
    """The same, read from a still on disk. Returns the crop's own path - a
    file beside the lock, overwritten each time - so callers that work by
    handing ImageMagick a file name need nothing new.
    """
    tools = _cv2()
    if tools is None or not path or not os.path.exists(path):
        return None
    cv2, _numpy = tools
    image = cv2.imread(path, cv2.IMREAD_COLOR)
    if image is None:
        return None
    got = region(image, boxes, margin)
    if not got:
        return None
    out = os.path.join(os.path.dirname(LOCK_IMAGE), '.webcam-lock-roi.jpg')
    try:
        if not cv2.imwrite(out, got['image'],
                           [int(cv2.IMWRITE_JPEG_QUALITY), 92]):
            return None
    except Exception:                                         # noqa: BLE001
        return None
    got['path'] = out
    return got


def preferred_box(boxes, image=None):
    """Which of the detector's boxes is the locked one, or None.

    The enhancer uses this to give the locked target the strongest lift and
    the kept crop, rather than spreading the same boost over every car that
    happens to be parked on the street.
    """
    if not boxes:
        return None
    found = find(image, boxes) if image is not None else None
    if not found:
        return None
    box = found['box']
    centre = (box['x'] + box['w'] / 2, box['y'] + box['h'] / 2)

    def holds(candidate):
        return (candidate['x'] <= centre[0] <= candidate['x'] + candidate['w']
                and candidate['y'] <= centre[1] <= candidate['y'] + candidate['h'])

    for candidate in boxes:
        if holds(candidate):
            return candidate
    return None


def main():
    """The command line, which is also how the Perl side of this talks to it.

      python3 webcam_lock.py                  say what is locked
      python3 webcam_lock.py clear            unlock
      python3 webcam_lock.py set <crop>       lock on to that crop
      python3 webcam_lock.py list             the newest crops, one per line
      python3 webcam_lock.py roi <still>      find the target in that still
      python3 webcam_lock.py box <still>      the target's box, as pixels

    `roi` is the one the Perl light meter calls every pass. It prints one tab
    separated line - the crop's path, how it was found, and the label - and
    nothing at all when there is no lock or the target is not in the frame,
    which is what makes it safe to drop straight into a shell backtick.
    """
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'clear':
        clear_lock()
        print('lock cleared')
        return 0
    if len(sys.argv) > 2 and sys.argv[1] == 'set':
        print('locked' if set_lock(sys.argv[2]) else 'could not lock on that')
        return 0
    if len(sys.argv) > 2 and sys.argv[1] in ('roi', 'box'):
        got = region_from_file(sys.argv[2])
        if not got:
            return 1
        if sys.argv[1] == 'box':
            box = got['box']
            print(f"{box['x']}\t{box['y']}\t{box['w']}\t{box['h']}\t"
                  f"{got['how']}\t{got['label']}")
        else:
            print(f"{got['path']}\t{got['how']}\t{got['label']}")
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == 'list':
        for item in recent_crops():
            print(f"{item['path']}\t{item['label']}\t{item['stamp']}\t"
                  f"{1 if item['is_locked'] else 0}")
        return 0
    print(describe())
    for item in recent_crops():
        print(f"  {item['stamp']}  {item['label']:<8} {item['path']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
