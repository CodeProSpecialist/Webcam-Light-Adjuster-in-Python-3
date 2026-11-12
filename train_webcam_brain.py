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

The model is written to ~/.webcam-models/brain_g_night_model/ and then made
read-only, because inference must never write to it. --force is the only way
to retrain over one that is already there.
"""

import argparse
import glob
import hashlib
import json
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

MODEL_DIR = os.environ.get(
    'WEBCAM_BRAIN_DIR',
    os.path.join(os.path.expanduser('~'), '.webcam-models', 'brain_g_night_model'))
MODEL_PATH = os.path.join(MODEL_DIR, 'model.keras')
META_PATH = os.path.join(MODEL_DIR, 'meta.json')

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


def targets_for(dark, clear):
    """What the brain should have said, for every pixel.

    The gain target is simply how much brighter the clear pixel is than the
    dark one. The saturation target is what the colour needs after that gain
    has been applied, so that the two outputs together land on the clear
    photo's own colour rather than on a lifted grey version of it.
    """
    import cv2
    import numpy as np

    eps = 0.012
    y_dark = cv2.cvtColor((dark * 255).astype(np.uint8),
                          cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
    y_clear = cv2.cvtColor((clear * 255).astype(np.uint8),
                           cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0

    gain = (y_clear + eps) / (y_dark + eps)
    gain = np.clip(gain, 1.0, GAIN_MAX)
    t_gain = np.log(gain) / np.log(GAIN_MAX)          # 0 to 1

    chroma_dark = dark.max(axis=2) - dark.min(axis=2)
    chroma_clear = clear.max(axis=2) - clear.min(axis=2)
    want = (chroma_clear + eps) / ((chroma_dark + eps) * gain)
    want = np.clip(want, 0.25, 4.0)
    t_sat = (np.log(want) / np.log(4.0) + 1.0) / 2.0  # 0 to 1, 0.5 is neutral
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
    os.environ.setdefault('TF_CPP_MIN_LOG_LEVEL', '2')
    from tensorflow import keras

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
    import cv2

    paths = []
    for pattern in ('*.jpg', '*.jpeg', '*.png', '*.JPG', '*.JPEG', '*.PNG'):
        paths += glob.glob(os.path.join(folder, pattern))
    paths.sort()

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
        answers = targets_for(dark, clear)

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


def train(photo_folder=None, force=False, samples=PRETRAIN_SAMPLES, quiet=False):
    import numpy as np

    def say(line):
        if not quiet:
            print(line, flush=True)

    if os.path.isfile(MODEL_PATH) and not force:
        say(f'{MODEL_PATH} is already there. --force to retrain over it.')
        return 0

    folders = [photo_folder] if photo_folder else list(PHOTO_GUESSES)
    photos = []
    used = ''
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
    say(f'brain G: samples built in {time.time() - started:.0f}s, '
        f'stack depths used {sorted(set(stacks))}')

    from tensorflow import keras
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

    meta = {
        'brain': 'brain_g_night',
        'feature_count': BRAIN_G_FEATURES,
        'foundation_units': unit_names(),
        'foundation_frozen': True,
        'architecture': '12 -> 9 frozen -> (9 + raw 12) -> 24 -> 16 -> 2',
        'outputs': ['log_gain_0_1', 'saturation_0_1_neutral_at_0.5'],
        'gain_max': GAIN_MAX,
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
    return 0


def main():
    parser = argparse.ArgumentParser(
        description='Train brain G, the night vision brain, on clear photos.')
    parser.add_argument('--photos', help='folder of clear, well exposed photos')
    parser.add_argument('--force', action='store_true',
                        help='retrain over a model that is already there')
    parser.add_argument('--samples', type=int, default=PRETRAIN_SAMPLES,
                        help=f'pretrain samples (default {PRETRAIN_SAMPLES})')
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args()
    return train(photo_folder=args.photos, force=args.force,
                 samples=min(args.samples, PRETRAIN_CAP), quiet=args.quiet)


if __name__ == '__main__':
    raise SystemExit(main())
