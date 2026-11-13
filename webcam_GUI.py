#!/usr/bin/env python3
"""A control window for whichever webcam is plugged in.

This is the window version of what the scheduler does from the command line:
the same presets, through the same engine (webcam_presets.py), and the same
automatic light adjustment (webcam_light_meter.py). The scheduler can keep
running while this is open; both only set controls, and they re-read each
other's work rather than fighting over it.

It builds itself from the camera. Every control on the window comes from
`v4l2-ctl -L` at startup, with the range the camera reported - which is the
only way one window can serve both the older webcam, whose controls run 0 to
255, and the current ones, where brightness runs -64 to 64. Nothing here has a
control name or a range written into it that the camera did not confirm.

It never opens the video stream. A V4L2 camera lets one program stream at a
time and the recording software has it, so there is no preview here: judge the
picture in whatever is recording it. Controls are a different thing from the
stream - they are ioctls on the device node and can be set while another
program is streaming, which is why any of this works at all.

    sudo apt-get -y install python3-tk v4l-utils imagemagick ffmpeg
    python3 webcam_GUI.py
"""

import os
import queue
import re
import subprocess
import sys
import threading
import time

# First run: check the modules below are installed, and offer to install the
# apt packages that provide them if they are not. Nothing is printed when
# they are all there, and sudo is only invoked when something is actually
# missing, so an ordinary run never prompts for a password.
#
# webcam_bootstrap sits beside this file, so the plain import finds it. If it
# has been deleted the import fails and the tkinter import below fails as it
# always did. Set WEBCAM_NO_BOOTSTRAP=1 to skip the check.
try:
    import webcam_bootstrap
except ImportError:
    pass
else:
    webcam_bootstrap.ensure('tkinter')

# Then the whole list, out loud: numpy, tkinter, astral, OpenCV, TensorFlow,
# v4l2-ctl, ffmpeg and ImageMagick, each one named with what was found and
# where, and any install streamed as it runs. This is deliberately not quiet
# the way the check above is - OpenCV and TensorFlow are the two that break,
# they break in ways that look alike, and the record of which was installed
# from where is the thing worth having when they do. WEBCAM_QUIET_INSTALL=1
# turns it down to failures only; WEBCAM_NO_BOOTSTRAP=1 skips it entirely.
try:
    import webcam_install
except ImportError:
    pass
else:
    webcam_install.ensure_all(
        verbose=not os.environ.get('WEBCAM_QUIET_INSTALL'))

import tkinter as tk
from tkinter import ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from webcam_light_meter import LightMeter, METER_SETTLE, LOG_FILE
except ImportError as problem:                                    # noqa: BLE001
    print(f'automatic light adjustment unavailable: {problem}', file=sys.stderr)
    LightMeter = None
    METER_SETTLE = 1
    LOG_FILE = ''

# The two enhancement switches live in a file rather than in this window, so
# that the scheduler - which may be the only thing running at three in the
# morning - reads the same answer the window shows.
try:
    import webcam_enhance
except ImportError as problem:                                    # noqa: BLE001
    print(f'night image enhancement unavailable: {problem}', file=sys.stderr)
    webcam_enhance = None

# The brain's own module: the harvest, the retraining, and the delete-and-start
# -again that the button in the brain tile calls.
try:
    import train_webcam_brain
except ImportError:                                               # noqa: BLE001
    train_webcam_brain = None

# Off and on for the light on the front of the camera, for the cameras that
# let software near it.
try:
    import webcam_led
except ImportError:                                               # noqa: BLE001
    webcam_led = None

# Reads the controls back and measures the picture, so the window can
# answer whether anything it sends reaches the stream the recorder tapes.
try:
    import webcam_verify
except ImportError:                                               # noqa: BLE001
    webcam_verify = None

# Every OpenCV, TensorFlow, detector and correction failure is written here by
# whichever part of the pipeline hit it - including by the scheduler and by
# cron runs that have no terminal of their own - and this window tails it into
# the terminal pane at the bottom so the failures are visible rather than only
# showing up as a picture that quietly did not improve.
try:
    import webcam_errors
except ImportError:                                               # noqa: BLE001
    webcam_errors = None

# The library checks and the four comparison frames for the tile at the
# bottom of the page.
try:
    import webcam_status
except ImportError:                                               # noqa: BLE001
    webcam_status = None

DEVICE = os.environ.get('WEBCAM_DEVICE', '/dev/video0')

_CONTROL_RE = re.compile(r'^\s*(\w+)\s+0x[0-9a-f]+\s+\((int|bool|menu|intmenu)\)', re.I)

# The order the controls appear in, when the camera has them. Anything the
# camera reports that is not in this list follows afterwards, so a camera with
# something unusual on it still gets a control rather than having it quietly
# dropped.
PREFERRED = [
    'brightness', 'contrast', 'saturation', 'hue', 'gamma', 'gain', 'sharpness',
    'backlight_compensation', 'white_balance_automatic', 'white_balance_temperature',
    'power_line_frequency', 'auto_exposure', 'exposure_time_absolute', 'exposure_absolute',
    'exposure_dynamic_framerate', 'exposure_auto_priority',
    'focus_automatic_continuous', 'focus_auto', 'focus_absolute',
    'pan_absolute', 'tilt_absolute', 'zoom_absolute', 'privacy',
]

# The terminal at the bottom of the window: 80 columns by 15 lines, which is
# an ordinary terminal's width and about a screenful of the scheduler's log.
# How often the comparison frames are checked for a newer set, and how often
# they are reloaded regardless. The check is six stat calls, so it can be
# frequent; the reload reads and scales six JPEGs, so it should not be.
COMPARE_POLL_MS = 3000
COMPARE_FORCE_MS = 30000

TERMINAL_COLS = 80
TERMINAL_ROWS = 15

# What runs in it. The scheduler prints the sunrise and sunset times it worked
# out and the preset it applies at each of them, so having it here means the
# window shows what the schedule is doing rather than only what this window
# has done itself.
TERMINAL_SCRIPT = 'python3-sunrise-sunset.py'

PRESETS = [
    ('Night', 'night', '#222233', 'white'),
    ('Night Mono', 'night-mono', '#000011', 'white'),
    ('Golden AM', 'golden-am', '#ee99aa', 'black'),
    ('Day', 'day', '#ccddee', 'black'),
    ('Bright Sun', 'bright-sun', '#ffff99', 'black'),
    ('Golden PM', 'golden-pm', '#ee8855', 'black'),
]


def run(args):
    """v4l2-ctl and friends, with their noise swallowed. Nothing here is worth
    taking the window down for."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


def home_dir():
    """Where this user's home directory is - no user name written into it."""
    return os.path.expanduser('~')


def find_beside(filename):
    """Next to this file, then the home directory, then the working directory.

    The same order run_preset uses, and for the same reason: these files get
    copied to ~ and run from either place.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for folder in (here, home_dir(), os.getcwd()):
        path = os.path.join(folder, filename)
        if os.path.isfile(path):
            return path
    return None


class Camera:
    """What this particular camera can actually do, read once at startup.

    Sending a value past the end of a control's range does not get clamped by
    the driver, it gets refused - and with the errors swallowed it was refused
    silently. So everything is clamped to what the camera reported, and
    controls it does not have are skipped.
    """

    def __init__(self, device):
        self.device = device
        self.controls = {}
        self.warned = set()
        self.read_controls()

    def read_controls(self):
        self.controls = {}
        for line in run(['v4l2-ctl', '-d', self.device, '-L']).splitlines():
            found = _CONTROL_RE.match(line)
            if not found:
                continue
            name, kind = found.group(1), found.group(2).lower()
            low = re.search(r'min=(-?\d+)', line)
            high = re.search(r'max=(-?\d+)', line)
            default = re.search(r'default=(-?\d+)', line)
            # Booleans report no min or max of their own.
            low = int(low.group(1)) if low else 0
            high = int(high.group(1)) if high else 1
            self.controls[name] = {
                'min': low,
                'max': high,
                'default': int(default.group(1)) if default else low,
                'type': kind,
            }
        return self.controls

    def get(self, name):
        out = run(['v4l2-ctl', '-d', self.device, '--get-ctrl', name])
        found = re.search(r':\s*(-?\d+)', out)
        return int(found.group(1)) if found else None

    def set(self, name, value):
        info = self.controls.get(name)
        if self.controls and info is None:
            if name not in self.warned:
                self.warned.add(name)
                print(f'{self.device} has no {name} control - skipping it', file=sys.stderr)
            return
        if info:
            value = max(info['min'], min(info['max'], int(value)))
        run(['v4l2-ctl', '-d', self.device, '--set-ctrl', f'{name}={value}'])


def run_preset(name, device):
    """Run a preset through the engine, wherever it actually lives.

    Next to this file first, then the home directory, then the working
    directory, which is the same order the wrapper scripts use - these files
    get copied to ~ and run both ways. In a child process, with the same
    interpreter this window is running under.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    for folder in (here, home_dir(), os.getcwd()):
        engine = os.path.join(folder, 'webcam_presets.py')
        if os.path.isfile(engine):
            result = subprocess.run([sys.executable, engine, name, device])
            return result.returncode == 0
    print('webcam_presets.py not found next to this file, in $HOME, or in the '
          'working directory', file=sys.stderr)
    return False


class TerminalPane:
    """A terminal at the bottom of the window with a program running in it.

    It is read-only: the program's output comes in, nothing is typed back.
    That is deliberate - the scheduler takes no input, and a Text widget that
    cannot be typed into cannot have a stray keystroke scroll it away from the
    newest line.

    A pipe is read with a blocking read, and a blocking read on the thread
    running the Tk event loop freezes the window. So a reader thread does the
    blocking and hands finished lines to a queue, and the event loop drains
    the queue on a timer. Only the reader thread writes to the queue and only
    the event loop touches the widget, which is the one arrangement Tk is
    safe under.
    """

    POLL = 200        # ms between drains of the queue
    MAX_LINES = 2000  # scrollback kept before the oldest lines are dropped
    ERROR_POLL = 1000  # ms between checks of the error log

    # What the failures look like in the pane. Red for the vision and
    # learning libraries, because those are the ones that change the picture;
    # amber for the correction stages, which degrade rather than stop.
    ERROR_COLOURS = {
        'OpenCV': '#ff6b6b',
        'TensorFlow': '#ff9b4a',
        'brain G': '#ffd166',
        'detector': '#9bdcff',
        'night correction': '#ffd166',
        '': '#ff8c8c',
    }

    def __init__(self, parent, root, filename, args=(), side='bottom'):
        self.root = root
        self.filename = filename
        self.script = find_beside(filename)
        self.args = [str(a) for a in args]
        self.lines = queue.Queue()
        self.child = None
        self.reader = None
        self.stopping = False

        frame = tk.LabelFrame(parent, text=f'  {filename}  ',
                              font=('Helvetica', 9, 'bold'))
        frame.pack(side=side, fill='x', expand=False, padx=4, pady=(2, 4))
        self.frame = frame

        bar = tk.Frame(frame)
        bar.pack(fill='x')
        self.status = tk.Label(bar, text='starting', anchor='w',
                               font=('Helvetica', 8), fg='#003366')
        self.status.pack(side='left', padx=4)
        tk.Button(bar, text='Clear', font=('Helvetica', 8),
                  command=self.clear).pack(side='right', padx=2)
        tk.Button(bar, text='Restart', font=('Helvetica', 8),
                  command=self.restart).pack(side='right', padx=2)
        tk.Button(bar, text='Stop', font=('Helvetica', 8),
                  command=self.stop).pack(side='right', padx=2)

        body = tk.Frame(frame)
        body.pack(fill='both', expand=True)
        self.text = tk.Text(
            body, width=TERMINAL_COLS, height=TERMINAL_ROWS, wrap='none',
            font=('Monospace', 9), bg='#101010', fg='#d0d0d0',
            insertbackground='#d0d0d0', highlightthickness=0, state='disabled',
        )
        tvbar = tk.Scrollbar(body, orient='vertical', command=self.text.yview, width=16)
        thbar = tk.Scrollbar(frame, orient='horizontal', command=self.text.xview)
        self.text.configure(yscrollcommand=tvbar.set, xscrollcommand=thbar.set)
        self.text.pack(side='left', fill='both', expand=True)
        tvbar.pack(side='right', fill='y')
        thbar.pack(side='bottom', fill='x')

        # The wheel bindings in build_scroll_area are bind_all, so without
        # these the wheel over the terminal would scroll the controls behind
        # it instead of the terminal.
        for sequence, amount in (('<Button-4>', -3), ('<Button-5>', 3)):
            self.text.bind(sequence, lambda e, n=amount: (self.text.yview_scroll(n, 'units'),
                                                          'break')[1])
        self.text.bind('<MouseWheel>',
                       lambda e: (self.text.yview_scroll(-3 if e.delta > 0 else 3, 'units'),
                                  'break')[1])

        # Colour tags for the tailed failures, one per stage.
        for stage, colour in self.ERROR_COLOURS.items():
            self.text.tag_configure(f'err-{stage}', foreground=colour)
        self.text.tag_configure('err-trace', foreground='#9a9a9a')

        # Start reading the error log from its current end, so opening the
        # window does not replay every failure since the machine was built.
        # The tile below offers a button to dump the recent history on demand.
        self.error_offset = webcam_errors.size() if webcam_errors else 0

        self.start()
        self.root.after(self.POLL, self.drain)
        self.root.after(self.ERROR_POLL, self.drain_errors)

    # ---- the child process ----

    def start(self):
        if self.child and self.child.poll() is None:
            return
        self.script = self.script or find_beside(self.filename)
        if not self.script:
            self.write(f'{self.filename} was not found next to this file, in '
                       f'{home_dir()}, or in the working directory.\n')
            self.status.configure(text='not found', fg='#aa0000')
            return
        try:
            # -u because a pipe makes Python buffer its output in blocks, and
            # a block of log an hour from now is no use in a window that is
            # meant to show what is happening.
            self.child = subprocess.Popen(
                [sys.executable, '-u', self.script] + self.args,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, cwd=os.path.dirname(self.script) or None,
            )
        except OSError as problem:
            self.write(f'could not start {self.script}: {problem}\n')
            self.status.configure(text='failed to start', fg='#aa0000')
            return
        self.stopping = False
        self.write(f'--- {os.path.basename(self.script)} started, '
                   f'pid {self.child.pid} ---\n')
        self.status.configure(text=f'running, pid {self.child.pid}', fg='#006600')
        self.reader = threading.Thread(target=self.read_pipe, args=(self.child,),
                                       daemon=True)
        self.reader.start()

    def read_pipe(self, child):
        """The reader thread. It touches the queue and nothing else."""
        try:
            for line in child.stdout:
                self.lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            try:
                child.stdout.close()
            except (OSError, ValueError):
                pass
            self.lines.put(f'--- exited, status {child.wait()} ---\n')

    def stop(self):
        if not self.child or self.child.poll() is not None:
            self.status.configure(text='not running', fg='#555555')
            return
        self.stopping = True
        # Ask first; a scheduler that is mid-write to the camera gets to
        # finish. Kill only if it ignores the asking.
        self.child.terminate()
        try:
            self.child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.child.kill()
        self.status.configure(text='stopped', fg='#555555')

    def restart(self):
        self.stop()
        self.child = None
        self.start()

    def clear(self):
        self.text.configure(state='normal')
        self.text.delete('1.0', 'end')
        self.text.configure(state='disabled')

    # ---- the event loop's half ----

    def drain(self):
        pulled = 0
        while pulled < 400:
            try:
                self.write(self.lines.get_nowait())
            except queue.Empty:
                break
            pulled += 1
        if self.child and self.child.poll() is not None and not self.stopping:
            self.status.configure(text=f'exited, status {self.child.returncode}',
                                  fg='#aa0000')
        self.root.after(self.POLL, self.drain)

    def drain_errors(self):
        """Tail the error log into this pane.

        The pipeline keeps working when OpenCV, TensorFlow, the detector or
        one of the correction stages fails - it falls back - and that used to
        happen silently. Everything it falls back from is written to the log
        with its full traceback, and this is where it becomes visible.

        Read by byte offset rather than re-read whole, so a long log costs
        nothing per poll.
        """
        if webcam_errors is None:
            return
        try:
            fresh, self.error_offset = webcam_errors.read_from(self.error_offset)
            if fresh:
                self.write_errors(fresh)
        except Exception:                                      # noqa: BLE001
            pass
        finally:
            self.root.after(self.ERROR_POLL, self.drain_errors)

    def write_errors(self, text):
        """Write logged failures in, coloured by which stage they came from.

        A traceback's indented lines are greyed so the headline stays
        readable: the first line says what failed and what was done instead,
        which is the part worth seeing at a glance, and the frames under it
        are there for when that is not enough.
        """
        stage = ''
        for line in text.splitlines(keepends=True):
            body = line.rstrip('\n')
            if body.startswith('['):
                where = body.split(']', 1)[-1].split(':', 1)[0].strip()
                stage = webcam_errors.stage_of(where) if webcam_errors else ''
                if stage not in self.ERROR_COLOURS:
                    stage = ''
                self.write(line, tag=f'err-{stage}')
            elif body.startswith('    '):
                self.write(line, tag='err-trace')
            else:
                self.write(line, tag=f'err-{stage}')

    def write(self, line, tag=None):
        # Follow the newest line only when the view is already at the bottom,
        # so scrolling back to read something does not get yanked away by the
        # next line arriving.
        at_bottom = self.text.yview()[1] > 0.999
        self.text.configure(state='normal')
        self.text.insert('end', line, tag or ())
        extra = int(self.text.index('end-1c').split('.')[0]) - self.MAX_LINES
        if extra > 0:
            self.text.delete('1.0', f'{extra + 1}.0')
        self.text.configure(state='disabled')
        if at_bottom:
            self.text.see('end')

    def height_needed(self):
        self.frame.update_idletasks()
        return self.frame.winfo_reqheight()


class Window:
    def __init__(self, root):
        self.root = root
        self.camera = Camera(DEVICE)
        self.meter = LightMeter(device=DEVICE) if LightMeter else None
        self.meter_on = tk.BooleanVar(value=True)
        self.bool_vars = {}
        # Both switches start at whatever they were left at, not at off: the
        # scheduler has been running on them all night and the window should
        # show what is actually in force rather than resetting it.
        saved = webcam_enhance.read_settings() if webcam_enhance else {}
        self.enh_night = tk.BooleanVar(value=bool(saved.get(
            getattr(webcam_enhance, 'KEY_NIGHT', ''), False)))
        # These two default to on, matching read_settings: every mode across
        # the day is corrected unless something turns it off.
        self.enh_all_day = tk.BooleanVar(value=bool(saved.get(
            getattr(webcam_enhance, 'KEY_ALL_DAY', ''), True)))
        self.enh_detect = tk.BooleanVar(value=bool(saved.get(
            getattr(webcam_enhance, 'KEY_DETECT', ''), True)))
        # The optional extra source: a frame taken off the camera itself,
        # rather than from the stills the recorder writes. Off by default.
        self.enh_sample = tk.BooleanVar(value=bool(saved.get(
            getattr(webcam_enhance, 'KEY_SAMPLE_STREAM', ''), False)))
        # Where the brain trains from. The picture folder defaults to on, for
        # the reason read_settings gives: a conf file written before these two
        # switches existed should not silently mean "never train".
        self.train_pics = tk.BooleanVar(value=bool(saved.get(
            getattr(webcam_enhance, 'KEY_TRAIN_PICTURES', ''), True)))
        self.train_cam = tk.BooleanVar(value=bool(saved.get(
            getattr(webcam_enhance, 'KEY_TRAIN_CAMERA', ''), False)))

        root.title('Webcam Settings')
        tk.Label(root, text='Webcam Controls', font=('Helvetica', 13, 'bold')).pack(pady=4)

        count = len(self.camera.controls)
        tk.Label(
            root,
            text=(f'{count} controls read from {DEVICE}' if count
                  else f'No controls readable from {DEVICE} - is the camera plugged in?'),
            font=('Helvetica', 9), fg='#003366',
        ).pack()

        # The whole window below the heading is one tall scrolling page, in
        # this order down it: the camera controls and presets side by side,
        # then the scheduler's terminal, then the brain's details. The
        # scrollbar down the right hand side moves all of it together.
        self.build_scroll_area()
        self.build_controls()
        self.build_presets()
        self.build_meter()
        self.build_enhance()
        self.terminal = TerminalPane(self.page, root, TERMINAL_SCRIPT, side='top')
        self.build_brain_info()
        self.build_status_tile()
        self.size_to_fit()
        root.protocol('WM_DELETE_WINDOW', self.on_close)
        if self.meter:
            root.after(6000, self.meter_tick)

    def on_close(self):
        """Take the child process with us. Without this the scheduler in the
        terminal would outlive the window that started it."""
        self.terminal.stop()
        self.root.destroy()

    # ---- scrolling ----
    #
    # A camera with every control the kernel defines makes a taller column than
    # most screens have room for, so everything below the heading scrolls. A
    # Frame cannot scroll itself, so the frame holding the columns goes into a
    # canvas as a canvas window and the canvas scrolls that.

    def build_scroll_area(self):
        area = tk.Frame(self.root)
        self.canvas = tk.Canvas(area, highlightthickness=0)
        # Wide enough to get hold of with the mouse - this is the slider for
        # the whole window, not an afterthought.
        vbar = tk.Scrollbar(area, orient='vertical', command=self.canvas.yview,
                            width=18)
        hbar = tk.Scrollbar(self.root, orient='horizontal', command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        hbar.pack(side='bottom', fill='x')
        self.canvas.pack(side='left', fill='both', expand=True)
        vbar.pack(side='right', fill='y')
        area.pack(fill='both', expand=True)

        # The page: everything that scrolls lives in here, stacked top to
        # bottom. A Frame cannot scroll itself, so it goes into the canvas as
        # a canvas window and the canvas scrolls it.
        self.page = tk.Frame(self.canvas)
        self.cols_id = self.canvas.create_window(0, 0, window=self.page, anchor='nw')
        self.page.bind('<Configure>',
                       lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', self.on_canvas_resize)

        # The two columns at the top of the page: controls on the left,
        # presets and the switches on the right.
        self.cols = tk.Frame(self.page)
        self.cols.pack(side='top', fill='x', anchor='nw')

        # X11 sends buttons 4 and 5 for the wheel; <MouseWheel> is the rest.
        self.root.bind_all('<Button-4>', lambda e: self.canvas.yview_scroll(-3, 'units'))
        self.root.bind_all('<Button-5>', lambda e: self.canvas.yview_scroll(3, 'units'))
        self.root.bind_all('<MouseWheel>',
                           lambda e: self.canvas.yview_scroll(-3 if e.delta > 0 else 3, 'units'))
        self.root.bind_all('<Next>', lambda e: self.canvas.yview_scroll(1, 'pages'))
        self.root.bind_all('<Prior>', lambda e: self.canvas.yview_scroll(-1, 'pages'))
        self.root.bind_all('<Home>', lambda e: self.canvas.yview_moveto(0))
        self.root.bind_all('<End>', lambda e: self.canvas.yview_moveto(1))

        self.left = tk.Frame(self.cols)
        self.left.pack(side='left', anchor='n', padx=6)
        self.right = tk.Frame(self.cols)
        self.right.pack(side='left', anchor='n', padx=6)

    def on_canvas_resize(self, event):
        # Let the columns spread into a widened window, but never squeeze them
        # below the width they asked for - squeezing would cut off the right
        # hand column instead of letting the horizontal scrollbar reach it.
        if event.width <= 1:
            return
        needed = self.page.winfo_reqwidth()
        self.canvas.itemconfigure(self.cols_id, width=max(event.width, needed))

    def size_to_fit(self):
        """Open at the size the page asks for, but never taller than the
        screen will take - which is the whole reason for the scrollbar.

        The page is now a tall one: controls, then the terminal, then the
        brain's details. It will not fit on any screen and is not meant to,
        any more than a web page is; it opens showing the top of itself and
        the scrollbar reaches the rest.
        """
        self.root.update()
        want_w = min(self.page.winfo_reqwidth(),
                     int(self.root.winfo_screenwidth() * 0.98))
        want_h = min(self.page.winfo_reqheight(),
                     int(self.root.winfo_screenheight() * 0.86))
        self.canvas.configure(width=want_w, height=max(want_h, 300),
                              scrollregion=self.canvas.bbox('all'))

    # ---- a control for everything the camera reported ----

    def build_controls(self):
        tk.Label(self.left, text='--- Camera Controls ---',
                 font=('Helvetica', 10, 'bold')).pack(pady=4)

        known = set(PREFERRED)
        order = [c for c in PREFERRED if c in self.camera.controls]
        order += sorted(c for c in self.camera.controls if c not in known)

        for name in order:
            info = self.camera.controls[name]
            live = self.camera.get(name)
            start = live if live is not None else info['default']

            if info['type'] == 'bool':
                var = tk.BooleanVar(value=bool(start))
                self.bool_vars[name] = var
                tk.Checkbutton(
                    self.left, text=name, variable=var, anchor='w',
                    command=lambda n=name, v=var: self.camera.set(n, 1 if v.get() else 0),
                ).pack(fill='x', padx=3)
                continue

            # menu controls are small integer ranges; a slider reads them well
            # enough and needs no table of names to keep up to date.
            row = tk.Frame(self.left)
            row.pack(fill='x', padx=3)
            tk.Label(row, text=name, width=26, anchor='w').pack(side='left')
            value_label = tk.Label(row, text=str(start), width=7)
            def on_move(value, name=name, label=value_label):
                label.configure(text=value)
                self.camera.set(name, value)

            scale = tk.Scale(
                row, from_=info['min'], to=info['max'], orient='horizontal',
                length=190, showvalue=0, command=on_move,
            )
            scale.set(start)
            scale.pack(side='left')
            value_label.pack(side='left')

    # ---- presets ----

    def build_presets(self):
        tk.Label(self.right, text='--- Presets ---',
                 font=('Helvetica', 10, 'bold')).pack(pady=4)
        grid = tk.Frame(self.right)
        grid.pack()
        self.preset_status = tk.Label(
            self.right, text='The engine clamps every value to this camera.',
            anchor='w', justify='left', font=('Helvetica', 8), wraplength=310)

        for index, (label, name, bg, fg) in enumerate(PRESETS):
            tk.Button(
                grid, text=label, bg=bg, fg=fg, width=10,
                command=lambda n=name: self.on_preset(n),
            ).grid(row=index // 3, column=index % 3, padx=3, pady=3)
        self.preset_status.pack(fill='x', padx=6)
        self.build_led_row()

    # ---- the LED on the front of the camera ----

    def build_led_row(self):
        """Off and on for the camera's own light, when the camera has one
        that software can reach.

        Most webcams do not. The privacy LED is often wired to the sensor's
        power rail, and on those nothing here can touch it - which is why
        this reports what the camera did rather than what it was told, and
        says plainly when the answer is no. A button that silently does
        nothing is worse than one that says it cannot.
        """
        row = tk.Frame(self.right)
        row.pack(fill='x', padx=6, pady=(4, 0))
        tk.Label(row, text='Camera LED:', font=('Helvetica', 9)).pack(
            side='left')
        self.led_off_button = tk.Button(
            row, text='Turn the LED off', font=('Helvetica', 8),
            command=lambda: self.on_led('off'))
        self.led_off_button.pack(side='left', padx=3)
        self.led_on_button = tk.Button(
            row, text='LED on', font=('Helvetica', 8),
            command=lambda: self.on_led('on'))
        self.led_on_button.pack(side='left', padx=3)
        # The other half of the question: not "is the LED off" but "is
        # anything I do here reaching the stream the recorder is taping".
        tk.Button(row, text='Is the camera listening?', font=('Helvetica', 8),
                  command=self.on_check_camera).pack(side='right', padx=2)
        self.led_status = tk.Label(
            self.right, text='', anchor='w', justify='left',
            font=('Helvetica', 8), wraplength=310, fg='#333333')
        self.led_status.pack(fill='x', padx=6)
        self.refresh_led_status()

    def refresh_led_status(self):
        """What the LED is doing now, without changing it."""
        if webcam_led is None:
            self.led_status.configure(
                text='webcam_led.py is not next to this file', fg='#aa0000')
            for button in (self.led_off_button, self.led_on_button):
                button.configure(state='disabled')
            return
        try:
            worked, text = webcam_led.status(DEVICE)
        except Exception as problem:                           # noqa: BLE001
            worked, text = False, f'could not ask the camera: {problem}'
        self.led_status.configure(text=text,
                                  fg='#333333' if worked else '#884400')
        # A camera with no LED control keeps the buttons, greyed, rather than
        # losing them - so the row still answers the question "can I?" next
        # time rather than looking like a feature that was never there.
        state = 'normal' if worked else 'disabled'
        for button in (self.led_off_button, self.led_on_button):
            button.configure(state=state)

    def on_led(self, mode):
        self.led_status.configure(text=f'asking the camera for LED {mode}...',
                                  fg='#003366')
        self.root.update()
        try:
            worked, text = webcam_led.set_mode(mode, DEVICE)
        except Exception as problem:                           # noqa: BLE001
            worked, text = False, f'could not reach the camera: {problem}'
        self.led_status.configure(text=text,
                                  fg='#0a7a2a' if worked else '#aa0000')
        if not worked and webcam_errors is not None:
            webcam_errors.note('camera', f'LED {mode}: {text}')

    def on_preset(self, name):
        self.preset_status.configure(text=f'Applying {name}...')
        self.root.update()
        ok = run_preset(name, DEVICE)
        # The preset just changed the light, so the autofocus needs a fresh
        # search on it; the engine does this too, and doing it twice costs
        # nothing but a third of a second.
        if self.meter:
            self.meter.refocus()
        self.preset_status.configure(
            text=(f'Applied {name}. The light meter will re-learn it as its baseline.'
                  if ok else
                  f'Could not run {name} - is webcam_presets.py next to this file?'))

    # ---- the light meter ----

    def build_meter(self):
        tk.Label(self.right, text='--- Automatic Light Adjustment (24/7) ---',
                 font=('Helvetica', 10, 'bold')).pack(pady=5)

        if not self.meter:
            tk.Label(self.right, anchor='w', justify='left', font=('Helvetica', 8),
                     wraplength=310,
                     text='webcam_light_meter.py was not found, so the light is '
                          'not being corrected. The presets above still work.',
                     ).pack(fill='x', padx=6)
            return

        tk.Label(
            self.right, anchor='w', justify='left', font=('Helvetica', 8), wraplength=310,
            text=('Corrects whichever preset is in force by measuring the picture itself '
                  'every 5 seconds, so it is neither too bright nor too dark. It does not '
                  'replace the presets or the scheduler - both keep working, and this '
                  'trims what they set. Press any preset and it re-learns that preset as '
                  'its new starting point. Colour is kept day and night, with less of it '
                  'at night.'),
        ).pack(fill='x', padx=6)

        tk.Label(self.right, text=self.meter.range_report(), anchor='w', justify='left',
                 font=('Helvetica', 8), wraplength=310, fg='#003366').pack(fill='x', padx=6)

        snap_row = tk.Frame(self.right)
        snap_row.pack(fill='x', padx=6)
        tk.Label(snap_row, text='Snapshots:', font=('Helvetica', 8)).pack(side='left')
        self.snap_var = tk.StringVar(value=self.meter.snapshot)
        entry = ttk.Entry(snap_row, textvariable=self.snap_var, width=36)
        entry.pack(side='left')
        self.snap_var.trace_add('write',
                                lambda *_: setattr(self.meter, 'snapshot', self.snap_var.get()))

        buttons = tk.Frame(self.right)
        buttons.pack(fill='x', padx=6, pady=2)
        tk.Checkbutton(buttons, text='Adjust the light automatically',
                       variable=self.meter_on, font=('Helvetica', 8),
                       command=self.on_meter_toggle).pack(side='left')
        tk.Button(buttons, text='Clear trim', font=('Helvetica', 8),
                  command=self.on_clear_trim).pack(side='left', padx=4)
        tk.Button(buttons, text='Snapshot + test', font=('Helvetica', 8),
                  command=self.on_test).pack(side='left', padx=4)

        target_row = tk.Frame(self.right)
        target_row.pack(fill='x', padx=6)
        tk.Label(target_row, text='Target brightness', font=('Helvetica', 8)).pack(side='left')
        self.target = tk.Scale(target_row, from_=60, to=160, orient='horizontal', length=150,
                               command=lambda v: setattr(self.meter, 'target', int(v)))
        self.target.set(self.meter.target)
        self.target.pack(side='left')

        self.meter_status = tk.Label(self.right, text='Automatic light adjustment starting',
                                     anchor='w', justify='left', font=('Helvetica', 8),
                                     wraplength=310)
        self.meter_status.pack(fill='x', padx=6)
        self.meter_detail = tk.Label(self.right, text=self.meter.prepare_snapshots(),
                                     anchor='w', justify='left', font=('Helvetica', 8),
                                     wraplength=310)
        self.meter_detail.pack(fill='x', padx=6)

        # Where to read back what went wrong hours ago, since the lines above
        # only ever show the latest thing.
        tk.Label(self.right, text=f'Error log: {LOG_FILE}', anchor='w', justify='left',
                 font=('Helvetica', 8), wraplength=310, fg='#555555').pack(fill='x', padx=6)

    # ---- brain G's details, under the terminal ----

    def build_brain_info(self):
        """Everything about the brain that is doing the correcting.

        This sits at the bottom of the page, under the scheduler's terminal,
        and it is here because a trained model is otherwise completely opaque:
        there is no way to tell from the picture whether the brain loaded,
        whether it is the one that shipped or one retrained on this camera, or
        whether it is any good. Every number it was judged on is on the
        screen, including the score a flat "lift everything the same amount"
        answer got, which is the only honest way to read the other numbers.
        """
        frame = tk.LabelFrame(self.page, text='  brain G - the night vision brain  ',
                              font=('Helvetica', 9, 'bold'))
        frame.pack(side='top', fill='x', padx=4, pady=(2, 6))
        self.brain_frame = frame

        bar = tk.Frame(frame)
        bar.pack(fill='x')
        self.brain_status = tk.Label(bar, text='reading the brain...', anchor='w',
                                     font=('Helvetica', 8, 'bold'), fg='#003366')
        self.brain_status.pack(side='left', padx=4)
        tk.Button(bar, text='Refresh', font=('Helvetica', 8),
                  command=self.refresh_brain_info).pack(side='right', padx=2)

        # Delete and start again. This is here because a trained model can come
        # out wrong in a way no score catches - too dark, washed out, strange
        # colour - and then the brain is what did it and the fix is to throw it
        # away and train another on what has been harvested since. The corpus
        # of frames is not touched, so nothing is lost that cannot be rebuilt.
        row = tk.Frame(frame)
        row.pack(fill='x', padx=4, pady=(2, 0))
        self.brain_reset_button = tk.Button(
            row, text='delete old brain and restart with new brain',
            font=('Helvetica', 8), command=self.on_reset_brain)
        self.brain_reset_button.pack(side='left')
        tk.Label(row,
                 text='If image is corrected too dark or incorrectly, delete '
                      'and retrain the machine learning model.',
                 font=('Helvetica', 8), anchor='w', justify='left',
                 wraplength=420).pack(side='left', padx=6)

        # ---- where the brain trains from ----
        #
        # Two boxes rather than one, because the two sources are not equivalent
        # and on this machine only one of them is usually available. The
        # picture folder is always there: the light meter has been filling it
        # with frames of the real scene in the real light, which is exactly
        # what makes this brain better than a general one. The camera is the
        # better source in principle - frames straight off the sensor, no JPEG
        # round trip - and is only possible while nothing else holds the
        # device, which on a machine that is recording is rarely.
        #
        # Ticking a box trains from it now and leaves it selected, so the
        # automatic retrain uses the same source. The Perl window and the
        # scheduler read the same conf file, so a box ticked here means the
        # same thing to all three.
        train = tk.LabelFrame(frame, text='  where the brain trains from  ',
                              font=('Helvetica', 9, 'bold'))
        train.pack(fill='x', padx=4, pady=(4, 2))
        # The meter owns the snapshot folder, so the box shows whatever it is
        # actually set to rather than a path written in here.
        snaps = getattr(self.meter, 'snapshot', None) if self.meter else None
        if not snaps:
            snaps = os.path.expanduser('~/Pictures/webcam-sample-images')
        tk.Checkbutton(train, text=f'train brain from {snaps}',
                       variable=self.train_pics, font=('Helvetica', 8), anchor='w',
                       command=lambda: self.on_train_toggle('pictures')
                       ).pack(fill='x', padx=6)
        tk.Checkbutton(train, text='train brain direct from the detected webcamera',
                       variable=self.train_cam, font=('Helvetica', 8), anchor='w',
                       command=lambda: self.on_train_toggle('camera')
                       ).pack(fill='x', padx=6)

        train_row = tk.Frame(train)
        train_row.pack(fill='x', padx=6, pady=2)
        self.brain_train_button = tk.Button(
            train_row, text='Train now from the ticked sources',
            font=('Helvetica', 8), command=self.on_train_now)
        self.brain_train_button.pack(side='left')
        self.train_status = tk.Label(train_row, text=self.train_summary(),
                                     font=('Helvetica', 8), anchor='w',
                                     justify='left', wraplength=360, fg='#003366')
        self.train_status.pack(side='left', padx=6)

        tk.Label(train, anchor='w', justify='left', font=('Helvetica', 8),
                 wraplength=420, fg='#555555',
                 text=('The camera box can only work while nothing else has the '
                       'camera: a V4L2 device lets one program stream at a time, '
                       'so while the recording software is running it is held and '
                       'the picture folder is the source that is actually '
                       'available. The box says who has it rather than failing '
                       'quietly.')).pack(fill='x', padx=6, pady=(0, 2))

        # A read-only Text rather than a column of labels: the details run to
        # twenty-odd lines and a Text lays them out in one monospaced block
        # that lines up, without a widget per line.
        body = tk.Frame(frame)
        body.pack(fill='both', expand=True)
        self.brain_text = tk.Text(body, width=TERMINAL_COLS, height=22, wrap='word',
                                  font=('Monospace', 9), bg='#101820', fg='#cfd8dc',
                                  highlightthickness=0, state='disabled')
        self.brain_text.pack(side='left', fill='both', expand=True)
        # The wheel bindings for the page are bind_all, so without these the
        # wheel over this block would scroll the page behind it instead.
        for sequence, amount in (('<Button-4>', -3), ('<Button-5>', 3)):
            self.brain_text.bind(
                sequence,
                lambda e, n=amount: (self.brain_text.yview_scroll(n, 'units'), 'break')[1])
        self.brain_text.bind(
            '<MouseWheel>',
            lambda e: (self.brain_text.yview_scroll(-3 if e.delta > 0 else 3, 'units'),
                       'break')[1])
        self.refresh_brain_info()

    def train_summary(self):
        """One line saying what the two boxes add up to."""
        if not webcam_enhance:
            return 'no enhancement module, so there is nothing to train'
        if not self.train_pics.get() and not self.train_cam.get():
            return 'Both boxes off - the brain is not being trained.'
        where = []
        if self.train_cam.get():
            where.append('the camera')
        if self.train_pics.get():
            where.append('the picture folder')
        return 'Training from ' + ' and then '.join(where) + '.'

    def on_train_toggle(self, which):
        """A box was ticked or unticked.

        Write it out so the Perl window and the scheduler agree, then - when it
        was just ticked - train from it, because ticking it is the instruction
        and waiting until tomorrow's automatic retrain to act on it would look
        like the box did nothing. Unticking one is an instruction to stop using
        that source, so nothing is started on the way out.
        """
        if not webcam_enhance:
            self.train_status.configure(
                text='webcam_enhance.py is not here, so there is nothing to train')
            return
        ok = webcam_enhance.write_settings(train_pictures=self.train_pics.get(),
                                           train_camera=self.train_cam.get())
        text = self.train_summary()
        if not ok:
            text += f' (could not write {webcam_enhance.CONF_FILE})'
        self.train_status.configure(text=text)

        turned_on = self.train_cam.get() if which == 'camera' else self.train_pics.get()
        if turned_on:
            self.on_train_now()

    def on_train_now(self):
        """Train from whichever boxes are ticked.

        On a worker thread that only ever puts text on a queue, the same
        arrangement the reset button uses and for the same reason: training
        takes a minute or two and a minute or two on the thread running the
        event loop is a window that has stopped repainting. The camera burst
        happens on the worker too, since it is a couple of seconds of ffmpeg.
        """
        if not webcam_enhance or not train_webcam_brain:
            self.train_status.configure(
                text='train_webcam_brain.py is not next to this file')
            return
        if getattr(self, 'brain_worker', None) and self.brain_worker.is_alive():
            return
        if not self.train_pics.get() and not self.train_cam.get():
            self.train_status.configure(
                text='Neither box is ticked, so there is nothing to train from.')
            return

        device = DEVICE
        folder = None
        if self.meter is not None:
            try:
                folder = self.meter.snap_dir()
            except Exception:                                 # noqa: BLE001
                folder = None
        if not folder:
            folder = os.path.expanduser('~/Pictures/webcam-sample-images')

        self.brain_reset_button.configure(state='disabled')
        self.brain_train_button.configure(state='disabled')
        self.train_status.configure(text='Looking for frames to train from...')
        self.brain_lines = queue.Queue()

        def work():
            try:
                sources, note = webcam_enhance.training_sources(device=device,
                                                                folder=folder)
                if note:
                    self.brain_lines.put(note)
                if not sources:
                    self.brain_lines.put(note or 'nothing to train from')
                    return
                # One run per source, in order, so a folder that fails does not
                # take the other one down with it.
                for label, path in sources:
                    self.brain_lines.put(f'training from {label}: {path}')
                    try:
                        train_webcam_brain.train(photo_folder=path, force=True,
                                                 quiet=True)
                        self.brain_lines.put(f'trained from {label}')
                    except Exception as problem:              # noqa: BLE001
                        self.brain_lines.put(
                            f'could not train from {label}: {problem}')
            except Exception as problem:                      # noqa: BLE001
                self.brain_lines.put(f'could not train: {problem}')
            finally:
                self.brain_lines.put(None)       # the done marker

        self.brain_worker = threading.Thread(target=work, daemon=True)
        self.brain_worker.start()
        self.root.after(400, self.drain_brain_worker)

    def on_reset_brain(self):
        """Delete the brain and train a new one from the harvest.

        Training takes a minute or two, and a minute or two of work on the
        thread running the event loop is a frozen window. So it goes to a
        worker thread, and the worker only ever puts text on a queue - the
        event loop drains that queue on a timer and is the only thing that
        touches a widget, which is the one arrangement Tk is safe under.
        """
        if not train_webcam_brain:
            self.brain_status.configure(
                text='train_webcam_brain.py is not next to this file')
            return
        if getattr(self, 'brain_worker', None) and self.brain_worker.is_alive():
            return

        self.brain_reset_button.configure(state='disabled')
        self.brain_status.configure(text='deleting the brain and training a '
                                         'new one, this takes a minute...')
        self.brain_lines = queue.Queue()

        def work():
            try:
                train_webcam_brain.reset_and_retrain(
                    report=self.brain_lines.put)
            except Exception as problem:                      # noqa: BLE001
                self.brain_lines.put(f'could not retrain: {problem}')
            finally:
                self.brain_lines.put(None)       # the done marker

        self.brain_worker = threading.Thread(target=work, daemon=True)
        self.brain_worker.start()
        self.root.after(400, self.drain_brain_worker)

    def drain_brain_worker(self):
        """Show what the worker has said, and tidy up when it has finished."""
        finished = False
        while True:
            try:
                line = self.brain_lines.get_nowait()
            except queue.Empty:
                break
            if line is None:
                finished = True
                break
            self.brain_status.configure(text=str(line)[:160])
        if finished:
            self.brain_reset_button.configure(state='normal')
            if getattr(self, 'brain_train_button', None):
                self.brain_train_button.configure(state='normal')
            if getattr(self, 'train_status', None):
                self.train_status.configure(text=self.train_summary())
            if webcam_enhance:
                # The brain on disk has changed, so drop the loaded one.
                webcam_enhance._brain.loaded = None
            self.refresh_brain_info()
            return
        self.root.after(400, self.drain_brain_worker)

    def brain_details(self):
        """The status line and the block of detail, as (headline, text)."""
        if not webcam_enhance:
            return ('no enhancement module',
                    'webcam_enhance.py was not found next to this file, so there '
                    'is no image correction and no brain.\n')

        brain = webcam_enhance._brain()
        if brain is None:
            why = getattr(webcam_enhance._brain, 'why', '') or 'no trained brain found'
            looked = '\n'.join('    ' + d for d in webcam_enhance.brain_dirs())
            return ('not loaded - the analytic correct is doing the work',
                    f'Why: {why}\n\nLooked in, in this order:\n{looked}\n\n'
                    'The analytic correct still runs, so the two switches above '
                    'still do something - it works out one gamma from the whole '
                    'frame rather than a gain for every pixel.\n\n'
                    + self.harvest_section({}))

        model, meta, folder = brain

        def line(label, value):
            return f'  {label:<22} {value}\n'

        units = meta.get('foundation_units') or []
        val = meta.get('val_loss')
        flat = meta.get('flat_answer_val_loss')
        better = ''
        if isinstance(val, (int, float)) and isinstance(flat, (int, float)) and flat:
            better = f'  ({(1 - val / flat) * 100:.0f}% better than a flat answer)'

        text = 'WHAT IT DOES\n'
        text += ('  Predicts, for every pixel, how much to lift its brightness and\n'
                 '  what to do with its colour afterwards. Trained on clear photos:\n'
                 '  each one was darkened with a camera model and the brain learned\n'
                 '  its way back to the original. A per-pixel gain is why a car in\n'
                 '  shadow can be lifted hard while the light above it is left alone.\n\n')

        text += 'MODEL\n'
        text += line('architecture', meta.get('architecture', '?'))
        text += line('features per pixel', meta.get('feature_count', '?'))
        text += line('outputs', ', '.join(meta.get('outputs') or ['?']))
        text += line('most it may lift', f'{meta.get("gain_max", "?")}x')
        text += line('foundation', 'FROZEN, hand weighted, never trained')
        if units:
            text += '  frozen units\n'
            for index, name in enumerate(units):
                text += f'      {index}  {name}\n'
        text += '\n'

        text += 'TRAINING\n'
        text += line('pretrain samples', f'{meta.get("pretrain_samples", "?")} '
                                         f'(cap {meta.get("pretrain_cap", "?")})')
        text += line('epochs run', f'{meta.get("epochs_run", "?")} of '
                                   f'{meta.get("epochs_max", "?")}, early stopping '
                                   f'on the held-back photos')
        text += line('clear photos used', f'{meta.get("photos_trained_on", "?")} trained on, '
                                          f'{meta.get("photos_held_back", "?")} held back')
        text += line('photo folder', meta.get('photo_folder', '?'))
        text += line('trained', meta.get('trained_at', '?'))
        text += line('tensorflow', meta.get('tensorflow', '?'))
        text += '\n'

        text += 'HOW WELL IT DID\n'
        text += line('val loss', f'{val:.5f}{better}' if isinstance(val, (int, float))
                     else '?')
        text += line('a flat answer scores', f'{flat:.5f}' if isinstance(flat, (int, float))
                     else '?')
        mae = meta.get('val_mae')
        text += line('val mean error',
                     f'{mae:.4f}' if isinstance(mae, (int, float)) else '?')
        text += ('  The photos it was scored on were held back whole, not by pixel,\n'
                 '  so no crop it trained on appears in the score.\n\n')

        scored = meta.get('evaluation') or {}
        if scored:
            text += 'THE FINISHED PICTURE, ON THOSE SAME HELD-BACK PHOTOS\n'
            text += ('  Eight degraded frames of each, run through the real pipeline\n'
                     '  and compared with the clear original. Higher is better.\n\n')
            text += f'    {"stage":<22}{"PSNR dB":>9}{"SSIM":>8}\n'
            for key, label in (('dark_input', 'one dark frame'),
                               ('stack_and_clahe', 'stacked + CLAHE'),
                               ('analytic', '+ analytic correct'),
                               ('brain', '+ brain G')):
                peak = scored.get(f'{key}_psnr_db')
                sim = scored.get(f'{key}_ssim')
                if peak is None:
                    continue
                text += (f'    {label:<22}{peak:>9.2f}'
                         + (f'{sim:>8.3f}' if sim is not None else f'{"-":>8}') + '\n')
            gap = scored.get('brain_beats_analytic_db')
            if gap is not None:
                text += (f'\n  The brain is {gap:+.2f} dB on the analytic correct, which is\n'
                         '  what runs when there is no brain to load.\n')
            text += '\n'

        text += 'ON DISK\n'
        text += line('folder', folder)
        digest = meta.get('model_sha256') or ''
        text += line('sha256', (digest[:32] + '...') if digest else 'not recorded')
        text += line('checked at load', 'feature count and sha256; a mismatch means '
                                        'the analytic correct runs instead')
        # The mode bits, not os.access: root can write to a 0444 file, so
        # os.access would report a read-only model as writable when this runs
        # as root and the line would be wrong exactly where it matters.
        model_file = os.path.join(folder, 'model.keras')
        try:
            mode = os.stat(model_file).st_mode & 0o222
        except OSError:
            mode = -1
        text += line('read-only', 'cannot tell' if mode < 0 else
                     'yes - locked, and inference never writes to it' if mode == 0 else
                     'writable (a git clone comes back this way); inference still '
                     'never writes to it. To lock it: chmod 444 ' + model_file)
        text += '\n'

        text += self.harvest_section(meta)

        headline = f'loaded from {folder}'
        return (headline, text)

    def harvest_section(self, meta):
        """What has been harvested from this camera, and what will happen next.

        The harvest is the part worth watching, because it is what makes the
        brain this camera's rather than a general one. The corpus is also the
        valuable half: a model can be deleted and retrained from it in a
        minute, and the frames cannot be got back without waiting another day.
        """
        if not train_webcam_brain:
            return ('TRAINING ON THIS CAMERA\n'
                    '  train_webcam_brain.py is not next to this file, so nothing\n'
                    '  is being harvested and the brain cannot retrain itself.\n')
        try:
            state = train_webcam_brain.corpus_state()
            ready, why = train_webcam_brain.ready_to_train(state)
        except Exception as problem:                           # noqa: BLE001
            return f'TRAINING ON THIS CAMERA\n  could not read the harvest: {problem}\n'

        def line(label, value):
            return f'  {label:<22} {value}\n'

        text = 'TRAINING ON THIS CAMERA\n'
        text += ('  Every pass files a still into a training set, tagged with the\n'
                 '  preset and how hard the camera was working, and a preset or light\n'
                 '  change files one immediately. Once there is a spread across the\n'
                 '  presets and the day, it retrains on them and deletes the shipped\n'
                 '  brain. Only well-exposed frames can be training targets: a dark\n'
                 '  frame cannot be its own answer sheet, so one is kept only when a\n'
                 '  well-exposed frame of the same scene lands within a couple of\n'
                 '  minutes to be its answer - dusk, dawn, or a light coming on.\n\n')
        text += line('trained from', meta.get('trained_from', 'not trained yet'))
        text += line('well-exposed frames', state['clear_frames'])
        text += line('dark frames banked', f'{state["dim_frames"]} '
                                           f'({state["real_pairs"]} with an answer '
                                           f'beside them)')
        text += line('preset/light spread', f'{state["bucket_count"]} combinations')
        text += line('presets seen', ', '.join(state['presets']) or 'none yet')
        text += line('hours covered', state['hours_covered'])
        text += line('retrainings so far', state['trainings'])
        text += line('corpus folder', state['folder'])
        text += line('next', 'ready to retrain' if ready else why)

        against = meta.get('against_shipped')
        if isinstance(against, dict):
            text += '\n  Against the shipped brain, on the same held-back frames\n'
            text += '  from this camera:\n'
            text += line('    this camera\'s brain', f'{against.get("own_psnr_db")} dB')
            text += line('    the shipped brain', f'{against.get("shipped_psnr_db")} dB')
            text += line('    shipped brain', 'deleted' if against.get('shipped_deleted')
                         else 'KEPT - the new one scored worse, so it was not thrown '
                              'away; it will retrain tomorrow')
        text += ('\n  To train it by hand on a folder of clear photos instead:\n'
                 '    python3 train_webcam_brain.py --photos <that folder> --force\n')
        return text

    def refresh_brain_info(self):
        """Re-read the brain and redraw the block.

        Worth having a button for: retraining happens in a terminal while this
        window is open, and this is how the window notices.
        """
        if webcam_enhance:
            # Drop the cached load so a brain retrained since this window
            # opened is picked up rather than the old one being reported.
            webcam_enhance._brain.loaded = None
        try:
            headline, text = self.brain_details()
        except Exception as problem:                           # noqa: BLE001
            headline, text = ('could not read the brain', f'{problem}\n')
        self.brain_status.configure(text=headline)
        self.brain_text.configure(state='normal')
        self.brain_text.delete('1.0', 'end')
        self.brain_text.insert('end', text)
        self.brain_text.configure(state='disabled')

    # ---- is it working, and what did it do to the picture ----

    def build_status_tile(self):
        """The bottom tile: whether the libraries work, and the four frames.

        Two questions this answers that nothing else on the page does.

        Is it working - OpenCV and TensorFlow are both needed and they fail
        differently. OpenCV does the stacking, the CLAHE, the features and the
        detector; TensorFlow runs brain G. Either can be installed and still
        not run - a conda build sitting next to a pip one is the usual way -
        and from the picture alone that looks the same as a working pipeline
        having a quiet night. So each is imported and then actually used, and
        only counts as working when the real call returns.

        And what did it do - the four frames, two rows of two, each row the
        width of the terminal above them. The top row is the correction: the
        frame as the camera gave it, then the same frame stacked, CLAHE'd and
        lifted by brain G. The row under it is the enhancement that runs on
        top of that: the corrected frame again, then the frame with the car or
        the person found in it and given a second lift of their own, with the
        box drawn round what was picked out. Green for a person, blue for a
        vehicle, the same colours webcam_detect.py draws.
        """
        frame = tk.LabelFrame(
            self.page, text='  is it working, and what it did to the picture  ',
            font=('Helvetica', 9, 'bold'))
        frame.pack(side='top', fill='x', expand=False, padx=4, pady=(2, 8))
        self.status_frame = frame

        bar = tk.Frame(frame)
        bar.pack(fill='x')
        self.lib_summary = tk.Label(bar, text='checking the libraries...',
                                    anchor='w', font=('Helvetica', 8),
                                    fg='#003366')
        self.lib_summary.pack(side='left', padx=4)
        tk.Button(bar, text='Re-check', font=('Helvetica', 8),
                  command=self.refresh_status_tile).pack(side='right', padx=2)
        tk.Button(bar, text='Show recent errors', font=('Helvetica', 8),
                  command=self.dump_errors).pack(side='right', padx=2)
        tk.Button(bar, text='Clear error log', font=('Helvetica', 8),
                  command=self.clear_errors).pack(side='right', padx=2)

        # One row per library, filled in by the worker below.
        self.lib_rows = {}
        grid = tk.Frame(frame)
        grid.pack(fill='x', padx=6, pady=(2, 4))
        for index, name in enumerate(('OpenCV', 'TensorFlow',
                                      'Object detector', 'Brain G')):
            tk.Label(grid, text=f'{name}:', anchor='w', width=16,
                     font=('Helvetica', 8, 'bold')).grid(row=index, column=0,
                                                         sticky='w')
            state = tk.Label(grid, text='checking...', anchor='w', width=12,
                             font=('Monospace', 8), fg='#666666')
            state.grid(row=index, column=1, sticky='w')
            detail = tk.Label(grid, text='', anchor='w',
                              font=('Helvetica', 8), fg='#333333',
                              justify='left', wraplength=620)
            detail.grid(row=index, column=2, sticky='w')
            self.lib_rows[name] = (state, detail)

        # The pictures. Two rows of two, with a caption under each.
        self.compare_images = {}      # name -> PhotoImage, held against GC
        self.compare_labels = {}
        pictures = tk.Frame(frame)
        pictures.pack(fill='x', padx=6, pady=(0, 2))
        self.compare_note = tk.Label(
            pictures, text='no comparison frames yet - they are written the '
                           'next time a snapshot is corrected',
            anchor='w', font=('Helvetica', 8), fg='#003366', justify='left',
            wraplength=740)
        self.compare_note.pack(fill='x', pady=(0, 2))

        # Two wide and three tall. The first two rows are the whole frame -
        # the correction, then what the detector did on top of it - and the
        # third is the close crop of whatever was found, which is the row
        # that answers whether the number plate or the face can be read.
        stages = webcam_status.STAGES if webcam_status else []
        columns = getattr(webcam_status, 'GRID_COLUMNS', 2) if webcam_status else 2
        rows = getattr(webcam_status, 'GRID_ROWS', 3) if webcam_status else 3
        for row in range(rows):
            line = tk.Frame(pictures)
            line.pack(fill='x')
            for column in range(columns):
                index = row * columns + column
                name, caption = (stages[index] if index < len(stages)
                                 else (f'slot{index}', ''))
                cell = tk.Frame(line, bd=1, relief='sunken')
                cell.pack(side='left', padx=(0, 6), pady=2)
                # The crop row is shorter: a cut-out of a car is a wide thin
                # thing and giving it the height of a whole frame would leave
                # most of the cell empty.
                height = 8 if name.startswith('crop-') else 12
                blank = ('(nothing found to crop yet)'
                         if name.startswith('crop-') else '(not written yet)')
                picture = tk.Label(cell, text=blank,
                                   font=('Helvetica', 8), fg='#888888',
                                   bg='#101010', width=46, height=height)
                picture.pack()
                tk.Label(cell, text=caption, font=('Helvetica', 8),
                         anchor='w', fg='#222222').pack(fill='x')
                self.compare_labels[name] = picture

        legend = tk.Frame(pictures)
        legend.pack(fill='x', pady=(2, 0))
        tk.Label(legend, text='boxes:', font=('Helvetica', 8),
                 fg='#333333').pack(side='left')
        tk.Button(legend, text='Original image, full size',
                  font=('Helvetica', 8),
                  command=self.open_original_window).pack(side='right', padx=2)
        for label, colour, _bgr in (webcam_status.LEGEND if webcam_status
                                    else []):
            tk.Label(legend, text=f'  {label}  ', font=('Helvetica', 8, 'bold'),
                     bg=colour, fg='#000000').pack(side='left', padx=3)

        self.status_worker = None
        self.status_queue = queue.Queue()
        self.refresh_status_tile()
        self.root.after(COMPARE_POLL_MS, self.poll_compare_frames)

    def refresh_status_tile(self):
        """Run the checks off the main thread and redraw when they land.

        Importing TensorFlow takes seconds, and doing that on the thread
        running the event loop would freeze the window while it happened -
        which looks exactly like the crash this tile is meant to diagnose.
        """
        if webcam_status is None:
            self.lib_summary.configure(
                text='webcam_status.py is not next to this file',
                fg='#aa0000')
            return
        if self.status_worker and self.status_worker.is_alive():
            return
        self.lib_summary.configure(text='checking the libraries...',
                                   fg='#003366')

        def work():
            try:
                self.status_queue.put(('checks', webcam_status.all_checks()))
            except Exception as problem:                       # noqa: BLE001
                self.status_queue.put(('failed', problem))

        self.status_worker = threading.Thread(target=work, daemon=True)
        self.status_worker.start()
        self.root.after(200, self.drain_status_worker)

    def drain_status_worker(self):
        try:
            kind, payload = self.status_queue.get_nowait()
        except queue.Empty:
            if self.status_worker and self.status_worker.is_alive():
                self.root.after(200, self.drain_status_worker)
            return
        if kind == 'failed':
            self.lib_summary.configure(text=f'the checks themselves failed: '
                                            f'{payload}', fg='#aa0000')
            return
        self.show_checks(payload)
        self.load_compare_frames()

    def show_checks(self, rows):
        """Fill the four library rows in, and say in one line how many of them
        are working - which is what gets read at a glance."""
        working = 0
        for row in rows:
            widgets = self.lib_rows.get(row['name'])
            if not widgets:
                continue
            state, detail = widgets
            if row['ok']:
                working += 1
                state.configure(text='working', fg='#006600')
            else:
                state.configure(text='NOT working', fg='#aa0000')
            version = f"{row['version']}  " if row['version'] else ''
            hint = f"   try: {row['hint']}" if row['hint'] and not row['ok'] else ''
            detail.configure(text=f"{version}{row['detail']}{hint}",
                             fg='#333333' if row['ok'] else '#aa0000')
        total = len(rows)
        newest = webcam_errors.last_line() if webcam_errors else ''
        summary = f'{working} of {total} working'
        if newest:
            summary += f'   -   newest failure: {newest[:90]}'
        self.lib_summary.configure(
            text=summary, fg='#003366' if working == total else '#aa0000')

    # ---- the four frames ----

    def compare_width(self):
        """How wide the pair of pictures should be: the width of the terminal
        above them, so the rows line up with it and with each other."""
        try:
            width = self.terminal.text.winfo_width()
        except Exception:                                      # noqa: BLE001
            width = 0
        if width < 100:          # before the window is mapped
            width = TERMINAL_COLS * 8
        return max(360, min(1600, width))

    def load_compare_frames(self):
        """Rebuild the thumbnails and hang them on the six labels.

        Written as PPM by webcam_status, which tkinter's own PhotoImage reads
        with nothing else installed - no Pillow, which is one more dependency
        to go wrong on a window that installs its own.
        """
        if webcam_status is None:
            return
        try:
            thumbs = webcam_status.thumbnails(self.compare_width())
        except Exception as problem:                           # noqa: BLE001
            self.compare_note.configure(
                text=f'could not build the comparison thumbnails: {problem}',
                fg='#aa0000')
            return

        shown = 0
        for thumb in thumbs:
            label = self.compare_labels.get(thumb['name'])
            if label is None:
                continue
            if not thumb['path']:
                # The crop pair is only written when something was actually
                # found, so an empty one is an ordinary quiet night rather
                # than a failure, and says so.
                blank = ('(nothing found to crop yet)'
                         if thumb['name'].startswith('crop-')
                         else '(not written yet)')
                label.configure(image='', text=blank, fg='#888888')
                self.compare_images.pop(thumb['name'], None)
                continue
            try:
                picture = tk.PhotoImage(file=thumb['path'])
            except Exception as problem:                       # noqa: BLE001
                label.configure(image='', text=f'(unreadable: {problem})',
                                fg='#aa0000')
                continue
            self.compare_images[thumb['name']] = picture
            label.configure(image=picture, text='',
                            width=thumb['width'], height=thumb['height'])
            shown += 1

        # The separate window, when it is open, follows the same frames.
        self.refresh_original_window()

        note = webcam_status.compare_note()
        if shown:
            self.compare_note.configure(
                text=note or f'{shown} of 4 frames shown', fg='#003366')
        else:
            self.compare_note.configure(
                text='no comparison frames yet - they are written the next '
                     'time a snapshot is corrected', fg='#666666')

    def poll_compare_frames(self):
        """Pick up a newer set of frames without being asked.

        Two things wanted here and they pull in opposite directions: show a
        new set the moment it is written, and do not cost anything on a quiet
        night when nothing is being written at all.

        So this looks at the file times every few seconds - which is a stat
        on six files and nothing else - and only loads when one of them has
        moved. A set written between two polls is on the screen within
        COMPARE_POLL_MS rather than waiting out a fixed half minute. Every
        COMPARE_FORCE_MS it reloads anyway, whatever the times say, which
        covers a clock that went backwards over a daylight saving change and
        a file written twice inside the same second.
        """
        try:
            if webcam_status is not None:
                folder = webcam_status.compare_dir()
                newest = 0
                for name, _caption in webcam_status.STAGES:
                    path = os.path.join(folder, name.replace('.ppm', '.jpg'))
                    if os.path.exists(path):
                        newest = max(newest, os.path.getmtime(path))
                now = time.time()
                due = (now - getattr(self, 'compare_loaded_at', 0) >
                       COMPARE_FORCE_MS / 1000.0)
                if (newest and newest > getattr(self, 'compare_seen', 0)) or due:
                    self.compare_seen = max(newest,
                                            getattr(self, 'compare_seen', 0))
                    self.compare_loaded_at = now
                    self.load_compare_frames()
        except Exception:                                      # noqa: BLE001
            pass
        finally:
            self.root.after(COMPARE_POLL_MS, self.poll_compare_frames)

    def on_check_camera(self):
        """Does the camera take what this window sends, and is the picture it
        is producing usable?

        Worth asking separately, because the recorder - Xeoma, here - holds
        the camera nearly all the time. Controls are per device rather than
        per handle, so writes from outside the recorder do reach the stream it
        is taping; what is not guaranteed is that they stay, and v4l2-ctl
        reports success for a write the camera then ignores. So this reads
        every control back and measures the newest still, and reports what the
        camera says rather than what it was told.
        """
        if webcam_verify is None:
            self.led_status.configure(text='webcam_verify.py is not here',
                                      fg='#aa0000')
            return
        self.led_status.configure(text='asking the camera...', fg='#003366')
        self.root.update()
        try:
            now = webcam_verify.read_controls(DEVICE)
            path, _when = webcam_verify.newest_snapshot()
            shot = webcam_verify.measure(path)
            ok, why = webcam_verify.verdict(shot)
        except Exception as problem:                           # noqa: BLE001
            self.led_status.configure(
                text=f'could not ask the camera: {problem}', fg='#aa0000')
            return
        if not now:
            self.led_status.configure(
                text='v4l2-ctl found no controls - the camera is not there, '
                     'or v4l2-ctl is not installed', fg='#aa0000')
            return
        bits = [f'{len(now)} controls readable']
        for name in ('brightness', 'contrast', 'gain'):
            if name in now:
                bits.append(f'{name}={now[name]}')
        age = ''
        if path:
            try:
                age = (f', newest still '
                       f'{int(time.time() - os.path.getmtime(path))}s old')
            except OSError:
                age = ''
        self.led_status.configure(text=f"{', '.join(bits)}{age} - {why}",
                                  fg='#0a7a2a' if ok else '#aa0000')
        if webcam_errors is not None:
            webcam_errors.note('camera', f"checked: {', '.join(bits)} - {why}")

    # ---- the untouched frame, in a window of its own ----

    def open_original_window(self):
        """A second window showing nothing but the frame as the camera gave
        it, as large as it will go.

        The grid shows the original at a sixth of the tile, which is enough
        to see that the correction did something and not enough to see what
        the camera actually sees. That is a different question - whether
        there is anything in the raw frame at all, or whether the exposure
        needs changing rather than the correction - and it needs the picture
        big and on its own, with nothing lifted, stacked or drawn on it.

        Opened once: asking again raises the window that is already there
        rather than stacking a second one on top of it.
        """
        existing = getattr(self, 'original_window', None)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            self.refresh_original_window()
            return

        window = tk.Toplevel(self.root)
        window.title('The original frame, before any correction')
        self.original_window = window

        bar = tk.Frame(window)
        bar.pack(fill='x', padx=6, pady=4)
        self.original_note = tk.Label(
            bar, text='', font=('Helvetica', 9), anchor='w', fg='#003366',
            justify='left')
        self.original_note.pack(side='left', fill='x', expand=True)
        tk.Button(bar, text='Refresh', font=('Helvetica', 8),
                  command=self.refresh_original_window).pack(side='right',
                                                             padx=2)
        tk.Button(bar, text='Close', font=('Helvetica', 8),
                  command=window.destroy).pack(side='right', padx=2)

        self.original_label = tk.Label(window, text='(nothing corrected yet)',
                                       bg='#101010', fg='#888888',
                                       font=('Helvetica', 10))
        self.original_label.pack(fill='both', expand=True, padx=6, pady=(0, 6))
        self.refresh_original_window()

    def refresh_original_window(self):
        """Reload the untouched frame into that window, if it is open.

        Called by its own button and by every pass of the comparison
        refresh, so the window follows the camera rather than freezing on
        whatever was there when it opened.
        """
        window = getattr(self, 'original_window', None)
        if window is None or not window.winfo_exists():
            return
        if webcam_status is None:
            self.original_label.configure(image='', text='webcam_status.py '
                                                         'is not here')
            return
        # As wide as the screen will take, less a margin, and never so small
        # that it is no better than the tile.
        try:
            width = max(480, min(1400, self.root.winfo_screenwidth() - 160))
        except Exception:                                      # noqa: BLE001
            width = 900
        try:
            big = webcam_status.original_thumb(width)
        except Exception as problem:                           # noqa: BLE001
            self.original_label.configure(
                image='', text=f'could not read the original: {problem}',
                fg='#aa0000')
            return
        if not big['path']:
            self.original_label.configure(
                image='', text='(no frame has been corrected yet - the '
                               'original is written at the same time as the '
                               'corrected copy)', fg='#888888')
            return
        try:
            picture = tk.PhotoImage(file=big['path'])
        except Exception as problem:                           # noqa: BLE001
            self.original_label.configure(
                image='', text=f'(unreadable: {problem})', fg='#aa0000')
            return
        # Held on the instance or tkinter garbage collects it and the label
        # goes blank - the usual PhotoImage trap.
        self.original_image = picture
        self.original_label.configure(image=picture, text='')
        self.original_note.configure(
            text=f"straight from the camera, no stacking, no CLAHE, no brain "
                 f"G, nothing drawn on it - {big['width']}x{big['height']} "
                 f"shown, from {os.path.basename(big['source'])}")

    # ---- the error log ----

    def dump_errors(self):
        """Put the recent history of failures into the terminal pane.

        The pane only tails what happens from the moment the window opened,
        which is the right default - otherwise every window would open full
        of old news. This is for when the picture has been wrong for days and
        the question is what has been failing all along.
        """
        if webcam_errors is None:
            self.lib_summary.configure(
                text='webcam_errors.py is not next to this file', fg='#aa0000')
            return
        text = webcam_errors.tail(120)
        if not text.strip():
            self.terminal.write('--- no failures logged '
                                f'({webcam_errors.LOG_FILE}) ---\n')
            return
        self.terminal.write(f'--- recent failures from '
                            f'{webcam_errors.LOG_FILE} ---\n')
        self.terminal.write_errors(text)
        self.terminal.write('--- end of the log ---\n')

    def clear_errors(self):
        if webcam_errors is None:
            return
        done = webcam_errors.clear()
        self.terminal.error_offset = 0
        self.terminal.write('--- error log cleared ---\n' if done else
                            '--- there was no error log to clear ---\n')
        self.refresh_status_tile()

    # ---- night image enhancement ----

    def build_enhance(self):
        """The two switches for frame averaging, CLAHE and the ML auto
        correct.

        This is a different kind of thing from everything above it and is
        worth keeping visually apart. Everything else in this window sends
        values to the camera and so changes what gets recorded. This does not
        touch the camera at all: it reads the stills in the snapshot folder,
        averages the newest few to cancel the sensor noise, equalizes them
        tile by tile, lifts what is left, and writes the result into an
        `enhanced` folder beside them. The recording is unchanged - what this
        gives is a far more readable set of stills of the same scene.
        """
        tk.Label(self.right, text='--- Night Image Enhancement ---',
                 font=('Helvetica', 10, 'bold')).pack(pady=5)

        if not webcam_enhance:
            tk.Label(self.right, anchor='w', justify='left', font=('Helvetica', 8),
                     wraplength=310,
                     text='webcam_enhance.py was not found next to this file, so '
                          'there is no image correction. Everything else still works.',
                     ).pack(fill='x', padx=6)
            return

        tk.Label(
            self.right, anchor='w', justify='left', font=('Helvetica', 8), wraplength=310,
            text=('Works on the snapshots in the folder above, not on the camera: the '
                  f'newest {webcam_enhance.STACK_FRAMES} stills are averaged together, '
                  'which cancels most of the sensor noise, then CLAHE pulls detail out '
                  'of the shadows a tile at a time and the auto correct lifts what is '
                  'left. The corrected copies go in an "enhanced" folder beside the '
                  'originals. The recording itself is not changed by this, and the '
                  'light meter goes on measuring the camera\'s own stills.'),
        ).pack(fill='x', padx=6)

        boxes = tk.Frame(self.right)
        boxes.pack(fill='x', padx=6, pady=2)
        tk.Checkbutton(boxes, text='night image CLAHE and ML auto correct',
                       variable=self.enh_night, font=('Helvetica', 8), anchor='w',
                       command=self.on_enhance_toggle).pack(fill='x')
        tk.Checkbutton(boxes, text='24 hour image CLAHE and ML auto correct',
                       variable=self.enh_all_day, font=('Helvetica', 8), anchor='w',
                       command=self.on_enhance_toggle).pack(fill='x')
        tk.Checkbutton(boxes, text='find cars and people and enhance them',
                       variable=self.enh_detect, font=('Helvetica', 8), anchor='w',
                       command=self.on_enhance_toggle).pack(fill='x')
        tk.Checkbutton(boxes,
                       text='also sample the camera stream now and then '
                            '(optional)',
                       variable=self.enh_sample, font=('Helvetica', 8),
                       anchor='w',
                       command=self.on_enhance_toggle).pack(fill='x')

        tk.Label(self.right, anchor='w', justify='left', font=('Helvetica', 8),
                 wraplength=310, fg='#555555',
                 text=('The first box only runs it once the gain is past half its range, '
                       'which is the same test the sharpness uses for night - a floodlit '
                       'yard at midnight does not get it and an overcast dusk does. The '
                       'second runs it on every still around the clock and overrides the '
                       'first, and is on by default so that every mode of the day is '
                       'corrected.\n\n'
                       'The last box is the optional extra: as well as the stills, open '
                       'the camera every few minutes and correct a frame straight off '
                       'the stream. The stills cost nothing because the recorder writes '
                       'them anyway; this opens a device the recorder usually holds, so '
                       'it is off unless you want it.\n\n'
                       'The third looks for cars and people in the corrected still and '
                       'gives them a second, stronger lift of their own, saving a close '
                       'crop of each into an "detections" folder. It runs after the '
                       'correction on purpose: a detector sees nothing in a raw night '
                       'frame, and finds what is there once the frame has been lifted.'),
                 ).pack(fill='x', padx=6)

        tk.Label(self.right, text=self.enhancer_report(), anchor='w', justify='left',
                 font=('Helvetica', 8), wraplength=310, fg='#003366').pack(fill='x', padx=6)

        row = tk.Frame(self.right)
        row.pack(fill='x', padx=6, pady=2)
        tk.Button(row, text='Correct one now', font=('Helvetica', 8),
                  command=self.on_enhance_now).pack(side='left')

        self.enh_status = tk.Label(self.right, text=self.enhance_summary(), anchor='w',
                                   justify='left', font=('Helvetica', 8), wraplength=310)
        self.enh_status.pack(fill='x', padx=6)

    def enhancer_report(self):
        """What is installed for this, so a missing piece shows before it is
        needed rather than as a silent lack of improvement."""
        try:
            return webcam_enhance.Enhancer().report()
        except Exception as problem:                           # noqa: BLE001
            return f'enhancement: {problem}'

    def enhance_summary(self):
        if self.enh_all_day.get():
            line = 'Correcting every snapshot, around the clock.'
        elif self.enh_night.get():
            line = 'Correcting snapshots once the gain says it is dark.'
        else:
            return ('Image correction off - the snapshots are left as the camera '
                    'wrote them.')
        if self.enh_detect.get():
            line += ' Cars and people get a second lift and a crop of their own.'
        return line

    def on_enhance_toggle(self):
        """Write both switches out, so the scheduler sees the change too."""
        ok = webcam_enhance.write_settings(
            night=self.enh_night.get(), all_day=self.enh_all_day.get(),
            detect=self.enh_detect.get(),
            sample_stream=self.enh_sample.get())
        text = self.enhance_summary()
        if not ok:
            text += f' (could not write {webcam_enhance.CONF_FILE})'
        self.enh_status.configure(text=text)

    def on_enhance_now(self):
        """Correct the newest snapshot once, whatever the switches say.

        Pressing the button is itself the instruction, so this does not check
        them - it is how to see what the correction does to tonight's picture
        before leaving it running on a schedule.
        """
        if not self.meter:
            self.enh_status.configure(text='No light meter, so there is no snapshot folder')
            return
        frame = self.meter.find_snapshot()
        if not frame:
            note = f' - {self.meter.capture_note}' if self.meter.capture_note else ''
            self.enh_status.configure(text=f'No snapshot to correct{note}')
            return
        try:
            written, how = webcam_enhance.Enhancer().enhance_now(frame)
        except Exception as problem:                           # noqa: BLE001
            self.enh_status.configure(text=f'Could not correct it: {problem}')
            return
        if written:
            self.enh_status.configure(
                text=f'Wrote {os.path.basename(written)} in '
                     f'{os.path.dirname(written)} - {how}')
            self.refresh_brain_info()
        else:
            self.enh_status.configure(text=f'Could not correct it: {how}')

    def on_meter_toggle(self):
        # Clearing what the loop thinks it last wrote makes the next pass learn
        # the current settings afresh.
        self.meter.wrote = {}
        self.meter.means = []
        self.meter.last_dir = 0
        self.meter_status.configure(
            text='Automatic light adjustment on' if self.meter_on.get()
            else 'Automatic light adjustment off')

    def on_clear_trim(self):
        self.meter.apply_trim(0)
        self.meter.means = []
        self.meter.last_dir = 0
        self.meter.hold = METER_SETTLE
        self.meter_detail.configure(text='Trim cleared - back to the preset as written')

    def on_test(self):
        self.meter.capture_snapshot()
        frame = self.meter.find_snapshot()
        measured = self.meter.measure(frame)
        if measured is None:
            note = f' - {self.meter.capture_note}' if self.meter.capture_note else ''
            self.meter_detail.configure(text=f'No snapshot found{note}')
            return
        mean, hot, cold = measured
        self.meter_detail.configure(
            text=f'Measured {frame}: mean {mean:.0f}, white {hot * 100:.1f}%, '
                 f'black {cold * 100:.0f}%')

    def meter_tick(self):
        # pass_once only does work every 30 seconds and returns None in between,
        # so checking every 2 keeps the window responsive and costs nothing.
        if self.meter_on.get():
            line = self.meter.pass_once()
            if line:
                self.meter_status.configure(text=line)
        self.root.after(5000, self.meter_tick)


def main():
    root = tk.Tk()
    Window(root)
    root.mainloop()


if __name__ == '__main__':
    main()
