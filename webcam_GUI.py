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
import re
import subprocess
import sys
import tkinter as tk
from tkinter import ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from webcam_light_meter import LightMeter, METER_SETTLE, LOG_FILE
except ImportError as problem:                                    # noqa: BLE001
    print(f'automatic light adjustment unavailable: {problem}', file=sys.stderr)
    LightMeter = None
    METER_SETTLE = 2
    LOG_FILE = ''

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


class Window:
    def __init__(self, root):
        self.root = root
        self.camera = Camera(DEVICE)
        self.meter = LightMeter(device=DEVICE) if LightMeter else None
        self.meter_on = tk.BooleanVar(value=True)
        self.bool_vars = {}

        root.title('Webcam Settings')
        tk.Label(root, text='Webcam Controls', font=('Helvetica', 13, 'bold')).pack(pady=4)

        count = len(self.camera.controls)
        tk.Label(
            root,
            text=(f'{count} controls read from {DEVICE}' if count
                  else f'No controls readable from {DEVICE} - is the camera plugged in?'),
            font=('Helvetica', 9), fg='#003366',
        ).pack()

        self.build_scroll_area()
        self.build_controls()
        self.build_presets()
        self.build_meter()
        self.size_to_fit()
        if self.meter:
            root.after(6000, self.meter_tick)

    # ---- scrolling ----
    #
    # A camera with every control the kernel defines makes a taller column than
    # most screens have room for, so everything below the heading scrolls. A
    # Frame cannot scroll itself, so the frame holding the columns goes into a
    # canvas as a canvas window and the canvas scrolls that.

    def build_scroll_area(self):
        area = tk.Frame(self.root)
        self.canvas = tk.Canvas(area, highlightthickness=0)
        vbar = tk.Scrollbar(area, orient='vertical', command=self.canvas.yview)
        hbar = tk.Scrollbar(self.root, orient='horizontal', command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=vbar.set, xscrollcommand=hbar.set)
        hbar.pack(side='bottom', fill='x')
        self.canvas.pack(side='left', fill='both', expand=True)
        vbar.pack(side='right', fill='y')
        area.pack(fill='both', expand=True)

        self.cols = tk.Frame(self.canvas)
        self.cols_id = self.canvas.create_window(0, 0, window=self.cols, anchor='nw')
        self.cols.bind('<Configure>',
                       lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', self.on_canvas_resize)

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
        needed = self.cols.winfo_reqwidth()
        self.canvas.itemconfigure(self.cols_id, width=max(event.width, needed))

    def size_to_fit(self):
        """Open at the size the controls ask for, but never taller than the
        screen will take - which is the whole reason for the scrollbars."""
        self.root.update()
        want_w = min(self.cols.winfo_reqwidth(), int(self.root.winfo_screenwidth() * 0.98))
        want_h = min(self.cols.winfo_reqheight(), int(self.root.winfo_screenheight() * 0.88))
        self.canvas.configure(width=want_w, height=want_h,
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
        # pass_once only does work every 5 seconds and returns None in between,
        # so checking every 2 keeps the window responsive and costs nothing.
        if self.meter_on.get():
            line = self.meter.pass_once()
            if line:
                self.meter_status.configure(text=line)
        self.root.after(2000, self.meter_tick)


def main():
    root = tk.Tk()
    Window(root)
    root.mainloop()


if __name__ == '__main__':
    main()
