"""First-run dependency check for the webcam programs.

Every entry script calls ``ensure()`` before importing the modules it needs.
If they are all installed this returns immediately and nothing is printed,
which is every run after the first. If any is missing it offers to install
the apt packages that provide them.

Why this exists: the failure without it is ``ModuleNotFoundError: No module
named 'astral'``, which is accurate and tells you nothing about which apt
package to install. This names the package.

Three rules it follows, which are the whole reason it is safe to have a
camera program touching apt at all:

1. It never installs without being told to. On a terminal it asks, and
   prints the exact command first. Answer anything but yes and it exits
   having changed nothing.
2. With no terminal - under cron, systemd, a pipe - it does not ask and does
   not install. It prints the command and exits non-zero. A prompt there
   would hang forever waiting for an answer nobody can give, and the
   scheduler would silently never start.
3. It only ever installs from apt, and only the packages in the table below.
   Not pip: Ubuntu 24.04 and 26.04 refuse ``pip3 install`` into the system
   Python with ``externally-managed-environment``, and the ways around that
   - a virtual environment, or ``--break-system-packages`` - are both
   decisions about how this machine is set up that a camera program has no
   business making on its own. The README covers them for anyone who wants
   that route.

sudo asks for the password itself, on the terminal, in its own way. Nothing
here reads, stores or passes a password. sudo is only ever invoked when
something is actually missing, so a run with everything installed never
prompts for anything.

Set ``WEBCAM_NO_BOOTSTRAP=1`` to skip all of this - for a machine where the
modules come from a virtual environment or anywhere else that is not apt,
which is a legitimate setup and not something this should argue with.
"""

import importlib.util
import os
import shutil
import subprocess
import sys

# Importable name -> the apt package that provides it.
APT_FOR = {
    'astral': 'python3-astral',
    'tkinter': 'python3-tk',
}

# Command-line programs the light meter and snapshots need. Missing ones are
# mentioned once and nothing more: the schedule still runs without them, only
# the automatic light adjustment stays idle, and the programs already say so
# at startup themselves.
APT_FOR_PROGRAM = {
    'v4l2-ctl': 'v4l-utils',
    'ffmpeg': 'ffmpeg',
}


def _missing(module):
    """True if `module` cannot be imported.

    find_spec is used rather than a try/import because importing is the
    expensive half and all that is wanted here is whether it is there.
    It raises rather than returning None when a parent package is itself
    absent, hence the except.
    """
    try:
        return importlib.util.find_spec(module) is None
    except (ImportError, ValueError):
        return True


def _interactive():
    # Both ends matter: stdin to answer the question, stdout to read it.
    return sys.stdin.isatty() and sys.stdout.isatty()


def _apt_command(packages):
    sudo = [] if os.geteuid() == 0 else ['sudo']
    if sudo and not shutil.which('sudo'):
        sudo = []
    return sudo + ['apt-get', '-y', 'install'] + sorted(set(packages))


def _advise_gpu():
    """Mention the GPU TensorFlow install, once, on a terminal only.

    Deliberately never installs: see the module docstring's third rule and
    the note at the top of webcam_gpu. Headless runs stay silent - a cron
    job has nobody to read advice.
    """
    if not _interactive():
        return
    try:
        import webcam_gpu                                      # noqa: PLC0415
    except ImportError:
        return
    try:
        webcam_gpu.advise()
    except Exception:                                          # noqa: BLE001
        pass


def ensure(*modules):
    if os.environ.get('WEBCAM_NO_BOOTSTRAP'):
        return

    # A name in the program table is a program, not an importable module.
    # webcam_record and webcam_archive both call ensure('ffmpeg'), meaning
    # the encoder - and taking that as a module name is how this used to
    # refuse to start on a machine that had ffmpeg itself but no PyPI
    # package of the same name. There is no such Python module worth having:
    # the one on PyPI called ffmpeg is an unrelated wrapper.
    modules = [m for m in modules if m not in APT_FOR_PROGRAM]

    missing = [m for m in modules if _missing(m)]

    missing_programs = sorted(
        p for p in APT_FOR_PROGRAM if not shutil.which(p)
    )

    if not missing and not missing_programs:
        _advise_gpu()
        return

    # A missing program is installed here too, not merely reported. ffmpeg is
    # the one that matters: without it there is no recording at all, so a
    # first run on a clean machine should fix that itself rather than hand
    # back an apt command to paste. On a terminal it offers to install,
    # exactly as it does for a Python module. Headless, with every module
    # present, it keeps the old behaviour and warns rather than exiting: a
    # cron job that still does something useful beats one that dies.
    if not missing and not _interactive():
        it = 'it' if len(missing_programs) == 1 else 'them'
        command = _apt_command(
            APT_FOR_PROGRAM[p] for p in missing_programs
        )
        why = ' and '.join(
            reason for program, reason in (
                ('ffmpeg', 'there is no recording without ffmpeg'),
                ('v4l2-ctl', 'the automatic light adjustment needs v4l2-ctl'),
            ) if program in missing_programs
        )
        print(
            'webcam: not installed: {names} - {why}. To add '
            '{it}: {command}'.format(
                names=' '.join(missing_programs),
                why=why,
                it=it,
                command=' '.join(command),
            ),
            file=sys.stderr,
        )
        return

    packages = [APT_FOR[m] for m in missing if m in APT_FOR]
    packages += [APT_FOR_PROGRAM[p] for p in missing_programs]
    unknown = [m for m in missing if m not in APT_FOR]

    print()
    if missing:
        print('webcam: these Python modules are needed and not installed:')
        for module in missing:
            print('  {0}'.format(module))
        print()
    if missing_programs:
        print('webcam: these programs are needed and not installed:')
        for program in missing_programs:
            print('  {0}'.format(program))
        print()

    if unknown:
        print('No apt package is known here for: {0}'.format(
            ' '.join(unknown)))
        print('Install those yourself, then run this again.')
        print()
        if not packages:
            sys.exit(1)

    command = _apt_command(packages)
    shown = ' '.join(command)

    if not _interactive():
        # cron, systemd, a pipe. Asking would hang.
        print('Not running on a terminal, so nothing will be installed here.')
        print('Run this once, by hand:')
        print()
        print('    {0}'.format(shown))
        print()
        print('Then start this again.')
        print()
        sys.exit(1)

    print('This can install them now with:')
    print()
    print('    {0}'.format(shown))
    print()
    if command[0] == 'sudo':
        print('sudo will ask for your password.')
    try:
        answer = input('Install them? [y/N] ').strip()
    except EOFError:
        answer = ''

    if answer.lower() not in ('y', 'yes'):
        print()
        print('Nothing installed. Run the command above when you are ready.')
        print()
        sys.exit(1)

    print()
    try:
        status = subprocess.call(command)
    except OSError as problem:
        print()
        print('Could not run apt-get: {0}'.format(problem))
        sys.exit(1)

    if status != 0:
        print()
        print('That did not finish - apt exited {0}.'.format(status))
        print('Run it by hand to see what it says:')
        print()
        print('    {0}'.format(shown))
        print()
        sys.exit(1)

    # apt succeeding is not the same as the modules now importing: a package
    # can install and still not provide what was asked for, if the table
    # above is wrong about which package holds which module. Check rather
    # than assume, because the alternative is the original ModuleNotFoundError
    # two lines later, now with an apt run in front of it confusing the
    # picture.
    importlib.invalidate_caches()
    still_missing = [m for m in missing if _missing(m)]
    still_missing += [p for p in missing_programs
                      if not shutil.which(p)]

    if still_missing:
        print()
        print('apt finished, but these still are not there:')
        for module in still_missing:
            print('  {0}'.format(module))
        print()
        print('That means the package named above does not provide them on '
              'this release.')
        print()
        sys.exit(1)

    print()
    print('Installed. Carrying on.')
    print()
