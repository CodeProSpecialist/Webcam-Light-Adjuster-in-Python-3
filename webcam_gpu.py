"""GPU setup for the TensorFlow half of the brains.

Two jobs, both done once per process and both safe to call from anywhere:

1. ``configure()`` - called immediately after TensorFlow is imported, by
   both webcam_enhance and train_webcam_brain. It caps TensorFlow's memory
   appetite and then proves the GPU can actually run a convolution before
   any brain is loaded onto it. If it cannot, the GPU is hidden and
   everything carries on at CPU speed rather than dying mid-frame.
2. ``advise()`` - called by the bootstrap when a GPU is present and the
   CUDA libraries are not. It prints the pip command and nothing else.

Why ``advise()`` only prints, where the apt side of webcam_bootstrap
offers to install: the bootstrap's third rule is that this program never
installs with pip, because on Ubuntu 24.04 and 26.04 that means either a
virtual environment or --break-system-packages, and both are decisions
about how the machine is set up that a camera program should not make on
its own. A GPU TensorFlow install is a bigger version of exactly that
decision - several gigabytes of CUDA wheels - so it stays a suggestion.

The memory cap matters on a card that is also driving the desktop. Without
set_memory_growth TensorFlow reserves nearly all of the VRAM at startup,
and on an 8 GB card already holding GNOME and a browser that means a
training run and the desktop fighting over the same memory for the whole
20,000-iteration pretraining.

The convolution test matters because a GPU can register fine and still
have no kernels for its compute capability - older cards are dropped from
the TensorFlow wheels long before they stop working as display adapters.
That failure arrives as 'no kernel image is available for execution on the
device' at the first real layer, which from inside a training run looks
like an unrelated crash. Better to find out here, in two milliseconds, on
a 1x8x8x3 tensor nobody cares about.

Set ``WEBCAM_NO_GPU=1`` to keep everything on the CPU regardless.
"""

import os
import shutil
import subprocess
import sys

# None until configure() has run; then True if a usable GPU is in play.
_state = None


def _cuda_libs_present():
    """True if the CUDA runtime wheels TensorFlow needs are installed."""
    import importlib.util                                      # noqa: PLC0415
    try:
        return importlib.util.find_spec('nvidia.cudnn') is not None
    except (ImportError, ValueError):
        return False


# The oldest compute capability the TensorFlow wheels still ship kernels
# for. Maxwell is 5.0 to 5.3; a Quadro M4000 is 5.2 and falls below this.
#
# Measured on exactly that card, driver 580.196.07, TensorFlow 2.22: the
# card registers with 7 GB of memory, TensorFlow warns it will JIT from
# PTX, and then the first op - a Cast, not even a convolution - dies with
# CUDA_ERROR_NO_BINARY_FOR_GPU, because there is no PTX in the wheel to
# JIT from either. Installing cuDNN does not help and was never the
# missing piece: the op that fails is one of TensorFlow's own kernels,
# not one of cuDNN's primitives. Nor does the driver, which is the last
# branch supporting Maxwell at all - a downgrade loses the desktop and
# gains nothing.
#
# So a card below this is treated as no card. Not a half-working one.
MIN_COMPUTE_CAPABILITY = 6.0


def _compute_capabilities():
    """Every visible card's compute capability, as floats.

    Returns an empty list if nvidia-smi cannot answer - an old driver
    without --query-gpu=compute_cap, say. An empty list means 'do not
    know', which is deliberately not the same as 'too old': see
    usable_gpu_present(), which gives the card the benefit of the doubt
    there and lets the convolution probe in configure() settle it.
    """
    try:
        done = subprocess.run(
            ['nvidia-smi', '--query-gpu=compute_cap', '--format=csv,noheader'],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if done.returncode != 0:
        return []
    caps = []
    for line in done.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            caps.append(float(line))
        except ValueError:
            continue
    return caps


def gpu_present():
    """True if the NVIDIA driver can see a card, whatever its age.

    nvidia-smi rather than anything in TensorFlow, because this is also
    asked before TensorFlow is imported, and importing it to find out
    costs several seconds.
    """
    if os.environ.get('WEBCAM_NO_GPU'):
        return False
    if not shutil.which('nvidia-smi'):
        return False
    try:
        done = subprocess.run(
            ['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return done.returncode == 0 and bool(done.stdout.strip())


def too_old_for_tensorflow():
    """True if every visible card is below MIN_COMPUTE_CAPABILITY.

    False when there is no card at all, and False when the capability
    cannot be read - neither of those is 'too old'.
    """
    if not gpu_present():
        return False
    caps = _compute_capabilities()
    if not caps:
        return False
    return max(caps) < MIN_COMPUTE_CAPABILITY


def usable_gpu_present():
    """True if there is a card TensorFlow stands a chance of using.

    This, not gpu_present(), is what decides whether the GPU build of
    TensorFlow is worth installing.
    """
    return gpu_present() and not too_old_for_tensorflow()


def advise():
    """Say what to install for this machine's hardware. Installs nothing."""
    if not gpu_present():
        return

    if too_old_for_tensorflow():
        # Only worth saying if the GPU build is actually installed, since
        # that is the one that wastes several seconds and several
        # gigabytes registering a card it cannot use.
        if not _cuda_libs_present():
            return
        caps = _compute_capabilities()
        which = ', '.join('{0:.1f}'.format(c) for c in caps) or 'unknown'
        print()
        print('webcam: this machine has an NVIDIA GPU too old for '
              'TensorFlow (compute capability {0}; the wheels need {1} '
              'or newer).'.format(which, MIN_COMPUTE_CAPABILITY))
        print('The GPU build of TensorFlow will register the card and then '
              'fail on the first operation, so the brains use the CPU.')
        print('Nothing is broken. To drop several gigabytes of CUDA '
              'libraries that cannot be used here:')
        print()
        print('    python3 -m pip install --upgrade --force-reinstall '
              'tensorflow-cpu')
        print()
        print('Set WEBCAM_NO_GPU=1 to silence this.')
        print()
        return

    if _cuda_libs_present():
        return
    print()
    print('webcam: an NVIDIA GPU is present, but the CUDA libraries '
          'TensorFlow needs are not installed, so the brains will train '
          'and run on the CPU.')
    print('That works. It is only slower. To use the card instead:')
    print()
    print('    python3 -m pip install --upgrade "tensorflow[and-cuda]"')
    print()
    print('On Ubuntu 24.04 and 26.04 that needs a virtual environment or a '
          'conda environment - the system Python refuses pip installs. '
          'Nothing here will run it for you.')
    print('Set WEBCAM_NO_GPU=1 to silence this and stay on the CPU.')
    print()


def configure():
    """Make the GPU safe to use, or take it out of play. Returns True if used.

    Idempotent: the first call decides and every later one repeats the
    answer. That matters because both the enhancer and the trainer call it,
    and set_memory_growth raises if it is called after the GPU has been
    touched.
    """
    global _state
    if _state is not None:
        return _state

    if os.environ.get('WEBCAM_NO_GPU'):
        _state = False
        return _state

    # Refuse a card the wheels have no kernels for, before TensorFlow is
    # imported. Letting it get as far as the convolution probe below
    # works, but costs a CUDA init, a 7 GB allocation and two alarming
    # JIT warnings on every single start.
    if too_old_for_tensorflow():
        try:
            os.environ.setdefault('CUDA_VISIBLE_DEVICES', '')
        except Exception:                                      # noqa: BLE001
            pass
        _state = False
        return _state

    try:
        import tensorflow as tf                                # noqa: PLC0415
    except Exception:                                          # noqa: BLE001
        _state = False
        return _state

    try:
        cards = tf.config.list_physical_devices('GPU')
    except Exception:                                          # noqa: BLE001
        cards = []

    if not cards:
        _state = False
        return _state

    for card in cards:
        try:
            tf.config.experimental.set_memory_growth(card, True)
        except (RuntimeError, ValueError) as problem:
            # Already initialised, or the card does not support growth.
            # Not fatal - it only means TensorFlow keeps the whole card.
            print('webcam: could not cap GPU memory ({0}) - TensorFlow may '
                  'take all of it.'.format(problem), file=sys.stderr)

    # The real test. A convolution, because that is what the brains are
    # made of and what a missing kernel image fails on.
    try:
        with tf.device('/GPU:0'):
            probe = tf.zeros((1, 8, 8, 3))
            kernel = tf.zeros((3, 3, 3, 4))
            tf.nn.conv2d(probe, kernel, strides=1, padding='SAME').numpy()
    except Exception as problem:                               # noqa: BLE001
        print(file=sys.stderr)
        print('webcam: the GPU is visible but cannot run a convolution, so '
              'the brains will use the CPU instead.', file=sys.stderr)
        print('  {0}'.format(str(problem).strip().splitlines()[0]),
              file=sys.stderr)
        print('This is usually a card whose compute capability the installed '
              'TensorFlow was not built for. Nothing is broken - training '
              'and correction carry on, slower.', file=sys.stderr)
        print(file=sys.stderr)
        try:
            tf.config.set_visible_devices([], 'GPU')
        except Exception:                                      # noqa: BLE001
            pass
        try:
            import webcam_errors                               # noqa: PLC0415
            webcam_errors.report('GPU.probe', problem, detail=(
                'GPU registered but no usable kernels - fell back to CPU'))
        except Exception:                                      # noqa: BLE001
            pass
        _state = False
        return _state

    names = ', '.join(card.name.split('/')[-1] for card in cards)
    print('webcam: using the GPU for the brains ({0}).'.format(names))
    _state = True
    return _state


def in_use():
    """What configure() decided, without deciding it."""
    return bool(_state)
