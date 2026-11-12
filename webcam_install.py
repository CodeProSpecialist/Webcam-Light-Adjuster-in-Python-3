#!/usr/bin/env python3
"""Install everything the webcam programs need, out loud, one step at a time.

The older check (webcam_bootstrap.py) was deliberately quiet: it printed
nothing when everything was installed and named an apt package when something
was not. That is the wrong shape for the parts added since - OpenCV,
TensorFlow and the ONNX detector - because those three are the ones that
break, they break in ways that look like each other, and a quiet installer
leaves no record of which of them was actually installed and from where.

So this one says everything. Every dependency is named, in order, with what
was found and where it was found; every install prints the command first and
then streams its output as it runs, indented, so a twenty-minute TensorFlow
download is visibly doing something rather than apparently hung.

    python3 webcam_install.py              # check and install what is missing
    python3 webcam_install.py --check      # say what is missing, install nothing
    python3 webcam_install.py --force      # reinstall even what is present

Where each thing comes from, and why it is not all one package manager:

  conda   when this python is a conda python. OpenCV from conda-forge pulls
          its own ffmpeg, Qt and numpy, which is what makes it work on a
          release the distro's build is not ready for.
  pip     for what has no apt package - TensorFlow, and OpenCV when there is
          no conda. Outside a conda or virtual environment the system python
          refuses pip, so --break-system-packages is passed there and only
          there, and it is printed when it happens.
  apt     for the things that are not python at all: v4l2-ctl, ffmpeg, and
          the tk that python3-tk provides.

Nothing is installed without the plan being printed first. On a terminal it
asks once, for the whole list. With no terminal - cron, systemd - it installs
only when WEBCAM_AUTO_INSTALL=1 is set, and otherwise prints the plan and
carries on, because a scheduler that refuses to start at three in the morning
because TensorFlow is missing is worse than one that runs without the brain.
"""

import importlib
import os
import shutil
import subprocess
import sys

# Every dependency, in the order they are checked and installed - cheap and
# fundamental first, so that a failure happens before the twenty-minute one
# rather than after it.
#
#   module  what to import to test it
#   apt     the apt package, when there is one
#   pip     the pip name, when there is one
#   conda   (channel, package) for conda, when it is the better source
#   why     what stops working without it, printed when it is missing
DEPENDENCIES = [
    {'module': 'numpy', 'apt': 'python3-numpy', 'pip': 'numpy',
     'conda': ('conda-forge', 'numpy'),
     'why': 'every frame is a numpy array - nothing works without it'},
    {'module': 'tkinter', 'apt': 'python3-tk',
     'why': 'the control window itself'},
    {'module': 'astral', 'apt': 'python3-astral', 'pip': 'astral',
     'conda': ('conda-forge', 'astral'),
     'why': 'sunrise and sunset times for the schedule'},
    {'module': 'cv2', 'pip': 'opencv-contrib-python',
     'conda': ('conda-forge', 'opencv'),
     'why': 'the stacking, the CLAHE, the denoise and the car/person detector'},
    {'module': 'tensorflow', 'pip': 'tensorflow',
     'why': 'brain G, the learnt night correction'},
]

# Not python. Missing ones are named but never fatal: the schedule still runs,
# the automatic light adjustment just stays idle.
PROGRAMS = [
    {'program': 'v4l2-ctl', 'apt': 'v4l-utils',
     'why': 'reading and setting the camera controls'},
    {'program': 'ffmpeg', 'apt': 'ffmpeg',
     'why': 'grabbing the snapshots the correction works from'},
    {'program': 'convert', 'apt': 'imagemagick',
     'why': 'the fallback correction when OpenCV is not there'},
]

WIDTH = 72


def say(text=''):
    print(text, flush=True)


def step(index, total, text):
    say(f'[{index}/{total}] {text}')


def _in_conda():
    """A conda python, as opposed to the distro's. The marker directory is
    the reliable test - CONDA_PREFIX is only set once an env is activated,
    and this may be running from a cron line that never sourced the profile."""
    return os.path.isdir(os.path.join(sys.prefix, 'conda-meta'))


def _in_venv():
    return sys.prefix != getattr(sys, 'base_prefix', sys.prefix)


def _conda_exe():
    for name in ('mamba', 'conda'):
        found = shutil.which(name)
        if found:
            return found
    # An unactivated install still has the binary beside the python.
    guess = os.path.join(sys.prefix, 'bin', 'conda')
    return guess if os.path.exists(guess) else ''


def _have_module(name):
    """Import it for real rather than looking for its file.

    find_spec would be cheaper and is what the older check used, but it
    answers the wrong question for these: a cv2 or a tensorflow that is
    present and throws on import is exactly the state this is meant to find,
    and find_spec calls that installed.
    """
    try:
        module = importlib.import_module(name)
    except Exception as problem:                              # noqa: BLE001
        return False, f'{type(problem).__name__}: {problem}'
    version = getattr(module, '__version__', '') or ''
    where = getattr(module, '__file__', '') or 'built in'
    return True, f'{version} from {where}'.strip()


def plan_for(dep):
    """The command that would install this one, and the words for where from.

    conda when this is a conda python and the dependency names a channel;
    pip otherwise; apt for what pip has no answer for. The order matters: in
    a conda python, installing OpenCV with pip on top of the conda one is the
    mixed install that breaks, and this is where that is prevented.
    """
    if _in_conda() and dep.get('conda'):
        exe = _conda_exe()
        if exe:
            channel, package = dep['conda']
            return [exe, 'install', '-y', '-c', channel, package], 'conda'
    if dep.get('pip'):
        command = [sys.executable, '-m', 'pip', 'install', '--upgrade',
                   dep['pip']]
        # The system python refuses to be written to. Inside conda or a venv
        # it does not, and the flag would be wrong there.
        if not _in_conda() and not _in_venv():
            command.append('--break-system-packages')
        return command, 'pip'
    if dep.get('apt'):
        return _apt_command([dep['apt']]), 'apt'
    return [], ''


def _apt_command(packages):
    sudo = []
    if os.geteuid() != 0 and shutil.which('sudo'):
        sudo = ['sudo']
    return sudo + ['apt-get', '-y', 'install'] + sorted(set(packages))


def _interactive():
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (OSError, ValueError):
        return False


def stream(command, indent='    '):
    """Run it and print its output as it arrives.

    Line by line rather than captured and printed at the end, which is the
    whole point: a pip resolving TensorFlow's dependencies is quiet for a
    long time and a captured install looks identical to a hung one.
    """
    say(f'{indent}$ {" ".join(command)}')
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True,
                                   bufsize=1)
    except OSError as problem:
        say(f'{indent}could not run it: {problem}')
        return False
    try:
        for line in process.stdout:
            line = line.rstrip()
            if line:
                say(f'{indent}| {line}')
    except Exception as problem:                              # noqa: BLE001
        say(f'{indent}| (stopped reading the output: {problem})')
    code = process.wait()
    say(f'{indent}finished with exit code {code}')
    return code == 0


def check(verbose=True):
    """What is here and what is not. Installs nothing."""
    missing = []
    total = len(DEPENDENCIES) + len(PROGRAMS)
    index = 0
    if verbose:
        say('Checking what the webcam programs need.')
        say(f'  python   {sys.version.split()[0]} at {sys.executable}')
        kind = 'conda' if _in_conda() else ('virtualenv' if _in_venv()
                                            else 'system python')
        say(f'  this is  {kind}')
        say('')

    for dep in DEPENDENCIES:
        index += 1
        have, detail = _have_module(dep['module'])
        if have:
            if verbose:
                step(index, total, f"{dep['module']:<12} present   {detail}")
        else:
            missing.append(dep)
            if verbose:
                step(index, total, f"{dep['module']:<12} MISSING   {detail}")
                say(f"          needed for: {dep['why']}")

    for item in PROGRAMS:
        index += 1
        found = shutil.which(item['program'])
        if found:
            if verbose:
                step(index, total,
                     f"{item['program']:<12} present   {found}")
        else:
            missing.append(item)
            if verbose:
                step(index, total, f"{item['program']:<12} MISSING")
                say(f"          needed for: {item['why']}")
    if verbose:
        say('')
    return missing


def install(missing, verbose=True, assume_yes=False):
    """Install what check() found, printing the plan and then doing it."""
    if not missing:
        if verbose:
            say('Everything is installed. Nothing to do.')
        return True, []

    plans = []
    apt_only = []
    for dep in missing:
        if dep.get('program'):
            apt_only.append(dep['apt'])
            continue
        command, source = plan_for(dep)
        if command:
            plans.append((dep.get('module') or dep['apt'], command, source))
        else:
            plans.append((dep.get('module'), [], ''))
    if apt_only:
        plans.append(('camera tools', _apt_command(apt_only), 'apt'))

    say(f'{len(plans)} thing(s) to install:')
    for name, command, source in plans:
        if command:
            say(f'  {name:<14} via {source:<5} {" ".join(command)}')
        else:
            say(f'  {name:<14} no automatic install - see the README')
    say('')

    if not assume_yes:
        if _interactive():
            try:
                answer = input('Install these now? [y/N] ').strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ''
            if answer not in ('y', 'yes'):
                say('Nothing installed. The programs will run without them '
                    'where they can.')
                return False, [name for name, _c, _s in plans]
        elif os.environ.get('WEBCAM_AUTO_INSTALL') not in ('1', 'yes', 'true'):
            say('No terminal to ask on, so nothing is being installed.')
            say('Set WEBCAM_AUTO_INSTALL=1 to let it install unattended.')
            return False, [name for name, _c, _s in plans]
        else:
            say('No terminal, WEBCAM_AUTO_INSTALL is set - installing.')
    say('')

    failed = []
    for number, (name, command, source) in enumerate(plans, 1):
        if not command:
            failed.append(name)
            continue
        step(number, len(plans), f'installing {name} with {source}')
        ok = stream(command)
        if ok and name in ('numpy', 'cv2', 'tensorflow', 'astral'):
            # Confirm by importing in a fresh interpreter: the one running
            # this already failed to import it and may have cached that.
            probe = subprocess.run(
                [sys.executable, '-c', f'import {name}'],
                capture_output=True, text=True)
            ok = probe.returncode == 0
            say(f'    import {name}: '
                f'{"works" if ok else "still failing - " + probe.stderr.strip().splitlines()[-1] if probe.stderr.strip() else "still failing"}')
        if not ok:
            failed.append(name)
        say('')

    if failed:
        say(f'Could not install: {", ".join(str(f) for f in failed)}')
        say('The programs still start; what needed these stays idle and the '
            'reason is written to the error log.')
    else:
        say('All installed.')
    return not failed, failed


def ensure_all(verbose=True, assume_yes=False, force=False):
    """The one call an entry script makes at startup.

    Returns True when nothing is missing. Never raises and never exits: a
    camera program that will not start because an optional dependency is
    absent is worse than one that starts degraded and says so.
    """
    if os.environ.get('WEBCAM_NO_BOOTSTRAP'):
        return True
    try:
        missing = DEPENDENCIES + PROGRAMS if force else check(verbose=verbose)
        if not missing:
            if verbose:
                say('Everything the webcam programs need is installed.\n')
            return True
        ok, _failed = install(missing, verbose=verbose, assume_yes=assume_yes)
        return ok
    except Exception as problem:                              # noqa: BLE001
        say(f'the dependency check itself failed: '
            f'{type(problem).__name__}: {problem}')
        try:
            import webcam_errors
            webcam_errors.report('install', problem,
                                 detail='the startup dependency check')
        except Exception:                                     # noqa: BLE001
            pass
        return False


def main():
    args = sys.argv[1:]
    if '--check' in args:
        missing = check(verbose=True)
        if missing:
            names = [d.get('module') or d.get('program') for d in missing]
            say(f'missing: {", ".join(str(n) for n in names)}')
            sys.exit(1)
        say('nothing missing')
        return
    ok = ensure_all(verbose=True,
                    assume_yes='--yes' in args or '-y' in args,
                    force='--force' in args)
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
