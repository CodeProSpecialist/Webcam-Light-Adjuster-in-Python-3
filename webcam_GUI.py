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

        self.start()
        self.root.after(self.POLL, self.drain)

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

    def write(self, line):
        # Follow the newest line only when the view is already at the bottom,
        # so scrolling back to read something does not get yanked away by the
        # next line arriving.
        at_bottom = self.text.yview()[1] > 0.999
        self.text.configure(state='normal')
        self.text.insert('end', line)
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
        self.enh_all_day = tk.BooleanVar(value=bool(saved.get(
            getattr(webcam_enhance, 'KEY_ALL_DAY', ''), False)))

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

        tk.Label(self.right, anchor='w', justify='left', font=('Helvetica', 8),
                 wraplength=310, fg='#555555',
                 text=('The first box only runs it once the gain is past half its range, '
                       'which is the same test the sharpness uses for night - a floodlit '
                       'yard at midnight does not get it and an overcast dusk does. The '
                       'second runs it on every still around the clock and overrides the '
                       'first.'),
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
            return 'Correcting every snapshot, around the clock.'
        if self.enh_night.get():
            return 'Correcting snapshots once the gain says it is dark.'
        return 'Image correction off - the snapshots are left as the camera wrote them.'

    def on_enhance_toggle(self):
        """Write both switches out, so the scheduler sees the change too."""
        ok = webcam_enhance.write_settings(night=self.enh_night.get(),
                                           all_day=self.enh_all_day.get())
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
