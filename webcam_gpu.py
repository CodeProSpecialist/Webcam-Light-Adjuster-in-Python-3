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


def gpu_present():
    """True if the NVIDIA driver can see a card.

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


def advise():
    """Say how to turn a visible GPU into a usable one. Installs nothing."""
    if not gpu_present() or _cuda_libs_present():
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
