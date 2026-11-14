#!/usr/bin/env python3
"""Brain G - the night vision brain, and the trainer that makes it.

Built in the same shape as the trading robot's brains, for the same reasons:
a FROZEN hand-weighted foundation layer whose units are named detectors,
a small trainable head on top of it, a 20,000 sample pretrain, the model and
a meta.json written side by side, and a stale-model check that retrains rather
than quietly running a model that no longer matches the code.

WHAT IT LEARNS

It is trained on clear photos. For each clear photo the trainer makes a dark,
noisy copy of it with a camera model - lost exposure, shot noise, read noise,
a lens softened, a colour cast, JPEG - and then asks the brain to predict, for
each pixel, how much that pixel has to be lifted to get back to the clear
original. The clear photo is the answer sheet; the degraded copy is the
question. That is the whole of it.

Two numbers come out per pixel:

  * gain      - how much to multiply the brightness by, in log space
  * saturation - what to do with the colour after that gain, so that lifting
                 a dark frame does not leave it grey or garish

Per-pixel rather than per-frame is the point. A single exposure correction
cannot both lift a car in deep shadow and leave the security light above it
alone; a per-pixel gain can, because the brain sees how dark each pixel's own
neighbourhood is and how close the nearest bright source is.

THE FROZEN FOUNDATION

Nine named detector units, hand weighted, trainable=False, exactly as the
trading brains do it. These are not learnt and they do not drift:

  0 DEEP_SHADOW          this pixel's neighbourhood is very dark
  1 NEAR_BLACK_CLIPPED   it is at or near the black floor, where there is
                         nothing left to recover and lifting only lifts noise
  2 LOCAL_DETAIL         there is real local contrast here, so it is structure
                         rather than empty sky or tarmac
  3 GLARE_NEARBY         a bright source is close by, so be careful
  4 FLAT_NOISE           dark AND no local contrast - the signature of noise,
                         which is what must NOT be amplified
  5 MIDTONE_BODY         already somewhere near a sensible level
  6 HIGHLIGHT_GUARD      bright enough that gain would clip it
  7 COLOUR_PRESENT       there is chroma here worth keeping
  8 GLOBAL_DARK_SCENE    the whole frame is dark, not just this corner

The head above them - 9 to 6 to 4 to 2 - is what training actually fits.
Freezing the foundation is what keeps the thing predictable: whatever the head
learns, it is learning on top of a fixed, readable description of the pixel,
and a bad training run cannot turn NEAR_BLACK_CLIPPED into something else.

HOW TO RUN IT

    python3 train_webcam_brain.py --photos ~/Pictures/clear-photos
    python3 train_webcam_brain.py --photos ~/Pictures/clear-photos --force

Point --photos at a folder of clear, well exposed photographs. The daytime
stills from this very camera are the best training material there is, because
then the brain is learning this lens and this sensor rather than somebody
else's: leave the 24 hour switch off, let the folder fill up through a sunny
afternoon, copy those out somewhere, and train on them.

With no --photos it looks in a few likely folders, including the snapshot
folder, and takes whatever is clear enough to be worth training on.

A bare pretrain run trains all five brains, one per lighting regime, each to
~/.webcam-models/brain_<family>/, so a night-trained brain is never applied to
a bright daylight frame. --preset trains just one of them. Each model is made
read-only, because inference must never write to it. --force is the only way
to retrain over one that is already there.
"""

import argparse
import glob
import hashlib
import json
import math
import os
import sys
import time

BRAIN_G_FEATURES = 12
BRAIN_G_UNITS = 9

# 20,000 pretrain samples, and a 20,000 lifetime cap, which is the convention
# the other brains follow.
PRETRAIN_SAMPLES = 20000
PRETRAIN_CAP = 20000
EPOCHS = 30
EARLY_STOP_PATIENCE = 5
BATCH_SIZE = 512

# The most a pixel may be lifted: about 12x. Past that there is nothing in the
# sensor data to lift and the brain would be inventing the whole pixel.
GAIN_MAX = 12.0

# ---------------------------------------------------------------------------
# Training on THIS camera
#
# The brain that ships with these scripts was trained on general photographs.
# One trained on frames from this camera knows this lens, this sensor and this
# scene, and does better - so the scripts harvest their own training set as
# they run and retrain on it, after which the shipped brain is deleted and only
# the self-trained one is used.
#
# THE ONE RULE THAT CANNOT BE BENT: a dark night frame cannot be its own answer
# sheet. Training needs to know what the right answer looks like, and a noisy
# black frame does not contain it - train with dark frames as the target and the
# brain learns to produce dark frames. So what gets harvested as a TARGET is
# only ever a well exposed frame: daytime, dusk, a floodlit yard, anything the
# camera managed to expose properly. Those are what teach it this lens.
#
# What the light changes give, and it is the valuable part, is REAL PAIRS. When
# a preset or a light adjustment fires, the picture changes within a minute or
# two while the scene stays put - the camera is bolted down. A dark frame and a
# well exposed frame of the same scene minutes apart are a genuine training
# pair from this camera: a real input with a real answer, no camera model
# guessed at in between. Every light change command is a chance at one, which
# is why the harvest is wired to them and runs around the clock.
CORPUS_DIR = os.environ.get(
    'WEBCAM_CORPUS',
    os.path.join(os.path.expanduser('~'), 'Pictures', 'webcam-brain-corpus'))
CORPUS_INDEX = os.path.join(CORPUS_DIR, 'corpus.json')

# A well exposed frame, which is what may be a target.
CLEAR_MEAN_LOW = 70
CLEAR_MEAN_HIGH = 190
CLEAR_STD_MIN = 28

# How many frames to keep per bucket, and what a bucket is: the preset in force
# crossed with how hard the camera was working. Quotas rather than everything,
# so that a long winter night does not bury one sunny afternoon under ten
# thousand identical dark frames, and so the corpus stays a bounded size.
QUOTA_CLEAR = 40
QUOTA_DIM = 40

# Enough to train on at all. Below this the corpus would teach the brain this
# one scene and nothing else, which is overfitting, so the shipped brain keeps
# working until there is a real spread.
MIN_CLEAR_FRAMES = 24
MIN_BUCKETS = 3

# The same two tests for one preset's brain. A family only ever sees its own
# lighting regime, so it cannot cover three preset/light combinations and it
# fills up more slowly - but it also has far less to learn, because every
# frame it sees is the same kind of light.
MIN_CLEAR_FRAMES_PRESET = 12
MIN_BUCKETS_PRESET = 2

# A dark frame and a clear frame of the same scene this far apart in time are
# treated as a real pair. Long enough to catch a preset change settling,
# short enough that it is still the same night and the same scene.
PAIR_WINDOW = 150

# Retrain at most this often once it is already trained, so a 24 hour cycle
# gets folded in but the thing is not retraining every hour.
RETRAIN_EVERY = 24 * 3600

# ---------------------------------------------------------------------------
# one brain per lighting preset
#
# A single brain harvested across the whole day sees both bright daytime
# frames and dark night frames with the same well-exposed targets, and the
# cheapest way for it to cut its error is to learn gain < 1 on frames that
# are already bright - which is exactly the darkening that was showing up in
# daylight. The fix is one specialised brain per lighting regime, so a
# night-trained brain is never asked about a bright frame and vice versa.
#
# The six presets collapse into five regimes: the only two that share a brain
# are night and night_mono, because that is the same light with and without
# colour. Flat overcast daylight and direct sun are not the same light - the
# sun frame is clipped in the highlights and deep in the shadows where the
# overcast one is neither - so bright_sun gets a brain of its own.
# ---------------------------------------------------------------------------
BRAIN_PRESETS = {
    'night': ('night', 'night_mono'),
    'golden_am': ('golden_am',),
    'day': ('day',),
    'bright_sun': ('bright_sun',),
    'golden_pm': ('golden_pm',),
}
# What each brain is allowed to do, and how hard.
#
# This is the part that stops a brain from working against the light it was
# given. Four of the five have gain_min = 1.0 and so may only ever brighten a
# pixel, whatever they learn - which is what the darkening in daylight was.
# bright_sun is the single deliberate exception, because direct sun is the one
# case where the frame is genuinely too bright. What differs beyond that is
# how much each one may ask for, and how it treats grain, clarity and colour:
#
#   night       brighten only, and as hard as the sensor allows. The frame is
#               dark, grainy and soft, so this is the one that gets the full
#               lift, the strongest local contrast, the hardest denoise and
#               an unsharp pass to put the edges back that the grain and the
#               denoise took away.
#   golden_am   sunrise. The light climbs minute by minute, so the lift has
#               to come off as it does: a moderate ceiling, and the dim-light
#               part of the lift falls away as the frame brightens.
#   day         left exactly as it was - the behaviour the daylight pictures
#               already had. Nothing scaled, nothing added.
#   bright_sun  direct sun, and the only brain that may go the other way: its
#               gain range sits below 1, so it pulls the level down rather
#               than up, and it cools the colour slightly - a little more
#               blue, a little less red - which is what takes the yellow cast
#               off a frame shot in full sun.
#   golden_pm   sunset. The mirror of golden_am: the light drops away, and
#               the dimmer the frame the more lift it is allowed, up to a
#               dusk ceiling well above daylight's.
#
#   gain_min/gain_max   the range the gain output is decoded into, so the
#                       brain physically cannot ask for anything outside it
#   sat_min/sat_max     the same for the colour multiplier
#   dim_lift            how much extra lift a dim frame gets, 0 for none
#   clahe_scale         local contrast, against the standard clip
#   denoise_scale       grain cleaning, against the standard strength
#   sharpen             unsharp amount after the denoise, 0 for none
#   tint                a fixed BGR multiplier applied after the lift, for a
#                       warm or cool cast the gain cannot express. (1, 1, 1)
#                       leaves the colour alone.
BRAIN_MANDATE = {
    'night': dict(gain_min=1.0, gain_max=12.0, sat_min=0.35, sat_max=2.5,
                  dim_lift=1.0, clahe_scale=1.25, denoise_scale=1.4,
                  sharpen=0.55, tint=(1.0, 1.0, 1.0),
                  note='brighten only, hardest lift, most denoise, sharpened'),
    'golden_am': dict(gain_min=1.0, gain_max=5.0, sat_min=0.5, sat_max=2.0,
                      dim_lift=0.8, clahe_scale=1.1, denoise_scale=1.1,
                      sharpen=0.25, tint=(1.0, 1.0, 1.0),
                      note='sunrise - lift eases off as the light climbs'),
    'day': dict(gain_min=1.0, gain_max=12.0, sat_min=0.25, sat_max=4.0,
                dim_lift=0.0, clahe_scale=1.0, denoise_scale=1.0, sharpen=0.0,
                tint=(1.0, 1.0, 1.0),
                note='unchanged - the daylight behaviour as it was'),
    # The one brain that darkens. 0.7 to 1.0 means it can take about a third
    # off the level at most and can always decline to change it at all, and
    # the tint is a touch of blue with a touch less red - BGR order, so the
    # first number is the blue one.
    'bright_sun': dict(gain_min=0.7, gain_max=1.0, sat_min=0.6, sat_max=1.8,
                       dim_lift=0.0, clahe_scale=1.15, denoise_scale=0.8,
                       sharpen=0.15, tint=(1.06, 1.0, 0.96),
                       note='direct sun - darkens a little and cools the '
                            'colour slightly'),
    'golden_pm': dict(gain_min=1.0, gain_max=6.0, sat_min=0.5, sat_max=2.0,
                      dim_lift=1.0, clahe_scale=1.1, denoise_scale=1.15,
                      sharpen=0.3, tint=(1.0, 1.0, 1.0),
                      note='sunset - the dimmer the frame, the more lift'),
}

# What a brain with no mandate of its own gets: the old all-day behaviour,
# with the floor of 1.0 that every brain has.
DEFAULT_MANDATE = dict(gain_min=1.0, gain_max=GAIN_MAX, sat_min=0.25,
                       sat_max=4.0, dim_lift=0.0, clahe_scale=1.0,
                       denoise_scale=1.0, sharpen=0.0, tint=(1.0, 1.0, 1.0),
                       note='all-day default')


def brain_mandate(family=None):
    """What one brain is allowed to do. Never returns None."""
    return dict(BRAIN_MANDATE.get(family) or DEFAULT_MANDATE)


BRAIN_FAMILIES = tuple(BRAIN_PRESETS)
_PRESET_FAMILY = {preset: family
                  for family, presets in BRAIN_PRESETS.items()
                  for preset in presets}

MODELS_ROOT = os.path.join(os.path.expanduser('~'), '.webcam-models')

# Where the single all-day brain used to live. Still read when no preset has
# been recorded, so an install that has not yet applied a preset is no worse
# off than it was.
LEGACY_MODEL_DIR = os.path.join(MODELS_ROOT, 'brain_g_night_model')


def brain_family(preset=None):
    """The brain a preset belongs to, or None if the preset is unknown.

    Takes either a preset name ('night_mono') or a family name ('night'), so
    callers can pass whichever they have. With nothing passed it reads the
    preset in force.
    """
    if preset is None:
        preset = current_preset()
    name = (preset or '').strip().lower().replace('-', '_').replace(' ', '_')
    if name in BRAIN_PRESETS:
        return name
    return _PRESET_FAMILY.get(name)


def brain_dir_for(family=None):
    """The folder one family's brain lives in.

    WEBCAM_BRAIN_DIR still overrides everything, which is how a single brain
    is forced for testing. With no family - no preset recorded yet - the
    legacy all-day folder is used.
    """
    forced = os.environ.get('WEBCAM_BRAIN_DIR')
    if forced:
        return forced
    if not family:
        return LEGACY_MODEL_DIR
    return os.path.join(MODELS_ROOT, f'brain_{family}')


# The family currently being trained or deleted. MODEL_DIR and its two paths
# follow it, so every existing reference to MODEL_PATH keeps working and only
# has to be pointed at the right brain first, with use_brain().
ACTIVE_BRAIN = None
MODEL_DIR = brain_dir_for(None)
MODEL_PATH = os.path.join(MODEL_DIR, 'model.keras')
META_PATH = os.path.join(MODEL_DIR, 'meta.json')


def use_brain(which=None):
    """Point MODEL_DIR, MODEL_PATH and META_PATH at one preset's brain.

    Returns the family selected, or None for the legacy all-day folder.
    """
    global ACTIVE_BRAIN, MODEL_DIR, MODEL_PATH, META_PATH   # noqa: PLW0603
    family = which if which in BRAIN_PRESETS else brain_family(which)
    ACTIVE_BRAIN = family
    MODEL_DIR = brain_dir_for(family)
    MODEL_PATH = os.path.join(MODEL_DIR, 'model.keras')
    META_PATH = os.path.join(MODEL_DIR, 'meta.json')
    return family


def family_presets(family=None):
    """The preset names one family covers, or None for all of them."""
    if not family:
        return None
    return BRAIN_PRESETS.get(family)

# The brain that ships beside these scripts. It is deleted once a brain trained
# on this camera is in place and has been shown to be no worse - see
# retire_shipped_brain.
SHIPPED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'brain_g_night_model')

# Folders to look in for clear photos when none is given.
PHOTO_GUESSES = (
    os.path.expanduser('~/Pictures/clear-photos'),
    os.path.expanduser('~/Pictures/webcam-clear-photos'),
    os.path.expanduser('~/Pictures/webcam-sample-images'),
    os.path.expanduser('~/Pictures'),
)


# ---------------------------------------------------------------------------
# the features
#
# Twelve numbers per pixel, all of them cheap: box blurs, a couple of local
# standard deviations, two whole-frame figures. Everything here is computed
# from the degraded frame alone, because at run time that is all there is.
# ---------------------------------------------------------------------------

def feature_planes(image):
    """Build the 12 feature planes for one BGR frame of floats 0 to 1.

    Returns an (H, W, 12) array. The same function is used by the trainer and
    at inference, which is the only way the two can agree about what the
    numbers mean.
    """
    import cv2
    import numpy as np

    luma = cv2.cvtColor((image * 255).astype(np.uint8), cv2.COLOR_BGR2GRAY)
    luma = luma.astype(np.float32) / 255.0

    def box(plane, size):
        return cv2.blur(plane, (size, size), borderType=cv2.BORDER_REFLECT)

    m_small = box(luma, 9)
    m_mid = box(luma, 33)
    m_large = box(luma, 97)
    # Local standard deviation, the cheap way: the square root of the mean of
    # the squares less the square of the mean.
    s_small = np.sqrt(np.maximum(box(luma * luma, 17) - box(luma, 17) ** 2, 0))
    s_large = np.sqrt(np.maximum(box(luma * luma, 65) - box(luma, 65) ** 2, 0))
    glare = cv2.dilate(luma, np.ones((31, 31), np.uint8))

    hi = float(np.percentile(luma, 99))
    g_mean = float(luma.mean())

    low = image.min(axis=2)
    high = image.max(axis=2)
    colour = (high - low) / (high + 0.08)
    black = np.maximum(0.0, 1.0 - luma / 0.06)

    planes = [
        luma,
        m_small,
        m_mid,
        m_large,
        np.clip((luma - m_mid) * 4.0, -1, 1),
        np.clip(s_small * 6.0, 0, 1),
        np.clip(s_large * 6.0, 0, 1),
        np.full_like(luma, g_mean),
        np.full_like(luma, hi),
        glare,
        np.clip(colour, 0, 1),
        black,
    ]
    return np.stack(planes, axis=-1).astype(np.float32)


# ---------------------------------------------------------------------------
# the harvest: building a training set out of this camera's own frames
# ---------------------------------------------------------------------------

# Which preset is in force, written by whatever applied it and read by the
# harvest so that every frame is filed under the preset that produced it. A
# one-line file because the thing that applies a preset and the thing that
# harvests are usually two different processes.
LAST_PRESET_FILE = os.environ.get(
    'WEBCAM_LAST_PRESET',
    os.path.join(os.path.expanduser('~'), '.webcam-last-preset'))

# Which brain is active, written at the moment the preset is applied and read
# by the corrector on every frame. One brain is active at a time and the rest
# are not merely unused but dropped - see activate_brain in webcam_enhance.py.
ACTIVE_BRAIN_FILE = os.environ.get(
    'WEBCAM_ACTIVE_BRAIN',
    os.path.join(os.path.expanduser('~'), '.webcam-active-brain'))


def note_preset(name):
    """Record the preset just applied. Called by the settings engine.

    Also writes the brain that preset belongs to, and the moment of the
    change, into ACTIVE_BRAIN_FILE. That file is what makes the switch
    instant: the corrector reads it on every frame, so the moment a preset is
    applied the brain for that preset is the only one active and every other
    one is dropped - no waiting for a retrain, a restart or the next harvest.
    """
    now = int(time.time())
    ok = True
    try:
        with open(LAST_PRESET_FILE, 'w', encoding='utf-8') as handle:
            handle.write(f'{name}\n{now}\n')
    except OSError:
        ok = False
    family = brain_family(name)
    try:
        with open(ACTIVE_BRAIN_FILE, 'w', encoding='utf-8') as handle:
            handle.write(f'{family or ""}\n{name}\n{now}\n')
    except OSError:
        ok = False
    return ok


def active_brain():
    """The brain that should be running right now, and nothing else.

    Returns (family, preset, when) with family None when no preset has been
    applied yet. Read from ACTIVE_BRAIN_FILE when it is there, because that
    file is written at the moment of the change and carries the timestamp;
    otherwise worked out from the preset file, so an install that applied its
    preset before this existed still gets the right brain.
    """
    try:
        with open(ACTIVE_BRAIN_FILE, encoding='utf-8') as handle:
            family = handle.readline().strip()
            preset = handle.readline().strip()
            stamp = handle.readline().strip()
        if preset:
            # The family is re-derived rather than trusted, so editing the
            # table is enough to move a preset from one brain to another
            # without having to delete this file.
            return (brain_family(preset), preset,
                    int(stamp) if stamp.isdigit() else 0)
        if family:
            return (family if family in BRAIN_PRESETS else None, '', 0)
    except (OSError, ValueError):
        pass
    preset = current_preset()
    return (brain_family(preset), preset or '', 0)


def current_preset():
    """The preset in force, or None."""
    try:
        with open(LAST_PRESET_FILE, encoding='utf-8') as handle:
            name = handle.readline().strip()
        return name or None
    except OSError:
        return None


def read_index():
    """The corpus index: what has been harvested and what it was."""
    try:
        with open(CORPUS_INDEX, encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    data.setdefault('frames', [])
    data.setdefault('last_trained', 0)
    data.setdefault('trainings', 0)
    return data


def write_index(data):
    try:
        os.makedirs(CORPUS_DIR, exist_ok=True)
        with open(CORPUS_INDEX, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, indent=1)
        return True
    except OSError:
        return False


def measure_frame(path):
    """Mean, contrast and colourfulness of a frame, or None."""
    import cv2

    image = cv2.imread(path, cv2.IMREAD_COLOR)
    if image is None or image.ndim != 3:
        return None
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return {
        'mean': round(float(grey.mean()), 2),
        'std': round(float(grey.std()), 2),
        'sat': round(float(cv2.cvtColor(image, cv2.COLOR_BGR2HSV)[:, :, 1].mean()), 2),
        'width': int(image.shape[1]),
        'height': int(image.shape[0]),
    }


def is_clear(stats):
    """Whether this frame is well exposed enough to be an answer sheet.

    This is the gate that keeps the whole thing honest. Only a frame that the
    camera actually managed to expose can say what the right answer looks like.
    """
    return (stats is not None
            and CLEAR_MEAN_LOW <= stats['mean'] <= CLEAR_MEAN_HIGH
            and stats['std'] >= CLEAR_STD_MIN)


def bucket_for(preset, gain_fraction):
    """Which bucket a frame counts against: the preset in force, crossed with
    how hard the camera was working.

    Quotas are per bucket so that the corpus ends up spread across the presets
    and the light levels rather than being whatever happened most often.
    """
    preset = (preset or 'unknown').strip().lower().replace(' ', '-')
    if gain_fraction is None:
        work = 'gain-unknown'
    elif gain_fraction <= 0.33:
        work = 'gain-low'
    elif gain_fraction <= 0.66:
        work = 'gain-mid'
    else:
        work = 'gain-high'
    return f'{preset}/{work}'


def harvest(frame, preset=None, gain_fraction=None, controls=None, when=None):
    """Consider one still for the training set. Returns (kept, why).

    Called on every pass of the light meter, and so on every preset and light
    change - which is the point: the corpus fills up across all of the presets
    and the whole of a day and a night, rather than being whatever the camera
    happened to be doing when someone ran the trainer by hand.
    """
    import shutil as _shutil

    if not frame or not os.path.isfile(frame):
        return (False, 'no frame')

    stats = measure_frame(frame)
    if stats is None:
        return (False, 'unreadable')

    when = when or time.time()
    clear = is_clear(stats)
    kind = 'clear' if clear else 'dim'
    bucket = bucket_for(preset, gain_fraction)
    index = read_index()

    # A dim frame is only of use as half of a real pair: a well exposed frame
    # of this same scene within a couple of minutes, which is its answer. It is
    # still kept when it arrives unpaired, because the pairing usually happens
    # the other way round - a light change command BRIGHTENS the picture, so
    # the dark frame lands first and its answer arrives a minute later. Pairing
    # is therefore done in both directions: looking back here, and looking back
    # again from the clear frame below.
    pair_with = None
    if not clear:
        for other in reversed(index['frames']):
            if other['kind'] != 'clear':
                continue
            if abs(other['when'] - when) <= PAIR_WINDOW:
                pair_with = other['file']
                break

    quota = QUOTA_CLEAR if clear else QUOTA_DIM
    have = [f for f in index['frames']
            if f['kind'] == kind and f['bucket'] == bucket]
    if len(have) >= quota:
        # Full: replace the least useful one rather than refusing forever, so
        # the corpus keeps up with the seasons and with a camera that gets
        # moved. For clear frames the least useful is the flattest one.
        if clear:
            # For an answer sheet the flattest frame is the least useful.
            have.sort(key=lambda f: f.get('std', 0))
            worst = have[0]
            if stats['std'] <= worst.get('std', 0):
                return (False, f'{bucket} is full of better frames')
        else:
            # For a dark frame, an unpaired one is the least useful, then the
            # oldest - a paired frame is worth keeping whatever its age.
            have.sort(key=lambda f: (bool(f.get('pair_with')), f['when']))
            worst = have[0]
            if worst.get('pair_with') and not pair_with:
                return (False, f'{bucket} is full of paired frames')
        try:
            os.remove(os.path.join(CORPUS_DIR, kind, worst['file']))
        except OSError:
            pass
        index['frames'] = [f for f in index['frames'] if f is not worst]

    folder = os.path.join(CORPUS_DIR, kind)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        return (False, f'cannot make {folder}')

    name = time.strftime('%Y%m%d-%H%M%S', time.localtime(when)) + \
        f'-{bucket.replace("/", "-")}.jpg'
    target = os.path.join(folder, name)
    extra = 2
    while os.path.exists(target) and extra < 50:
        target = os.path.join(folder, name.replace('.jpg', f'-{extra}.jpg'))
        extra += 1
    try:
        _shutil.copy2(frame, target)
    except OSError as problem:
        return (False, f'could not copy it in: {problem}')

    record = {
        'file': os.path.basename(target),
        'kind': kind,
        'bucket': bucket,
        'preset': preset or 'unknown',
        'gain_fraction': (round(gain_fraction, 3)
                          if gain_fraction is not None else None),
        'when': int(when),
        'pair_with': pair_with,
    }
    record.update(stats)
    if controls:
        record['controls'] = controls
    index['frames'].append(record)

    # The other direction: a well exposed frame is the answer to any dark frame
    # of the same scene that came in just before it. This is the case that
    # matters, because a preset or light change command brightens the picture -
    # the dark frame is already banked and this is what it was waiting for.
    paired_back = 0
    if clear:
        for other in index['frames']:
            if other['kind'] != 'dim' or other.get('pair_with'):
                continue
            if abs(other['when'] - when) <= PAIR_WINDOW:
                other['pair_with'] = record['file']
                paired_back += 1
    write_index(index)

    note = f'{kind} frame into {bucket}'
    if pair_with:
        note += ' (paired with a well-exposed frame of the same scene)'
    if paired_back:
        note += (f' (and it is the answer for {paired_back} dark frame'
                 f'{"s" if paired_back > 1 else ""} already banked)')
    return (True, note)


def corpus_state(family=None):
    """What is in the training set, for the window and for the decision to
    retrain.

    With a family, only the frames harvested under that family's presets are
    counted - which is what one specialised brain is trained on.
    """
    index = read_index()
    frames = index['frames']
    wanted = family_presets(family)
    if wanted:
        frames = [f for f in frames
                  if (f.get('preset') or '').strip().lower().replace('-', '_')
                  in wanted]
        index = dict(index, frames=frames)
    clear = [f for f in index['frames'] if f['kind'] == 'clear']
    dim = [f for f in index['frames'] if f['kind'] == 'dim']
    buckets = sorted({f['bucket'] for f in clear})
    presets = sorted({f['preset'] for f in index['frames']})
    span = 0
    if index['frames']:
        times = [f['when'] for f in index['frames']]
        span = max(times) - min(times)
    return {
        'clear_frames': len(clear),
        'dim_frames': len(dim),
        'real_pairs': len([f for f in dim if f.get('pair_with')]),
        'buckets': buckets,
        'bucket_count': len(buckets),
        'presets': presets,
        'hours_covered': round(span / 3600.0, 1),
        'last_trained': index.get('last_trained', 0),
        'trainings': index.get('trainings', 0),
        'folder': CORPUS_DIR,
        'family': family,
    }


def ready_to_train(state=None, family=None):
    """Whether there is enough of this camera's own light to train on.

    Both tests are about spread rather than volume. A hundred frames of one
    afternoon from one preset would train a brain that knows that afternoon;
    what is wanted is frames from across the presets and across the day, which
    is what the 24 hour harvest is for.
    """
    state = state or corpus_state(family)
    family = family or state.get('family')
    want_frames = MIN_CLEAR_FRAMES_PRESET if family else MIN_CLEAR_FRAMES
    want_buckets = MIN_BUCKETS_PRESET if family else MIN_BUCKETS
    where = f' under the {family} preset' if family else ''
    if state['clear_frames'] < want_frames:
        return (False, f'{state["clear_frames"]} well-exposed frames{where} of '
                       f'{want_frames} needed - they come in as the camera '
                       f'sees daylight, dusk or a lit scene')
    if state['bucket_count'] < want_buckets:
        return (False, f'frames from {state["bucket_count"]} preset/light '
                       f'combinations{where} of {want_buckets} needed - let the '
                       f'schedule run through more of the day')
    return (True, f'{state["clear_frames"]} well-exposed frames across '
                  f'{state["bucket_count"]} preset/light combinations, '
                  f'{state["real_pairs"]} real pairs, '
                  f'{state["hours_covered"]} hours covered')


def _frames_under(family, kind):
    """The harvested file names of one kind, under one family's presets."""
    wanted = family_presets(family)
    names = []
    for record in read_index()['frames']:
        if record['kind'] != kind:
            continue
        if wanted is not None:
            preset = (record.get('preset') or '').strip().lower()
            if preset.replace('-', '_') not in wanted:
                continue
        names.append(record['file'])
    return names


def corpus_photos(limit=400, family=None):
    """The well exposed frames, as the trainer's photo list.

    With a family, only that family's own lighting regime - which is the
    whole point of the per-preset brains.
    """
    folder = os.path.join(CORPUS_DIR, 'clear')
    if not os.path.isdir(folder):
        return []
    # want_clear=False: these were already gated by is_clear on the way in,
    # and re-gating here with the general photo test would throw away frames
    # of this camera's own scene for being less contrasty than a stock photo.
    if not family:
        return load_photos(folder, limit=limit, want_clear=False)
    paths = [os.path.join(folder, name)
             for name in _frames_under(family, 'clear')]
    paths = [path for path in paths if os.path.isfile(path)]
    return load_photo_paths(paths, limit=limit, want_clear=False)


def corpus_pairs(limit=200, family=None):
    """The real pairs: a dark frame from this camera with a well exposed frame
    of the same scene, as (dark, clear) float images.

    This is the part no synthetic camera model can provide - the actual noise,
    the actual lens, the actual sodium-lit scene, with the actual answer.
    """
    import cv2

    index = read_index()
    by_name = {f['file']: f for f in index['frames']}
    wanted = family_presets(family)
    pairs = []
    for record in index['frames']:
        if record['kind'] != 'dim' or not record.get('pair_with'):
            continue
        if wanted is not None:
            preset = (record.get('preset') or '').strip().lower()
            if preset.replace('-', '_') not in wanted:
                continue
        clear_record = by_name.get(record['pair_with'])
        if not clear_record:
            continue
        dark_path = os.path.join(CORPUS_DIR, 'dim', record['file'])
        clear_path = os.path.join(CORPUS_DIR, 'clear', clear_record['file'])
        if not (os.path.isfile(dark_path) and os.path.isfile(clear_path)):
            continue
        dark = cv2.imread(dark_path, cv2.IMREAD_COLOR)
        clear = cv2.imread(clear_path, cv2.IMREAD_COLOR)
        if dark is None or clear is None or dark.shape != clear.shape:
            continue
        pairs.append((dark.astype('float32') / 255.0,
                      clear.astype('float32') / 255.0))
        if len(pairs) >= limit:
            break
    return pairs


def make_pair_samples(pairs, count, rng, crop=128):
    """Samples from real pairs: features off the real dark frame, targets from
    the real well exposed one."""
    import numpy as np

    features = np.zeros((count, BRAIN_G_FEATURES), dtype=np.float32)
    targets = np.zeros((count, 2), dtype=np.float32)
    filled = 0
    while filled < count and pairs:
        dark, clear = pairs[int(rng.integers(len(pairs)))]
        height, width = dark.shape[:2]
        size = min(crop, height, width)
        top = int(rng.integers(0, height - size + 1))
        left = int(rng.integers(0, width - size + 1))
        dark_crop = dark[top:top + size, left:left + size]
        clear_crop = clear[top:top + size, left:left + size]

        planes = feature_planes(dark_crop)
        answers = targets_for(dark_crop, clear_crop, family=ACTIVE_BRAIN)
        take = min(256, count - filled)
        ys = rng.integers(0, size, take)
        xs = rng.integers(0, size, take)
        features[filled:filled + take] = planes[ys, xs]
        targets[filled:filled + take] = answers[ys, xs]
        filled += take
    return features[:filled], targets[:filled]


def retire_shipped_brain():
    """Delete the brain that shipped with these scripts.

    Only ever called once a brain trained on this camera is saved and has been
    checked, which is the whole condition: nothing is thrown away until
    something better is in place.
    """
    if not os.path.isdir(SHIPPED_DIR):
        return (False, 'no shipped brain to delete')
    if not os.path.isfile(MODEL_PATH):
        return (False, 'refusing to delete it - there is no self-trained brain yet')
    # With one brain per preset, the shipped brain is still the only thing
    # standing behind a family that has not trained yet, so it is kept until
    # all five families have one of their own.
    missing = [family for family in BRAIN_FAMILIES
               if not os.path.isfile(os.path.join(brain_dir_for(family),
                                                  'model.keras'))]
    if missing:
        return (False, 'keeping it - no brain trained yet for '
                       + ', '.join(missing))
    removed = []
    for name in ('model.keras', 'meta.json'):
        path = os.path.join(SHIPPED_DIR, name)
        if not os.path.exists(path):
            continue
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
        try:
            os.remove(path)
            removed.append(name)
        except OSError as problem:
            return (False, f'could not delete {path}: {problem}')
    try:
        os.rmdir(SHIPPED_DIR)
    except OSError:
        pass
    return (True, f'deleted the shipped brain from {SHIPPED_DIR} '
                  f'({", ".join(removed)}) - only the brain trained on this '
                  f'camera is used now')


# ---------------------------------------------------------------------------
# the camera model
#
# How a clear photo becomes the sort of frame this webcam actually produces at
# night. Every step here is a real thing that happens in the sensor, in the
# order it happens, which is what makes the pairs worth training on.
# ---------------------------------------------------------------------------

def degrade(clear, rng):
    """One dark noisy frame from one clear frame, plus the stack count used.

    The stack count matters: at run time the brain is fed the average of
    several stills, so some of the training frames are averaged too. A brain
    trained only on single frames would over-correct for noise that the
    averaging has already removed.
    """
    import cv2
    import numpy as np

    # How much exposure was lost. Log-uniform, because the difference between
    # dusk and full dark is multiplicative.
    scale = float(np.exp(rng.uniform(np.log(0.03), np.log(0.30))))

    # A colour cast, which is what sodium and LED street lighting does.
    cast = rng.uniform(0.88, 1.12, size=3).astype(np.float32)

    # A slightly soft lens, wide open in the dark.
    blur = float(rng.uniform(0.0, 0.9))

    dark = clear * scale * cast[None, None, :]
    if blur > 0.05:
        dark = cv2.GaussianBlur(dark, (0, 0), blur)

    # Shot noise is Poisson in the photon count, so it has to be applied in
    # photons and not as a flat gaussian - that is why dark pixels are noisier
    # than bright ones, which is the whole problem being solved here.
    photons = float(rng.uniform(40, 500))
    read_sigma = float(rng.uniform(0.002, 0.020))

    frames = int(rng.choice([1, 1, 2, 4, 8]))
    stack = np.zeros_like(dark)
    for _ in range(frames):
        shot = rng.poisson(np.clip(dark, 0, None) * photons) / photons
        noisy = shot + rng.normal(0, read_sigma, dark.shape)
        stack += np.clip(noisy, 0, 1)
    dark = (stack / frames).astype(np.float32)

    # JPEG, because the stills are JPEGs and its blocking is part of what the
    # brain has to see through.
    quality = int(rng.integers(62, 96))
    ok, buffer = cv2.imencode('.jpg', (dark * 255).astype(np.uint8),
                              [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if ok:
        dark = cv2.imdecode(buffer, cv2.IMREAD_COLOR).astype(np.float32) / 255.0
    return dark, frames


def targets_for(dark, clear, family=None):
    """What the brain should have said, for every pixel.

    The gain target is simply how much brighter the clear pixel is than the
    dark one. The saturation target is what the colour needs after that gain
    has been applied, so that the two outputs together land on the clear
    photo's own colour rather than on a lifted grey version of it.

    Both targets are clipped to the family's mandate, so a brain is never
    shown an answer it is not allowed to give: the targets, the decode at
    inference and the recorded range all come from the same place. For four of
    the five families the floor on the gain is 1.0, so a pixel is never a
    target for being made darker than the camera recorded it; bright_sun is
    the exception and its range sits below 1.
    """
    import cv2
    import numpy as np

    eps = 0.012
    y_dark = cv2.cvtColor((dark * 255).astype(np.uint8),
                          cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
    y_clear = cv2.cvtColor((clear * 255).astype(np.uint8),
                           cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0

    mandate = brain_mandate(family)
    g_low = max(0.05, float(mandate['gain_min']))
    g_high = max(g_low * 1.0001, float(mandate['gain_max']))
    s_low = float(mandate['sat_min'])
    s_high = max(s_low * 1.0001, float(mandate['sat_max']))

    gain = (y_clear + eps) / (y_dark + eps)
    gain = np.clip(gain, g_low, g_high)
    # 0 to 1 across the family's own range, which is the same mapping the
    # decode at inference reverses.
    t_gain = np.log(gain / g_low) / np.log(g_high / g_low)

    chroma_dark = dark.max(axis=2) - dark.min(axis=2)
    chroma_clear = clear.max(axis=2) - clear.min(axis=2)
    want = (chroma_clear + eps) / ((chroma_dark + eps) * gain)
    want = np.clip(want, s_low, s_high)
    t_sat = np.log(want / s_low) / np.log(s_high / s_low)
    return np.stack([t_gain, t_sat], axis=-1).astype(np.float32)


# ---------------------------------------------------------------------------
# the brain
# ---------------------------------------------------------------------------

def foundation_weights():
    """The nine hand-weighted detector units, frozen.

    Each column is one unit: twelve weights into a tanh, set by hand so that
    the unit fires on the thing it is named after. The feature order is the
    order feature_planes builds them in:

      0 luma         1 mean9        2 mean33      3 mean97
      4 detail       5 std17        6 std65       7 frame mean
      8 frame p99    9 local max   10 colour     11 near-black
    """
    import numpy as np

    weights = np.zeros((BRAIN_G_FEATURES, BRAIN_G_UNITS), dtype=np.float32)
    bias = np.zeros(BRAIN_G_UNITS, dtype=np.float32)

    # 0 DEEP_SHADOW: dark pixel in a dark neighbourhood.
    weights[0, 0], weights[2, 0], weights[3, 0], bias[0] = -4.0, -3.0, -2.0, 1.6
    # 1 NEAR_BLACK_CLIPPED: the near-black feature says it outright.
    weights[11, 1], weights[0, 1], bias[1] = 5.0, -3.0, -0.6
    # 2 LOCAL_DETAIL: local contrast at either scale.
    weights[5, 2], weights[6, 2], bias[2] = 3.5, 2.0, -0.9
    # 3 GLARE_NEARBY: something bright within the neighbourhood but not here.
    weights[9, 3], weights[0, 3], bias[3] = 4.5, -2.0, -1.4
    # 4 FLAT_NOISE: dark and featureless, which is noise and not scene.
    weights[0, 4], weights[5, 4], weights[6, 4], bias[4] = -3.5, -3.0, -2.0, 1.4
    # 5 MIDTONE_BODY: already about where it should be.
    weights[0, 5], weights[2, 5], bias[5] = 3.0, 1.5, -1.5
    # 6 HIGHLIGHT_GUARD: bright enough that any gain clips it.
    weights[0, 6], weights[9, 6], bias[6] = 5.0, 1.0, -3.4
    # 7 COLOUR_PRESENT: there is chroma worth preserving.
    weights[10, 7], bias[7] = 4.5, -1.1
    # 8 GLOBAL_DARK_SCENE: the whole frame is dark, not just this part of it.
    weights[7, 8], weights[8, 8], bias[8] = -6.0, -2.5, 1.9
    return weights, bias


def unit_names():
    return ['DEEP_SHADOW', 'NEAR_BLACK_CLIPPED', 'LOCAL_DETAIL', 'GLARE_NEARBY',
            'FLAT_NOISE', 'MIDTONE_BODY', 'HIGHLIGHT_GUARD', 'COLOUR_PRESENT',
            'GLOBAL_DARK_SCENE']


def _keras():
    """keras, or a reported failure. TensorFlow installed-but-broken is the
    usual way this falls over - a numpy clash, or a conda build next to a pip
    one - and from inside a training run it looks like an unrelated crash, so
    the failure is written down here where the cause is still in view."""
    os.environ.setdefault('TF_CPP_MIN_LOG_LEVEL', '2')
    try:
        from tensorflow import keras                           # noqa: PLC0415
    except Exception as problem:                               # noqa: BLE001
        try:
            import webcam_errors                               # noqa: PLC0415
            webcam_errors.report('TensorFlow.import', problem, detail=(
                'brain G cannot be built or trained without a working '
                'TensorFlow - the correction falls back to the analytic '
                'lift until this is fixed.  pip install tensorflow'))
        except ImportError:
            pass
        raise
    return keras


def build_brain():
    """12 -> 9 FROZEN, then the 9 and the raw 12 together -> 24 -> 16 -> 2.

    The frozen foundation is as the other brains have it. The one addition is
    that the head is given the twelve raw features alongside the nine detector
    outputs, and that was measured rather than assumed. Three shapes were
    tried on held-back photos, against the score a flat "lift everything the
    same amount" answer gets:

        frozen 9 -> 6 -> 4 -> 2                 32% better than flat
        frozen 9 -> 24 -> 16 -> 2               34% better
        frozen 9 + raw 12 -> 24 -> 16 -> 2      41% better
        an unconstrained 96-96-64 on raw only   39% better

    The reason the detectors alone leave something behind is that a tanh unit
    saturates: DEEP_SHADOW reads 0.97 whether the pixel is at 2 out of 255 or
    at 8, and the difference between those two is most of what decides the
    gain. The raw features carry that back. And the last line is the useful
    one - the frozen foundation with the raw path beats a big unconstrained
    net several times its size, so the hand-weighted detectors are genuinely
    adding information rather than just constraining the fit.
    """
    keras = _keras()

    weights, bias = foundation_weights()
    pixel = keras.layers.Input(shape=(BRAIN_G_FEATURES,), name='pixel_features')
    foundation = keras.layers.Dense(BRAIN_G_UNITS, activation='tanh',
                                    name='foundation')
    detected = foundation(pixel)
    together = keras.layers.Concatenate(name='detectors_and_raw')([detected, pixel])
    head = keras.layers.Dense(24, activation='tanh', name='head_1')(together)
    head = keras.layers.Dense(16, activation='tanh', name='head_2')(head)
    out = keras.layers.Dense(2, activation='sigmoid',
                             name='gain_and_saturation')(head)
    model = keras.Model(pixel, out, name='brain_g_night')

    foundation.set_weights([weights, bias])
    # FROZEN, and it stays frozen: the head learns on top of a fixed reading
    # of the pixel rather than being free to redefine what the detectors mean.
    foundation.trainable = False

    model.compile(optimizer=keras.optimizers.Adam(2e-3), loss='mse',
                  metrics=['mae'])
    return model


# ---------------------------------------------------------------------------
# the training set
# ---------------------------------------------------------------------------

def load_photos(folder, limit=400, want_clear=True):
    """Clear, well exposed colour photographs from a folder.

    Anything already dark, flat or nearly greyscale is left out: a photo that
    is itself underexposed cannot be the answer sheet for underexposure.
    """
    paths = []
    for pattern in ('*.jpg', '*.jpeg', '*.png', '*.JPG', '*.JPEG', '*.PNG'):
        paths += glob.glob(os.path.join(folder, pattern))
    paths.sort()
    return load_photo_paths(paths, limit=limit, want_clear=want_clear)


def load_photo_paths(paths, limit=400, want_clear=True):
    """The same loader, over a list of files picked out somewhere else -
    which is how one family's harvested frames are loaded without the rest of
    the corpus coming with them.
    """
    import cv2

    photos = []
    for path in paths[:limit * 3]:
        image = cv2.imread(path, cv2.IMREAD_COLOR)
        if image is None or image.ndim != 3:
            continue
        if min(image.shape[:2]) < 160:
            continue
        if want_clear:
            grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            if not (70 <= grey.mean() <= 190 and grey.std() >= 32):
                continue
        # Nothing is gained from a 4000 pixel wide photo; the crops are small.
        if max(image.shape[:2]) > 1200:
            factor = 1200 / max(image.shape[:2])
            image = cv2.resize(image, (int(image.shape[1] * factor),
                                       int(image.shape[0] * factor)))
        photos.append((path, image.astype('float32') / 255.0))
        if len(photos) >= limit:
            break
    return photos


def make_samples(photos, count, rng, crop=128, report=None):
    """Turn photos into (features, targets) rows, one row per pixel sampled.

    Every crop is degraded on its own, so one photo contributes many different
    nights: different exposure loss, different noise, a different stack depth.
    """
    import numpy as np

    features = np.zeros((count, BRAIN_G_FEATURES), dtype=np.float32)
    targets = np.zeros((count, 2), dtype=np.float32)
    filled = 0
    stacks = []

    while filled < count:
        _, photo = photos[int(rng.integers(len(photos)))]
        height, width = photo.shape[:2]
        size = min(crop, height, width)
        top = int(rng.integers(0, height - size + 1))
        left = int(rng.integers(0, width - size + 1))
        clear = photo[top:top + size, left:left + size].copy()

        dark, frames = degrade(clear, rng)
        stacks.append(frames)
        planes = feature_planes(dark)
        answers = targets_for(dark, clear, family=ACTIVE_BRAIN)

        # Take a scatter of pixels from this crop rather than all of them:
        # neighbouring pixels say almost the same thing, and a spread of crops
        # is worth more than every pixel of a few.
        take = min(256, count - filled)
        ys = rng.integers(0, size, take)
        xs = rng.integers(0, size, take)
        features[filled:filled + take] = planes[ys, xs]
        targets[filled:filled + take] = answers[ys, xs]
        filled += take
        if report and filled % 4096 < take:
            report(f'  built {filled} of {count} samples')

    return features, targets, stacks


def photo_split(photos, fraction=0.2):
    """Hold some photos out entirely, rather than holding out pixels.

    This is the same leakage problem the trading brain had with a random
    shuffle. Two pixels from the same crop of the same photo are nearly the
    same sample, so splitting on pixels would let the brain see its own
    validation data and the validation score would mean nothing. Splitting on
    whole photos is the honest version.
    """
    cut = max(1, int(len(photos) * fraction))
    return photos[cut:], photos[:cut]


# ---------------------------------------------------------------------------
# training
# ---------------------------------------------------------------------------

def evaluate(model, val_photos, rng, trials=2):
    """Score the whole pipeline on photos the brain never saw.

    The validation loss says how well the brain predicted its two numbers. It
    does not say whether the finished picture is any better, which is the only
    question that matters - so this runs the real thing: eight degraded frames
    of a held-back photo, averaged, CLAHE'd, then lifted, and compares the
    result with the clear original it came from.

    The analytic correct is scored on the same frames, because "better than
    the simple method" is the claim that has to be earned. Returns a dict for
    meta.json, or {} if it could not be done.
    """
    import cv2
    import numpy as np

    try:
        import webcam_enhance                                 # noqa: PLC0415
    except ImportError:
        return {}
    try:
        from skimage.metrics import structural_similarity     # noqa: PLC0415
    except ImportError:
        structural_similarity = None

    def psnr(one, two):
        error = np.mean((one.astype(np.float64) - two.astype(np.float64)) ** 2)
        return 99.0 if error < 1e-9 else float(10 * np.log10(255 * 255 / error))

    def similarity(one, two):
        if structural_similarity is None:
            return None
        return float(structural_similarity(cv2.cvtColor(one, cv2.COLOR_BGR2GRAY),
                                           cv2.cvtColor(two, cv2.COLOR_BGR2GRAY)))

    enhancer = webcam_enhance.Enhancer()
    stages = {'dark_input': [], 'stack_and_clahe': [], 'analytic': [], 'brain': []}
    frames_used = 8

    for _, clear in val_photos:
        for _ in range(trials):
            reference = (clear * 255).astype(np.uint8)
            darks = [degrade(clear, rng)[0] for _ in range(frames_used)]
            single = (darks[-1] * 255).astype(np.uint8)
            stacked = np.clip(np.mean([d * 255 for d in darks], axis=0),
                              0, 255).astype(np.uint8)

            lab = cv2.cvtColor(stacked, cv2.COLOR_BGR2LAB)
            light, green_red, blue_yellow = cv2.split(lab)
            clahe = cv2.createCLAHE(
                clipLimit=webcam_enhance.CLAHE_CLIP,
                tileGridSize=(webcam_enhance.CLAHE_TILES,) * 2)
            equalized = cv2.cvtColor(
                cv2.merge((clahe.apply(light), green_red, blue_yellow)),
                cv2.COLOR_LAB2BGR)

            lifted = enhancer._analytic_lift(equalized)
            brained = enhancer._brain_lift((model, {}, ''), equalized)
            if brained is None:
                return {}

            for name, image in (('dark_input', single),
                                ('stack_and_clahe', equalized),
                                ('analytic', lifted),
                                ('brain', brained)):
                stages[name].append((psnr(image, reference), similarity(image, reference)))

    out = {'frames_stacked': frames_used,
           'photos_scored': len(val_photos),
           'trials_each': trials}
    for name, scores in stages.items():
        if not scores:
            continue
        out[f'{name}_psnr_db'] = round(float(np.mean([s[0] for s in scores])), 2)
        have = [s[1] for s in scores if s[1] is not None]
        if have:
            out[f'{name}_ssim'] = round(float(np.mean(have)), 3)
    if 'brain_psnr_db' in out and 'analytic_psnr_db' in out:
        out['brain_beats_analytic_db'] = round(
            out['brain_psnr_db'] - out['analytic_psnr_db'], 2)
    return out


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(65536), b''):
            digest.update(block)
    return digest.hexdigest()


def make_read_only(path):
    """Read-only, because inference must never write to the foundation."""
    try:
        os.chmod(path, 0o444)
        return True
    except OSError:
        return False


def train(photo_folder=None, force=False, samples=PRETRAIN_SAMPLES, quiet=False,
          use_corpus=False, preset=None):
    import numpy as np

    def say(line):
        if not quiet:
            print(line, flush=True)

    # Everything below writes to MODEL_PATH/META_PATH, so the one thing that
    # decides which brain this run trains is this call.
    family = use_brain(preset)
    label = f'brain {family}' if family else 'brain G'

    if os.path.isfile(MODEL_PATH) and not force:
        say(f'{MODEL_PATH} is already there. --force to retrain over it.')
        return 0

    photos = []
    used = ''
    pairs = []
    trained_from = 'general photos'

    if use_corpus:
        # This camera's own frames, harvested as the scripts ran - and, when a
        # family was named, only the frames harvested under that family's own
        # presets, so a night brain never sees a daylight frame.
        photos = corpus_photos(family=family)
        used = os.path.join(CORPUS_DIR, 'clear')
        pairs = corpus_pairs(family=family)
        trained_from = ('this camera, ' + '/'.join(BRAIN_PRESETS[family])
                        if family else 'this camera')
        say(f'{label}: training on THIS CAMERA - {len(photos)} well-exposed '
            f'frames and {len(pairs)} real dark/clear pairs from {CORPUS_DIR}'
            + (f' under the {family} presets' if family else ''))
    else:
        folders = [photo_folder] if photo_folder else list(PHOTO_GUESSES)
        for folder in folders:
            if not folder or not os.path.isdir(folder):
                continue
            photos = load_photos(folder)
            if len(photos) >= 4:
                used = folder
                break

    if len(photos) < 4:
        print('Not enough clear photos to train on. Point --photos at a folder '
              'of clear, well exposed photographs - daytime stills from this '
              'camera are the best material there is.', file=sys.stderr)
        return 1

    say(f'brain G: {len(photos)} clear photos from {used}')

    train_photos, val_photos = photo_split(photos)
    say(f'brain G: {len(train_photos)} photos to train on, '
        f'{len(val_photos)} held back for validation (split by photo, not by '
        f'pixel, so no crop appears in both)')

    rng = np.random.default_rng(20000)
    started = time.time()
    say(f'brain G: building {samples} training samples')
    x_train, y_train, stacks = make_samples(train_photos, samples, rng,
                                            report=say if not quiet else None)
    val_count = max(2048, samples // 5)
    say(f'brain G: building {val_count} validation samples')
    x_val, y_val, _ = make_samples(val_photos, val_count, rng)

    # The real pairs go in alongside the synthetic ones, and some are held
    # back. They are worth more per sample than anything synthetic - real
    # noise from this sensor with a real answer beside it - but there are
    # never many, so they supplement rather than replace.
    pair_note = 'none'
    if pairs:
        cut = max(1, int(len(pairs) * 0.25))
        val_pairs, train_pairs = pairs[:cut], pairs[cut:]
        if train_pairs:
            want = min(samples // 3, len(train_pairs) * 1024)
            px, py = make_pair_samples(train_pairs, want, rng)
            if len(px):
                x_train = np.concatenate([x_train, px])
                y_train = np.concatenate([y_train, py])
        if val_pairs:
            vx, vy = make_pair_samples(val_pairs, max(1024, val_count // 4), rng)
            if len(vx):
                x_val = np.concatenate([x_val, vx])
                y_val = np.concatenate([y_val, vy])
        pair_note = (f'{len(train_pairs)} pairs trained on, {len(val_pairs)} '
                     f'held back')
        say(f'brain G: real pairs from this camera - {pair_note}')

    say(f'brain G: samples built in {time.time() - started:.0f}s, '
        f'{len(x_train)} training samples, stack depths used '
        f'{sorted(set(stacks))}')

    keras = _keras()
    model = build_brain()
    say('brain G: 12 -> 9 FROZEN + raw 12 -> 24 -> 16 -> 2, foundation units: '
        + ', '.join(unit_names()))

    stop = keras.callbacks.EarlyStopping(monitor='val_loss',
                                         patience=EARLY_STOP_PATIENCE,
                                         restore_best_weights=True)
    history = model.fit(x_train, y_train, validation_data=(x_val, y_val),
                        epochs=EPOCHS, batch_size=BATCH_SIZE,
                        callbacks=[stop], verbose=0 if quiet else 2)

    epochs_run = len(history.history['loss'])
    val_loss = float(history.history['val_loss'][-1])
    val_mae = float(history.history['val_mae'][-1])

    # What a flat answer would have scored, so the numbers above mean
    # something. A brain that cannot beat "lift everything the same amount"
    # is not worth running.
    baseline = float(np.mean((y_val - y_train.mean(axis=0)) ** 2))

    say('brain G: scoring the whole pipeline on the held-back photos')
    try:
        scored = evaluate(model, val_photos, rng)
    except Exception as problem:                              # noqa: BLE001
        say(f'brain G: could not score the pipeline ({problem})')
        scored = {}
    if scored:
        say(f'  dark input          {scored.get("dark_input_psnr_db")} dB')
        say(f'  stack + CLAHE       {scored.get("stack_and_clahe_psnr_db")} dB')
        say(f'  + analytic correct  {scored.get("analytic_psnr_db")} dB')
        say(f'  + brain G           {scored.get("brain_psnr_db")} dB '
            f'({scored.get("brain_beats_analytic_db")} dB better than analytic)')

    os.makedirs(MODEL_DIR, exist_ok=True)
    for path in (MODEL_PATH, META_PATH):
        if os.path.exists(path):
            try:
                os.chmod(path, 0o644)
            except OSError:
                pass
    model.save(MODEL_PATH)

    _mandate = brain_mandate(family)
    meta = {
        'brain': f'brain_{family}' if family else 'brain_g_night',
        'brain_family': family,
        'brain_presets': list(BRAIN_PRESETS[family]) if family else None,
        'feature_count': BRAIN_G_FEATURES,
        'foundation_units': unit_names(),
        'foundation_frozen': True,
        'architecture': '12 -> 9 frozen -> (9 + raw 12) -> 24 -> 16 -> 2',
        # Both outputs are 0 to 1 across this brain's own range, so where
        # "no change" sits depends on the range: 0.5 exactly when the
        # saturation range is symmetric about 1.0, a little either side when
        # it is not. Recorded rather than assumed, because the tile reads it.
        'outputs': [
            f'gain_0_1_over_{_mandate["gain_min"]}_to_{_mandate["gain_max"]}',
            f'saturation_0_1_over_{_mandate["sat_min"]}_to_'
            f'{_mandate["sat_max"]}_no_change_at_'
            f'{math.log(1.0 / _mandate["sat_min"]) / math.log(_mandate["sat_max"] / _mandate["sat_min"]):.3f}',
        ],
        'gain_max': GAIN_MAX,
        # The range this brain's outputs decode into, and the rest of what it
        # is allowed to do. webcam_enhance reads these back rather than
        # assuming, so a brain always decodes against the range it was
        # trained against even if the mandate here changes later.
        'mandate': _mandate,
        'pretrain_samples': samples,
        'pretrain_cap': PRETRAIN_CAP,
        'epochs_run': epochs_run,
        'epochs_max': EPOCHS,
        'val_loss': round(val_loss, 6),
        'val_mae': round(val_mae, 5),
        'flat_answer_val_loss': round(baseline, 6),
        'photos_trained_on': len(train_photos),
        'photos_held_back': len(val_photos),
        'photo_folder': used,
        'trained_at': time.strftime('%Y-%m-%d %H:%M:%S'),
        'tensorflow': __import__('tensorflow').__version__,
        'read_only': True,
        'evaluation': scored,
        'trained_from': trained_from,
        'real_pairs': pair_note,
        'corpus': corpus_state(family) if use_corpus else None,
    }
    with open(META_PATH, 'w', encoding='utf-8') as handle:
        json.dump(meta, handle, indent=2)

    meta['model_sha256'] = sha256_of(MODEL_PATH)
    with open(META_PATH, 'w', encoding='utf-8') as handle:
        json.dump(meta, handle, indent=2)

    make_read_only(MODEL_PATH)
    make_read_only(META_PATH)

    say(f'brain G: trained {epochs_run} epochs, val loss {val_loss:.5f} '
        f'(a flat answer scores {baseline:.5f}), val mae {val_mae:.4f}')
    say(f'brain G: saved {MODEL_PATH} read-only, sha256 '
        f'{meta["model_sha256"][:16]}...')

    # Left for the caller, so self_train_if_ready can score the shipped brain
    # on exactly the same held-back frames before deciding anything.
    train.last = {'val_photos': val_photos, 'meta': meta, 'model': model,
                  'rng': rng}
    return 0


def pretrain_all(photo_folder=None, force=False, samples=PRETRAIN_SAMPLES,
                 quiet=False, report=None, families=None):
    """Pretrain one brain per lighting preset from the same clear photos.

    This is the first-run path. Five brains come out of it, one per lighting
    regime, each written to its own folder - which is what the corrector looks
    for once a preset is applied. Training a single brain here instead would
    write the old all-day folder, and that folder is not read at all once a
    preset is in force, so the work would be thrown away.

    The five are trained from the SAME photos; what differs is the range each
    one's answers are clipped and encoded into, which is its mandate. So the
    night brain comes out of this knowing only how to brighten, and the bright
    sun brain only how to pull a level down - before either has seen a single
    frame of this camera. The harvest then specialises them further, each on
    its own presets' frames.

    Returns (how_many_trained, lines).
    """
    def say(line):
        if report:
            report(line)
        elif not quiet:
            print(line, flush=True)

    trained, lines = 0, []
    for family in (families or BRAIN_FAMILIES):
        say(f'=== brain {family}: {BRAIN_MANDATE[family]["note"]}')
        try:
            outcome = train(photo_folder=photo_folder, force=force,
                            samples=samples, quiet=quiet, preset=family)
        except Exception as problem:                          # noqa: BLE001
            line = f'brain {family}: training failed - {problem}'
            lines.append(line)
            say(line)
            continue
        if outcome == 0:
            trained += 1
            lines.append(f'brain {family}: trained')
        else:
            lines.append(f'brain {family}: not trained')
    say(f'pretrained {trained} of {len(families or BRAIN_FAMILIES)} brains')
    return (trained, lines)


def delete_brain(folder):
    """Delete a brain folder. Returns (deleted, note)."""
    model = os.path.join(folder, 'model.keras')
    if not os.path.exists(model):
        return (False, f'nothing to delete in {folder}')
    gone = []
    for name in ('model.keras', 'meta.json'):
        path = os.path.join(folder, name)
        if not os.path.exists(path):
            continue
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
        try:
            os.remove(path)
            gone.append(name)
        except OSError as problem:
            return (False, f'could not delete {path}: {problem}')
    try:
        os.rmdir(folder)
    except OSError:
        pass
    return (True, f'deleted {", ".join(gone)} from {folder}')


def reset_and_retrain(quiet=False, report=None, preset=None, every=False):
    """Throw the current brain away and train a fresh one from the harvest.

    This is the button in the window, and it exists because a trained model can
    come out wrong in a way no score catches - if the corrected stills are too
    dark, or washed out, or the colour has gone strange, the brain is what did
    it and the fix is to delete it and train again on what has been harvested
    since.

    It deletes BOTH brains: the one trained on this camera and the shipped one
    if it is still there. Then it trains a new one from the corpus. Nothing is
    lost that cannot be rebuilt - the corpus of harvested frames is untouched,
    and it is the corpus, not the model, that is the valuable part.

    If there is not yet enough harvested to train on, it says so and leaves
    the analytic correct doing the work until there is. That is a real state
    and not a failure: the correction still runs, one tier down.

    ONE BRAIN, the one for the preset in force. The button is pressed because
    the picture on the screen right now looks wrong, and the brain that
    produced it is the brain for the light that is on - the other four had
    nothing to do with it and are left alone, trained and intact. Pass
    every=True to delete all of them, which is the only way to get the old
    behaviour and is not what the button does.

    Returns (trained, line).
    """
    def say(text):
        if report:
            report(text)
        elif not quiet:
            print(text, flush=True)

    # Delete the brain trained on this camera - the one that has been doing
    # the correcting, and so the one that produced the bad pictures. The
    # shipped brain, if it is still here, is deliberately NOT deleted yet: it
    # is the benchmark the new one has to beat, and it is also what keeps the
    # correction working if there turns out not to be enough harvested to
    # train on. self_train_if_ready retires it in the usual way once the new
    # brain is shown to be better.
    # Which brains to throw away. The default is the one brain that is active
    # - the one that produced the picture the user is unhappy with. every=True
    # is the explicit "all of them", including the old all-day folder.
    if every:
        families = list(BRAIN_FAMILIES) + [None]
    elif preset is not None:
        families = [brain_family(preset)]
    else:
        families = [active_brain()[0]]
    notes = []
    for family in families:
        done, note = delete_brain(brain_dir_for(family))
        if done:
            label = f'brain {family}' if family else 'brain G'
            notes.append(f'{label}: {note}')
            say(f'{label}: {note}')
    if not notes:
        say('brain G: there was no brain trained on this camera to delete')

    # Let it train again right away rather than waiting out the 24 hour gap.
    index = read_index()
    index['last_trained'] = 0
    stamps = index.get('last_trained_by_brain') or {}
    for family in families:
        stamps.pop(family, None)
    index['last_trained_by_brain'] = stamps
    write_index(index)

    retrain_for = None if every else families[0]
    state = corpus_state(retrain_for)
    ready, why = ready_to_train(state, family=retrain_for)
    if not ready:
        shipped = os.path.isfile(os.path.join(SHIPPED_DIR, 'model.keras'))
        deleted = (f'brain {retrain_for}' if retrain_for and not every
                   else 'the brains')
        line = (f'{deleted}: deleted. Not enough harvested to train a new one yet '
                f'- {why}. '
                + ('The shipped brain is doing the correcting in the meantime.'
                   if shipped else
                   'The analytic correct is doing the work until then.')
                + ' The harvest carries on and it will train itself as soon as '
                  'it has enough.')
        say(line)
        return (False, line)

    if not every:
        label = f'brain {retrain_for}' if retrain_for else 'brain G'
        say(f'{label}: training a fresh brain on this camera...')
        did, line = self_train_if_ready(force=True, quiet=quiet,
                                        preset=retrain_for)
    else:
        # Every family that has enough harvested under its own presets gets a
        # fresh brain; the others wait for the harvest and run analytic in the
        # meantime, which is a real state rather than a failure.
        did, lines = False, []
        for family in BRAIN_FAMILIES:
            trained, text = self_train_if_ready(force=True, quiet=quiet,
                                                preset=family)
            did = did or trained
            lines.append(text)
            say(text)
        line = ' | '.join(lines)
    if notes:
        line = '; '.join(notes) + '. ' + line
    say(line)
    return (did, line)


def self_train_if_ready(force=False, quiet=False, preset=None):
    """Retrain on this camera's own frames, and retire the shipped brain.

    This is what the light meter calls. It does nothing at all until the
    harvest has a real spread of this camera's well exposed frames, because a
    brain trained on one afternoon of one preset would be worse than the
    shipped one, not better.

    When it does train, the new brain is compared with the shipped one on the
    SAME held-back frames from this camera - which is the only comparison that
    answers the question being asked, since the two were trained on different
    material and their own training scores cannot be read against each other.
    The shipped brain is deleted once the new one is no worse. If the new one
    comes out worse, it is still what gets used - it is the one trained on
    this camera - but the shipped copy is kept and said so, loudly, rather
    than being thrown away on a bad training run.

    Returns (did_train, line).
    """
    # Which brain this run is for. With nothing passed it is the preset in
    # force, which is what the light meter wants: the brain being used right
    # now is the one worth retraining on the frames coming in right now.
    family = brain_family(preset) if preset is not None else brain_family()
    label = f'brain {family}' if family else 'brain G'

    state = corpus_state(family)
    ready, why = ready_to_train(state, family=family)
    if not ready:
        return (False, f'{label}: not retraining yet - {why}')

    index = read_index()
    stamps = index.get('last_trained_by_brain') or {}
    last = stamps.get(family, 0) if family else index.get('last_trained', 0)
    if last and not force and time.time() - last < RETRAIN_EVERY:
        hours = (RETRAIN_EVERY - (time.time() - last)) / 3600.0
        return (False, f'{label}: trained on this camera already, next retrain '
                       f'in {hours:.1f}h ({state["clear_frames"]} frames banked)')

    had_shipped = os.path.isfile(os.path.join(SHIPPED_DIR, 'model.keras'))
    outcome = train(force=True, quiet=quiet, use_corpus=True, preset=family)
    if outcome != 0 or not train.last:
        return (False, f'{label}: training on this camera did not complete')

    details = train.last
    new_meta = details['meta']
    new_score = (new_meta.get('evaluation') or {}).get('brain_psnr_db')

    # Score the shipped brain on the same held-back frames from this camera.
    shipped_score = None
    if had_shipped:
        try:
            keras = _keras()
            shipped = keras.models.load_model(
                os.path.join(SHIPPED_DIR, 'model.keras'), compile=False)
            shipped_eval = evaluate(shipped, details['val_photos'],
                                    details['rng'])
            shipped_score = shipped_eval.get('brain_psnr_db')
        except Exception:                                     # noqa: BLE001
            shipped_score = None

    index = read_index()
    now = int(time.time())
    index['last_trained'] = now
    if family:
        stamps = index.get('last_trained_by_brain') or {}
        stamps[family] = now
        index['last_trained_by_brain'] = stamps
    index['trainings'] = index.get('trainings', 0) + 1
    write_index(index)

    line = (f'{label}: retrained on this camera - {state["clear_frames"]} '
            f'well-exposed frames, {state["real_pairs"]} real pairs, '
            f'{state["bucket_count"]} preset/light combinations')
    if isinstance(new_score, (int, float)):
        line += f'; scores {new_score:.2f} dB on its own held-back frames'

    # Record the comparison where the window can show it.
    verdict = None
    if isinstance(new_score, (int, float)) and isinstance(shipped_score, (int, float)):
        verdict = {'own_psnr_db': new_score, 'shipped_psnr_db': shipped_score,
                   'own_is_better': new_score >= shipped_score - 0.05}
        line += f' against the shipped brain\'s {shipped_score:.2f} dB on the same frames'

    if verdict is None or verdict['own_is_better']:
        done, note = retire_shipped_brain()
        line += f'. {note}' if done else ''
        if verdict is not None:
            verdict['shipped_deleted'] = bool(done)
    else:
        line += ('. The shipped brain scored better on these frames, so it has '
                 'NOT been deleted - the brain trained on this camera is still '
                 'the one being used, but there is probably not enough variety '
                 'in the harvest yet. It will retrain tomorrow.')
        if verdict is not None:
            verdict['shipped_deleted'] = False

    if verdict is not None:
        try:
            os.chmod(META_PATH, 0o644)
            with open(META_PATH, encoding='utf-8') as handle:
                stored = json.load(handle)
            stored['against_shipped'] = verdict
            with open(META_PATH, 'w', encoding='utf-8') as handle:
                json.dump(stored, handle, indent=2)
            make_read_only(META_PATH)
        except (OSError, ValueError):
            pass

    return (True, line)


train.last = {}


def main():
    # Anything that gets all the way out of the training run lands in the same
    # log the windows tail, so a TensorFlow failure during a retrain shows up
    # in the terminal pane rather than only in whatever terminal started it.
    try:
        import webcam_errors                                   # noqa: PLC0415
        webcam_errors.install_excepthook('train_webcam_brain')
    except ImportError:
        pass
    parser = argparse.ArgumentParser(
        description='Train brain G, the night vision brain, on clear photos.')
    parser.add_argument('--photos', help='folder of clear, well exposed photos')
    parser.add_argument('--force', action='store_true',
                        help='retrain over a model that is already there')
    parser.add_argument('--samples', type=int, default=PRETRAIN_SAMPLES,
                        help=f'pretrain samples (default {PRETRAIN_SAMPLES})')
    parser.add_argument('--quiet', action='store_true')
    parser.add_argument('--self-train', action='store_true',
                        help="train on this camera's harvested frames and "
                             'retire the shipped brain')
    parser.add_argument('--harvest', metavar='FRAME',
                        help='consider one still for the training set')
    parser.add_argument('--preset',
                        help='which brain to work on: a preset name (night, '
                             'night_mono, golden_am, day, bright_sun, '
                             'golden_pm) or a family name (night, golden_am, '
                             'day, bright_sun, golden_pm). Used by --harvest '
                             'to file the '
                             'frame, and by training to pick the brain. With '
                             'nothing passed, training uses the preset in '
                             'force.')
    parser.add_argument('--all-presets', action='store_true',
                        help='every brain rather than one: with --self-train, '
                             "retrain each on its own harvested frames; with "
                             '--reset-retrain, delete all five. Pretraining '
                             'from photos does all five by default already')
    parser.add_argument('--gain-fraction', type=float,
                        help='0 to 1, how hard the camera is working, for --harvest')
    parser.add_argument('--corpus-state', action='store_true',
                        help='what has been harvested so far')
    parser.add_argument('--reset-retrain', action='store_true',
                        help="delete the brain for the preset in force (or "
                             'the one named by --preset) and train a new one '
                             'from the harvest. The other brains are left '
                             'alone; add --all-presets to delete every one')
    args = parser.parse_args()

    if args.corpus_state:
        state = corpus_state()
        ready, why = ready_to_train(state)
        print(f'corpus: {state["clear_frames"]} well-exposed frames, '
              f'{state["dim_frames"]} dark ({state["real_pairs"]} paired), '
              f'{state["bucket_count"]} preset/light combinations, '
              f'{state["hours_covered"]}h covered, in {state["folder"]}')
        print('presets seen: ' + (', '.join(state['presets']) or 'none'))
        print(('ready to train: ' if ready else 'not ready: ') + why)
        # And the same again per brain, which is what actually decides whether
        # each specialised brain can train.
        for family in BRAIN_FAMILIES:
            one = corpus_state(family)
            ok, note = ready_to_train(one, family=family)
            trained = os.path.isfile(os.path.join(brain_dir_for(family),
                                                  'model.keras'))
            print(f'  brain {family}: {one["clear_frames"]} clear, '
                  f'{one["real_pairs"]} pairs, {one["bucket_count"]} buckets, '
                  f'{"trained" if trained else "no model yet"} - '
                  + ('ready' if ok else note))
        return 0

    if args.harvest:
        kept, why = harvest(args.harvest, preset=args.preset,
                            gain_fraction=args.gain_fraction)
        print(('harvested: ' if kept else 'not harvested: ') + why)
        return 0

    if args.reset_retrain:
        did, line = reset_and_retrain(quiet=args.quiet, preset=args.preset,
                                      every=args.all_presets)
        if args.quiet:
            print(line)
        return 0 if did else 1

    if args.self_train:
        if args.all_presets:
            # One brain per family, each on its own lighting regime only.
            any_done = False
            for family in BRAIN_FAMILIES:
                done, line = self_train_if_ready(force=args.force,
                                                 quiet=args.quiet,
                                                 preset=family)
                any_done = any_done or done
                print(line)
            return 0 if any_done else 1
        did, line = self_train_if_ready(force=args.force, quiet=args.quiet,
                                        preset=args.preset)
        print(line)
        return 0 if did else 1

    if args.preset:
        # One named brain.
        return train(photo_folder=args.photos, force=args.force,
                     samples=min(args.samples, PRETRAIN_CAP),
                     quiet=args.quiet, preset=args.preset)

    # No preset named: pretrain all five, which is what a first run wants.
    # Training one brain here would write the old all-day folder, and that
    # folder is not read once a preset is in force, so the work would be
    # thrown away. --preset is how to train just one.
    trained, _lines = pretrain_all(photo_folder=args.photos, force=args.force,
                                   samples=min(args.samples, PRETRAIN_CAP),
                                   quiet=args.quiet)
    return 0 if trained else 1


if __name__ == '__main__':
    raise SystemExit(main())
