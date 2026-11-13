#!/usr/bin/env python3
"""The main window: live video first, with the settings page behind it.

This is the front door of the program now. Three pages across the top:

  Live      the video, large, as it comes off the camera. Start and stop
            recording, pick the device, see the disk.
  Archive   the index: search by date and time of day, play a segment back
            with a seek bar, and cut a clip out to keep.
  Settings  the window that used to be the whole program - every camera
            control, the presets, the light meter, the brain, the lock panel -
            unchanged, hosted here as a page.

The video is drawn from the frame the recorder writes ten times a second
rather than by opening the camera here. That is the point of the arrangement:
one process holds /dev/video0 and everything else reads what it publishes, so
the preview, the stills the brain trains on, and the recording never fight
over the device.

    python3 webcam_gui.py

The settings page still runs on its own - python3 webcam_settings_page.py opens
it in a window of its own, exactly as it did when it was the whole program.
"""

import os
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import webcam_bootstrap
except ImportError:
    pass
else:
    webcam_bootstrap.ensure('tkinter')

try:
    import webcam_install
except ImportError:
    pass
else:
    webcam_install.ensure_all(verbose=not os.environ.get('WEBCAM_QUIET_INSTALL'))

# OpenCV decodes the preview frames and plays the archive back. It is one of
# the two that webcam_install fetches, so by here it is almost always present;
# when it is not, say which program is missing rather than letting an import
# traceback be the whole explanation.
try:
    import cv2                                                    # noqa: E402
except ImportError as problem:
    sys.exit(f'OpenCV is needed for the video window and is not installed '
             f'({problem}).\nTry:  pip3 install --break-system-packages '
             'opencv-python-headless\nThe settings page alone still runs:  '
             'python3 webcam_settings_page.py')

import webcam_record                                              # noqa: E402
import webcam_archive                                             # noqa: E402

try:
    from webcam_errors import log_error
except Exception:                                                 # noqa: BLE001
    def log_error(text):
        print(text, file=sys.stderr)


# How the archive plays back. Each entry is a label, the rate footage runs at
# in seconds of video per second of clock, and how many frames a second to draw
# while doing it.
#
# The slow end is read as "frames per second": a 30fps recording drawn at 5
# frames a second is six times slow motion, which is the speed to watch
# somebody's hands at. The fast end is read as "minutes per second": at 1
# min/sec an hour of a still driveway goes past in a minute, which is the
# speed to find the one moment something moved at. Drawing stays at 15 a
# second there - 1800 frames a second is not something a screen or an eye has
# any use for, so frames are skipped rather than drawn.
PLAYBACK_SPEEDS = (
    ('5 fps - slow motion',       5.0 / 30,  5),
    ('10 fps - slow motion',     10.0 / 30, 10),
    ('15 fps - half speed',      15.0 / 30, 15),
    ('20 fps',                   20.0 / 30, 20),
    ('30 fps - normal speed',          1.0, 30),
    ('Fast forward - 1 min/sec',      60.0, 15),
    ('Fast forward - 2 min/sec',     120.0, 15),
)
PLAYBACK_DEFAULT = 4      # 30 fps, normal speed

PREVIEW_MS = 100          # the live pane redraws ten times a second
STATUS_MS = 2000
VIEW_W, VIEW_H = 960, 540  # the video pane: 720p at three quarters, which
                           # leaves room beside it on a 1080p screen


def to_photo(frame, max_w=VIEW_W, max_h=VIEW_H):
    """A BGR frame as something Tk will draw, scaled to fit the pane.

    PNG rather than PIL: Tk 8.6 reads PNG bytes straight into a PhotoImage, so
    the whole Pillow dependency is avoided for what is only ever a redraw.
    """
    if frame is None:
        return None
    height, width = frame.shape[:2]
    if width <= 0 or height <= 0:
        return None
    scale = min(max_w / float(width), max_h / float(height), 1.0)
    if scale < 1.0:
        frame = cv2.resize(frame, (int(width * scale), int(height * scale)),
                           interpolation=cv2.INTER_AREA)
    ok, buffer = cv2.imencode('.png', frame,
                              [cv2.IMWRITE_PNG_COMPRESSION, 1])
    if not ok:
        return None
    return tk.PhotoImage(data=buffer.tobytes())


def hhmmss(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds // 3600:d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}'


# ---- the live page ----

class LivePage(tk.Frame):
    """The video, up to four cameras at once.

    The panes are laid out by how many cameras there are rather than always
    being a 2x2 grid with holes in it: one camera gets the whole width, two
    sit side by side, three or four fill the quarters. A camera that is
    enabled but not producing frames keeps its pane and says what is wrong in
    it, which is the behaviour that matters at three in the morning - a
    missing pane looks like a layout bug, a pane saying "device busy" tells
    you what to go and fix.
    """

    # How large each pane is for a given number of cameras, and how the grid
    # is shaped. Four 720p previews decoded ten times a second is real work,
    # so the panes get smaller as they get more numerous - the archive keeps
    # the full 1280x720 either way, this is only what is drawn.
    LAYOUTS = {
        1: (1, 1, 960, 540),
        2: (2, 1, 640, 360),
        3: (2, 2, 480, 270),
        4: (2, 2, 480, 270),
    }

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.panes = []
        self.frames_drawn = 0
        self.last_count_at = time.time()
        self.shown_fps = 0.0

        settings = webcam_record.read_settings()
        self.known = webcam_record.list_devices()
        cameras = settings['cameras']

        self.grid_area = tk.Frame(self)
        self.grid_area.pack(side='top', padx=6, pady=6)

        bar = tk.Frame(self)
        bar.pack(side='top', fill='x', padx=6)
        self.state_dot = tk.Label(bar, text=' ', bg='#555555', width=2)
        self.state_dot.pack(side='left', padx=(0, 6))
        self.state_text = tk.Label(bar, text='starting...',
                                   font=('Helvetica', 11, 'bold'))
        self.state_text.pack(side='left')
        self.rate_text = tk.Label(bar, text='', fg='#445566',
                                  font=('Helvetica', 9))
        self.rate_text.pack(side='right')

        buttons = tk.Frame(self)
        buttons.pack(side='top', fill='x', padx=6, pady=4)
        tk.Button(buttons, text='Start Recording', width=16, bg='#cc3333',
                  fg='white', command=self.on_record).pack(side='left', padx=2)
        tk.Button(buttons, text='Free space now', width=15,
                  command=self.on_janitor).pack(side='left', padx=12)
        tk.Button(buttons, text='Open archive folder', width=18,
                  command=self.on_open_folder).pack(side='left', padx=2)

        # ---- the cameras ----
        #
        # Four slots, each a tick and a device. The device picker is here
        # rather than buried in the settings page because it is the one thing
        # that has to be right before anything else works, and because a USB
        # camera comes back as a different /dev/video number often enough to
        # matter.
        cams = tk.LabelFrame(self, text=f'Cameras (up to '
                                        f'{webcam_record.MAX_CAMERAS})',
                             padx=6, pady=4)
        cams.pack(side='top', fill='x', padx=6, pady=4)
        labels = [label for _path, label in self.known]
        self.slots = []
        for number in range(1, webcam_record.MAX_CAMERAS + 1):
            on = tk.BooleanVar(value=number <= len(cameras))
            device = tk.StringVar(
                value=cameras[number - 1] if number <= len(cameras) else '')
            tick = tk.Checkbutton(cams, text=f'Camera {number}', variable=on,
                                  anchor='w', width=10)
            tick.grid(row=number - 1, column=0, sticky='w')
            if number == 1:
                # Camera 1 cannot be turned off: it is what every preset, the
                # light meter and the brain mean by "the camera".
                tick.configure(state='disabled')
                on.set(True)
            picker = ttk.Combobox(cams, textvariable=device, width=46,
                                  values=labels, state='readonly')
            picker.grid(row=number - 1, column=1, sticky='w', padx=4, pady=1)
            for index, (path, label) in enumerate(self.known):
                if path == device.get():
                    picker.current(index)
                    break
            self.slots.append({'on': on, 'device': device, 'box': picker})
        tk.Button(cams, text='Rescan devices', command=self.on_rescan_devices
                  ).grid(row=0, column=2, padx=6)
        tk.Button(cams, text='Apply cameras', width=14, command=self.on_apply
                  ).grid(row=1, column=2, padx=6)
        tk.Label(cams, fg='#445566', font=('Helvetica', 9), justify='left',
                 wraplength=420,
                 text=('Each camera needs its own device - two recorders on '
                       'one device is the "busy" failure. Every camera '
                       'records at the same size and rate, into its own '
                       'cam1..cam4 folder.')
                 ).grid(row=2, column=2, rowspan=2, sticky='w', padx=6)

        # ---- what gets recorded ----
        box = tk.LabelFrame(self, text='Recording', padx=6, pady=4)
        box.pack(side='top', fill='x', padx=6, pady=4)

        tk.Label(box, text='Size').grid(row=0, column=0, sticky='e')
        self.size_var = tk.StringVar(
            value=f"{settings['width']}x{settings['height']}")
        ttk.Combobox(box, textvariable=self.size_var, width=12,
                     values=['1280x720', '1920x1080', '800x600', '640x480'],
                     state='readonly').grid(row=0, column=1, sticky='w', padx=4)

        tk.Label(box, text='Frames/sec').grid(row=0, column=2, sticky='e')
        self.fps_var = tk.IntVar(value=int(settings['fps']))
        tk.Spinbox(box, from_=1, to=60, width=5, textvariable=self.fps_var
                   ).grid(row=0, column=3, sticky='w', padx=4)

        tk.Label(box, text='Segment (min)').grid(row=0, column=4, sticky='e')
        self.segment_var = tk.IntVar(
            value=max(1, int(settings['segment_seconds']) // 60))
        tk.Spinbox(box, from_=1, to=60, width=5, textvariable=self.segment_var
                   ).grid(row=0, column=5, sticky='w', padx=4)

        tk.Label(box, text='Loop record at').grid(row=1, column=0, sticky='e')
        self.cap_var = tk.StringVar()
        self.cap_box = ttk.Combobox(
            box, textvariable=self.cap_var, width=8, state='readonly',
            values=[f'{int(gb)} GB' for gb in webcam_record.ARCHIVE_CAPS])
        self.cap_box.grid(row=1, column=1, sticky='w', padx=4)
        self.cap_var.set(f"{int(float(settings.get('max_archive_gb', 250)))} GB")

        tk.Label(box, text='Keep free (GB)').grid(row=1, column=2, sticky='e')
        self.free_var = tk.DoubleVar(
            value=float(settings.get('keep_free_gb', 25.0)))
        tk.Spinbox(box, from_=1, to=2000, increment=1, width=7,
                   textvariable=self.free_var).grid(row=1, column=3,
                                                    sticky='w', padx=4)

        tk.Label(box, text='Video').grid(row=1, column=4, sticky='e')
        self.encoder_var = tk.StringVar()
        ttk.Combobox(box, textvariable=self.encoder_var, width=30,
                     state='readonly',
                     values=['copy  (no CPU, ~4x disk)  - default',
                             'h264  (small files, a core per camera)']
                     ).grid(row=1, column=5, sticky='w', padx=4)
        self.encoder_var.set('h264  (small files, a core per camera)'
                             if settings.get('encoder') != 'copy'
                             else 'copy  (no CPU, ~4x disk)  - default')
        tk.Button(box, text='Apply', width=10, command=self.on_apply
                  ).grid(row=1, column=6, padx=6)

        tk.Label(
            self,
            text=('Only one program can stream a camera at a time, so nothing '
                  'else may be capturing these devices while this runs. If a '
                  'pane stays dark and says the device is busy, that is what '
                  'it means. Camera controls and presets keep working while '
                  'recording.'),
            fg='#664400', wraplength=900, justify='left', font=('Helvetica', 9),
        ).pack(side='top', anchor='w', padx=8, pady=(2, 0))

        self.disk = tk.Label(self, text='', fg='#003366', anchor='w',
                             font=('Helvetica', 10))
        self.disk.pack(side='top', fill='x', padx=8, pady=2)

        self.build_panes(len(cameras))
        self.after(PREVIEW_MS, self.draw_tick)
        self.after(600, self.fit_window)
        self.after(500, self.status_tick)

    # -- the panes --

    def fit_window(self):
        """Make the window the size the panes actually need.

        One camera is a one-pane window, not a four-pane window with three
        holes in it: the grid only ever builds as many panes as there are
        cameras, and this makes the window around them follow, so adding or
        removing a camera resizes the window rather than leaving empty space
        or cutting a pane off.
        """
        try:
            root = self.winfo_toplevel()
            root.update_idletasks()
            width = max(self.winfo_reqwidth(), 1020)
            height = self.winfo_reqheight()
            width = min(width, int(root.winfo_screenwidth() * 0.98))
            height = min(height, int(root.winfo_screenheight() * 0.94))
            root.minsize(min(width, 1020), min(height, 700))
            root.geometry(f'{width}x{height}')
        except tk.TclError:
            pass

    def build_panes(self, count):
        """Lay out one pane per camera. Rebuilt when the camera count changes,
        which is the only time the geometry needs to move."""
        for child in self.grid_area.winfo_children():
            child.destroy()
        self.panes = []
        count = max(1, min(webcam_record.MAX_CAMERAS, int(count)))
        across, _down, width, height = self.LAYOUTS[count]
        for number in range(1, count + 1):
            holder = tk.Frame(self.grid_area, bd=1, relief='sunken')
            holder.grid(row=(number - 1) // across,
                        column=(number - 1) % across, padx=3, pady=3)
            head = tk.Frame(holder)
            head.pack(side='top', fill='x')
            dot = tk.Label(head, text=' ', bg='#555555', width=2)
            dot.pack(side='left')
            title = tk.Label(head, text=f'Camera {number}',
                             font=('Helvetica', 9, 'bold'))
            title.pack(side='left', padx=4)
            canvas = tk.Canvas(holder, bg='#101014', width=width,
                               height=height, highlightthickness=0)
            canvas.pack(side='top')
            item = canvas.create_image(width // 2, height // 2,
                                       anchor='center')
            text = canvas.create_text(width // 2, height // 2, fill='#99aabb',
                                      width=width - 40, justify='center',
                                      font=('Helvetica', 10),
                                      text='waiting for the first frame...')
            # The REC mark, top left, over the picture: the one piece of
            # state worth reading from across a room, and the convention
            # every camera has used for forty years. Created hidden and
            # shown only while this camera is actually recording, so a dark
            # pane with a red dot on it can never happen - that would be the
            # single most misleading thing this window could do.
            rec_dot = canvas.create_oval(12, 12, 26, 26, fill='#dd1111',
                                         outline='#ffffff', width=1,
                                         state='hidden')
            rec_text = canvas.create_text(34, 19, anchor='w', text='REC',
                                          fill='#ffffff',
                                          font=('Helvetica', 11, 'bold'),
                                          state='hidden')
            self.panes.append({
                'canvas': canvas, 'item': item, 'text': text, 'dot': dot,
                'title': title, 'photo': None, 'mtime': 0.0,
                'width': width, 'height': height, 'index': number,
                'rec_dot': rec_dot, 'rec_text': rec_text,
            })

    # -- settings --

    def collect(self):
        """Whatever is ticked and typed, as recorder settings."""
        cameras = []
        for slot in self.slots:
            if not slot['on'].get():
                continue
            label = slot['device'].get().strip()
            device = label.split(' ')[0] if label else ''
            if device and device not in cameras:
                cameras.append(device)
        if not cameras:
            cameras = [webcam_record.DEVICE]
        try:
            width, height = (int(part) for part in
                             self.size_var.get().lower().split('x'))
        except (ValueError, AttributeError):
            width, height = 1280, 720
        return {
            'cameras': cameras,
            'device': cameras[0],
            'width': width,
            'height': height,
            'fps': int(self.fps_var.get() or 30),
            'segment_seconds': max(60, int(self.segment_var.get() or 5) * 60),
            'keep_free_gb': float(self.free_var.get() or 25.0),
            'max_archive_gb': float(self.cap_var.get().split()[0] or 250),
            'encoder': 'copy' if self.encoder_var.get().startswith('copy')
                       else 'h264',
        }

    def on_apply(self):
        wanted = self.collect()
        webcam_record.write_settings(**wanted)
        if len(wanted['cameras']) != len(self.panes):
            self.build_panes(len(wanted['cameras']))
            self.after(300, self.fit_window)
        if self.app.recorder.is_running():
            self.app.recorder.start(record=self.app.recorder.recording)

    def on_record(self):
        self.on_apply()
        self.app.recorder.start(record=True)

    def on_janitor(self):
        def work():
            removed, freed = webcam_record.free_space()
            self.after(0, lambda: messagebox.showinfo(
                'Free space',
                f'{removed} segment(s) recycled, {freed:.1f} GB freed.'
                if removed else
                'Nothing needed recycling - the archive is inside both the '
                'loop cap and the free-space floor.'))
        threading.Thread(target=work, daemon=True).start()

    # The archive folder and the state folder have to agree with each other.
    # ~/.cache/webcam holds the recorder's pid, its status json and the
    # preview frames, and those still name segments that the archive folder
    # used to hold. Delete an archive by hand and the recorder keeps writing
    # against state that no longer matches the disk, which shows up as a
    # camera that will not record. So opening the folder also says so, and
    # offers the one repair that fixes it.
    def on_open_folder(self):
        folder = webcam_record.read_settings()['archive_dir']
        try:
            import subprocess
            subprocess.Popen(['xdg-open', folder])
        except OSError as problem:
            messagebox.showinfo('Archive folder', f'{folder}\n\n{problem}')
        self.show_archive_notice(folder)

    def show_archive_notice(self, folder):
        """The notice window, with the repair button under it."""
        window = tk.Toplevel(self)
        window.title('Archive folder')
        window.transient(self.winfo_toplevel())
        frame = tk.Frame(window, padx=12, pady=12)
        frame.pack(fill='both', expand=True)

        tk.Label(frame, text=folder, font=('Helvetica', 9, 'bold'),
                 anchor='w', justify='left').pack(fill='x')
        tk.Label(
            frame,
            text='If the archive file is deleted, then the files in '
                 '~/.cache/webcam related to that archive need to also be '
                 'deleted and the program needs to be restarted to begin '
                 'recording onto a new archive.',
            wraplength=430, justify='left', anchor='w',
            pady=8).pack(fill='x')

        tk.Button(
            frame,
            text='Delete Archive files to fix camera not recording errors',
            command=lambda: self.on_clear_state(window)).pack(fill='x', pady=4)
        tk.Button(frame, text='Close', width=10,
                  command=window.destroy).pack(pady=2)

    def on_clear_state(self, window=None):
        """Empty ~/.cache/webcam.

        Nothing in there is recorded video - it is the pid file, the status
        json and the preview jpgs, all of which the recorder writes again on
        its next start. The recorder is stopped first so that it is not
        holding a pid file that is about to disappear.
        """
        if not messagebox.askyesno(
                'Delete archive state files',
                f'Delete the recorder state files in '
                f'{webcam_record.STATE_DIR}?\n\n'
                'No recorded video is touched. Recording stops, and the '
                'program needs restarting afterwards to begin a new '
                'archive.'):
            return

        try:
            self.app.recorder.stop()
        except Exception:                    # already stopped is fine
            pass

        removed, failed = 0, []
        state = webcam_record.STATE_DIR
        try:
            names = sorted(os.listdir(state))
        except OSError as problem:
            messagebox.showinfo('Delete archive state files',
                                f'{state}\n\n{problem}')
            return
        for name in names:
            path = os.path.join(state, name)
            try:
                if os.path.isdir(path) and not os.path.islink(path):
                    import shutil
                    shutil.rmtree(path)
                else:
                    os.remove(path)
                removed += 1
            except OSError as problem:
                failed.append(f'{name}: {problem}')

        self.state_text.config(text='state cleared - restart the program')
        self.state_dot.config(bg='#555555')
        if window is not None:
            window.destroy()

        note = (f'{removed} item(s) deleted from {state}.\n\n'
                'Now close and restart the program to begin recording onto '
                'a new archive.')
        if failed:
            note += '\n\nCould not delete:\n' + '\n'.join(failed)
        messagebox.showinfo('Delete archive state files', note)

    def on_rescan_devices(self):
        self.known = webcam_record.list_devices()
        labels = [label for _p, label in self.known]
        for slot in self.slots:
            slot['box'].configure(values=labels)

    # -- drawing --

    def draw_tick(self):
        """Redraw each pane from the frame its camera published, if it is a
        new one.

        A half-written frame decodes to None and is simply skipped rather than
        reported: the next one is 100ms away, and the recorder writes
        atomically where ffmpeg supports it.
        """
        for pane in self.panes:
            try:
                path = webcam_record.preview_file(pane['index'])
                mtime = os.path.getmtime(path)
                if mtime <= pane['mtime']:
                    continue
                frame = cv2.imread(path, cv2.IMREAD_COLOR)
                photo = to_photo(frame, pane['width'], pane['height'])
                if photo is None:
                    continue
                pane['mtime'] = mtime
                pane['photo'] = photo       # Tk drops an unreferenced one
                pane['canvas'].itemconfigure(pane['item'], image=photo)
                pane['canvas'].itemconfigure(pane['text'], text='')
                self.frames_drawn += 1
            except (OSError, cv2.error):
                continue
        now = time.time()
        if now - self.last_count_at >= 2:
            self.shown_fps = self.frames_drawn / (now - self.last_count_at)
            self.frames_drawn = 0
            self.last_count_at = now
        self.after(PREVIEW_MS, self.draw_tick)

    def status_tick(self):
        status = self.app.recorder.status()
        rows = {row['index']: row for row in status.get('per_camera', [])}

        for pane in self.panes:
            row = rows.get(pane['index'], {})
            device = row.get('device', '')
            stale = time.time() - pane['mtime'] > 5 if pane['mtime'] else True
            # Recording AND a picture arriving: either alone is not recording.
            marked = 'normal' if (row.get('recording') and not stale) else 'hidden'
            pane['canvas'].itemconfigure(pane['rec_dot'], state=marked)
            pane['canvas'].itemconfigure(pane['rec_text'], state=marked)
            if row.get('recording') and not stale:
                pane['dot'].config(bg='#cc2222')
            elif row.get('running') and not stale:
                pane['dot'].config(bg='#2277cc')
            else:
                pane['dot'].config(bg='#555555')
            pane['title'].config(
                text=f"Camera {pane['index']}"
                     + (f'  {device}' if device else '')
                     + (f"  {row['camera_gb']:.1f} GB"
                        if row.get('camera_gb') else ''))
            if stale and row.get('error'):
                pane['canvas'].itemconfigure(pane['item'], image='')
                pane['canvas'].itemconfigure(pane['text'],
                                             text=row['error'][:300])
            elif stale and row.get('retry_in'):
                pane['canvas'].itemconfigure(
                    pane['text'],
                    text=f"camera lost - retrying in {row['retry_in']}s")

        if status['recording']:
            self.state_dot.config(bg='#cc2222')
            self.state_text.config(
                text=f"RECORDING  {status['live']} of {status['cameras']} "
                     f"camera(s)  {status['size']} @ {status['fps']}fps")
        elif status['running']:
            self.state_dot.config(bg='#2277cc')
            self.state_text.config(text='preview only - nothing is being kept')
        else:
            self.state_dot.config(bg='#555555')
            self.state_text.config(text='stopped')
        self.rate_text.config(text=f'drawing {self.shown_fps:.0f} fps total')

        target = status['keep_free_gb']
        cap = status.get('max_archive_gb', 250.0)
        free = status['free_gb']
        held = status['archive_gb']
        self.disk.config(
            text=(f"archive {held:.1f} of {cap:.0f} GB "
                  f"({held / cap * 100 if cap else 0:.0f}% of the loop) in "
                  f"{status['segments']} segments   -   {free:.1f} GB free on "
                  f"disk, floor {target:.0f} GB"
                  + (f"   -   oldest kept: {status['oldest']}"
                     if status['oldest'] else '')),
            fg='#aa0000' if free < target * 0.6 else '#003366')
        self.after(STATUS_MS, self.status_tick)


# ---- the archive page ----

class ArchivePage(tk.Frame):
    """Search the index, play a segment back, cut a clip out.

    Playback is deliberately its own capture on the file rather than anything
    to do with the camera, so an archive can be watched while the recorder
    keeps recording.
    """

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.photo = None
        self.capture = None
        self.playing = False
        self.rows = []
        self.current = None
        self.frame_count = 0
        self.video_fps = 30.0
        self.seeking = False

        today = time.strftime('%Y-%m-%d')

        # -- the search row --
        finder = tk.LabelFrame(self, text='Archive index search', padx=6, pady=4)
        finder.pack(side='top', fill='x', padx=6, pady=4)

        tk.Label(finder, text='Date from').grid(row=0, column=0, sticky='e')
        self.day_from = tk.Entry(finder, width=12)
        self.day_from.insert(0, today)
        self.day_from.grid(row=0, column=1, padx=3)
        tk.Label(finder, text='to').grid(row=0, column=2, sticky='e')
        self.day_to = tk.Entry(finder, width=12)
        self.day_to.insert(0, today)
        self.day_to.grid(row=0, column=3, padx=3)

        tk.Label(finder, text='Time from').grid(row=0, column=4, sticky='e')
        self.time_from = tk.Entry(finder, width=8)
        self.time_from.grid(row=0, column=5, padx=3)
        tk.Label(finder, text='to').grid(row=0, column=6, sticky='e')
        self.time_to = tk.Entry(finder, width=8)
        self.time_to.grid(row=0, column=7, padx=3)

        tk.Label(finder, text='Camera').grid(row=0, column=10, sticky='e')
        self.camera_var = tk.StringVar(value='All cameras')
        ttk.Combobox(finder, textvariable=self.camera_var, width=12,
                     state='readonly',
                     values=['All cameras'] +
                            [f'Camera {n}' for n in
                             range(1, webcam_record.MAX_CAMERAS + 1)]
                     ).grid(row=0, column=11, padx=4)

        tk.Label(finder, text='Text / note').grid(row=1, column=0, sticky='e')
        self.text_box = tk.Entry(finder, width=26)
        self.text_box.grid(row=1, column=1, columnspan=3, sticky='w', padx=3)
        tk.Label(finder, text='At least (sec)').grid(row=1, column=4, sticky='e')
        self.min_secs = tk.Entry(finder, width=8)
        self.min_secs.grid(row=1, column=5, padx=3)

        tk.Button(finder, text='Search', width=10, command=self.on_search
                  ).grid(row=0, column=8, padx=6)
        tk.Button(finder, text='Everything', width=10, command=self.on_all
                  ).grid(row=1, column=8, padx=6)
        tk.Button(finder, text='Rescan disk', width=11, command=self.on_rescan
                  ).grid(row=0, column=9, padx=3)
        tk.Button(finder, text='Clear filters', width=11,
                  command=self.on_clear).grid(row=1, column=9, padx=3)

        # The spans worth one click. "The last ten minutes" is how an archive
        # actually gets searched - something happened, and it happened
        # recently - and typing two dates and two times to ask that is three
        # clicks too many. Each button fills the four boxes above as well as
        # searching, so the span can then be nudged by hand.
        spans = tk.LabelFrame(self, text='Back from now', padx=6, pady=3)
        spans.pack(side='top', fill='x', padx=6)
        for label, seconds in (('1 min', 60), ('5 min', 300),
                               ('10 min', 600), ('30 min', 1800),
                               ('1 hour', 3600), ('5 hours', 5 * 3600),
                               ('12 hours', 12 * 3600),
                               ('24 hours', 24 * 3600)):
            tk.Button(spans, text=label, width=8,
                      command=lambda s=seconds, t=label: self.on_span(s, t)
                      ).pack(side='left', padx=2)

        self.summary = tk.Label(self, text='', anchor='w', fg='#003366',
                                font=('Helvetica', 10))
        self.summary.pack(side='top', fill='x', padx=8)
        self.span_label = tk.Label(self, text='', anchor='w', fg='#445566',
                                   font=('Helvetica', 9))
        self.span_label.pack(side='top', fill='x', padx=8)
        self.found = tk.Label(self, text='', anchor='w', fg='#003366',
                              font=('Helvetica', 9))
        self.found.pack(side='top', fill='x', padx=8)

        # -- results beside the player --
        split = tk.Frame(self)
        split.pack(side='top', fill='both', expand=True, padx=6, pady=4)

        left = tk.Frame(split)
        left.pack(side='left', fill='both', expand=False)
        columns = ('cam', 'when', 'length', 'size', 'note')
        self.tree = ttk.Treeview(left, columns=columns, show='headings',
                                 height=18, selectmode='browse')
        for name, title, width in (('cam', 'Cam', 40),
                                   ('when', 'Date and time', 150),
                                   ('length', 'Length', 62),
                                   ('size', 'Size', 72),
                                   ('note', 'Note', 96)):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, anchor='w')
        bar = tk.Scrollbar(left, orient='vertical', command=self.tree.yview,
                           width=16)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side='left', fill='both', expand=True)
        bar.pack(side='right', fill='y')
        self.tree.bind('<<TreeviewSelect>>', self.on_pick)
        self.tree.bind('<Double-1>', lambda e: self.on_play())

        right = tk.Frame(split)
        right.pack(side='left', fill='both', expand=True, padx=(8, 0))
        self.video = tk.Canvas(right, bg='#101014', width=640, height=360,
                               highlightthickness=0)
        self.video.pack(side='top')
        self.video_item = self.video.create_image(320, 180, anchor='center')
        self.video_text = self.video.create_text(
            320, 180, fill='#99aabb', text='pick a segment on the left')

        self.scrub = tk.Scale(right, from_=0, to=100, orient='horizontal',
                              showvalue=False, command=self.on_scrub)
        self.scrub.pack(side='top', fill='x', pady=2)
        self.scrub.bind('<Button-1>', lambda e: setattr(self, 'seeking', True))
        self.scrub.bind('<ButtonRelease-1>',
                        lambda e: setattr(self, 'seeking', False))

        controls = tk.Frame(right)
        controls.pack(side='top', fill='x')
        tk.Button(controls, text='Play', width=8, command=self.on_play
                  ).pack(side='left', padx=2)
        tk.Button(controls, text='Pause', width=8, command=self.on_pause
                  ).pack(side='left', padx=2)
        tk.Button(controls, text='-10s', width=6,
                  command=lambda: self.nudge(-10)).pack(side='left', padx=2)
        tk.Button(controls, text='+10s', width=6,
                  command=lambda: self.nudge(10)).pack(side='left', padx=2)
        tk.Label(controls, text='Speed').pack(side='left', padx=(12, 2))
        self.speed_var = tk.StringVar()
        self.speed_box = ttk.Combobox(
            controls, textvariable=self.speed_var, width=24, state='readonly',
            values=[label for label, _rate, _fps in PLAYBACK_SPEEDS])
        self.speed_box.current(PLAYBACK_DEFAULT)
        self.speed_box.pack(side='left')
        self.speed_box.bind('<<ComboboxSelected>>', self.on_speed)

        self.at_label = tk.Label(controls, text='', fg='#445566')
        self.at_label.pack(side='right')

        # -- cutting a clip out --
        cutter = tk.LabelFrame(right, text='Download a clip of the archive',
                               padx=6, pady=4)
        cutter.pack(side='top', fill='x', pady=6)
        tk.Label(cutter, text='From').grid(row=0, column=0, sticky='e')
        self.clip_from = tk.Entry(cutter, width=20)
        self.clip_from.grid(row=0, column=1, padx=3)
        tk.Label(cutter, text='To').grid(row=0, column=2, sticky='e')
        self.clip_to = tk.Entry(cutter, width=20)
        self.clip_to.grid(row=0, column=3, padx=3)
        tk.Button(cutter, text='Use this segment', command=self.on_clip_fill
                  ).grid(row=1, column=0, pady=3, sticky='w')
        tk.Button(cutter, text='Mark start here', command=self.on_mark_start
                  ).grid(row=1, column=1, pady=3)
        tk.Button(cutter, text='Mark end here', command=self.on_mark_end
                  ).grid(row=1, column=2, pady=3)
        tk.Button(cutter, text='Save clip...', width=12, bg='#225599',
                  fg='white', command=self.on_export
                  ).grid(row=0, column=4, rowspan=2, padx=8)
        tk.Label(cutter, text='Export as').grid(row=2, column=0, sticky='e')
        self.format_var = tk.StringVar()
        self.format_box = ttk.Combobox(
            cutter, textvariable=self.format_var, width=44, state='readonly',
            values=[text for _k, _e, text in webcam_archive.EXPORT_FORMATS])
        self.format_box.current(0)
        self.format_box.grid(row=2, column=1, columnspan=4, sticky='w', pady=2)
        tk.Label(cutter,
                 text=('A clip may span as many segments as it needs - the '
                       'pieces are trimmed and joined without re-encoding, so '
                       'it is quick and loses nothing. Times are '
                       '"YYYY-MM-DD HH:MM:SS".'),
                 wraplength=440, justify='left', fg='#445566',
                 font=('Helvetica', 9)).grid(row=3, column=0, columnspan=5,
                                             sticky='w', pady=(2, 0))
        self.clip_cam = tk.Label(cutter, text='', fg='#445566',
                                 font=('Helvetica', 9))
        self.clip_cam.grid(row=2, column=5, sticky='w', padx=4)
        self.clip_state = tk.Label(cutter, text='', fg='#003366',
                                   wraplength=440, justify='left',
                                   font=('Helvetica', 9))
        self.clip_state.grid(row=4, column=0, columnspan=5, sticky='w')

        self.after(800, self.first_load)

    # -- index --

    def first_load(self):
        self.on_rescan(quiet=True)

    def on_rescan(self, quiet=False):
        def work():
            try:
                result = webcam_archive.scan()
            except Exception as problem:                          # noqa: BLE001
                log_error(f'archive scan: {problem}')
                result = {}
            self.after(0, lambda: self.after_scan(result, quiet))
        threading.Thread(target=work, daemon=True).start()
        self.summary.config(text='scanning the archive folder...')

    def after_scan(self, result, quiet):
        info = webcam_archive.stats()
        per_cam = '  '.join(f'cam{number}: {count}' for number, count
                            in sorted(info.get('cameras', {}).items()))
        self.summary.config(
            text=(f"index holds {info['segments']} segments, "
                  f"{info['hours']} hours, {info['gigabytes']} GB across "
                  f"{info['days']} day(s)"
                  + (f'   -   {per_cam}' if per_cam else '')
                  + (f"   -   {info['first']} to {info['last']}"
                     if info['first'] else '')))
        if not quiet and result:
            self.found.config(
                text=f"rescan: {result.get('added', 0)} new, "
                     f"{result.get('updated', 0)} updated, "
                     f"{result.get('removed', 0)} gone from disk")
        self.on_span(3600, '1 hour')

    def picked_camera(self):
        """The camera filter as a number, or None for all of them."""
        text = self.camera_var.get()
        return int(text.split()[-1]) if text.startswith('Camera') else None

    def on_search(self):
        rows = webcam_archive.search(
            self.day_from.get().strip() or None,
            self.day_to.get().strip() or None,
            webcam_archive._hhmm(self.time_from.get().strip()),
            webcam_archive._hhmm(self.time_to.get().strip()),
            self.text_box.get().strip(),
            float(self.min_secs.get() or 0) if self.min_secs.get().strip()
            else 0,
            camera=self.picked_camera())
        self.show_rows(rows)

    def on_span(self, seconds, label=''):
        """Everything recorded in the last N seconds.

        Filtered on the real start and end times rather than on the date and
        time-of-day columns, because a span of hours crosses midnight and a
        time-of-day filter cannot say "the last five hours" across one. A
        segment counts when any part of it falls inside the span, so the
        segment that was being written when the thing happened is included
        rather than missed by a few seconds.
        """
        now = time.time()
        cutoff = now - seconds
        rows = [row for row in webcam_archive.search(
                    limit=20000, camera=self.picked_camera())
                if row['ended'] >= cutoff and row['started'] <= now]
        self.span_label.config(
            text=f"showing the last {label or hhmmss(seconds)}:  "
                 + time.strftime('%a %H:%M:%S', time.localtime(cutoff))
                 + '  to  ' + time.strftime('%H:%M:%S', time.localtime(now)))
        # Fill the boxes too, so the span can be adjusted by hand from here.
        self.day_from.delete(0, 'end')
        self.day_from.insert(0, time.strftime('%Y-%m-%d',
                                              time.localtime(cutoff)))
        self.day_to.delete(0, 'end')
        self.day_to.insert(0, time.strftime('%Y-%m-%d', time.localtime(now)))
        self.time_from.delete(0, 'end')
        self.time_from.insert(0, time.strftime('%H:%M:%S',
                                               time.localtime(cutoff)))
        self.time_to.delete(0, 'end')
        self.time_to.insert(0, time.strftime('%H:%M:%S', time.localtime(now)))
        self.show_rows(rows)

    def on_clear(self):
        for box in (self.day_from, self.day_to, self.time_from, self.time_to,
                    self.text_box, self.min_secs):
            box.delete(0, 'end')
        self.span_label.config(text='')

    def on_all(self):
        self.show_rows(webcam_archive.search(limit=5000,
                                             camera=self.picked_camera()))

    def show_rows(self, rows):
        self.rows = rows
        self.tree.delete(*self.tree.get_children())
        for index, row in enumerate(rows):
            self.tree.insert(
                '', 'end', iid=str(index),
                values=(row.get('camera', 1),
                        f"{row['day']} {row['clock']}",
                        hhmmss(row['duration']),
                        f"{row['size'] / 1e6:.0f} MB",
                        row.get('note', '')))
        self.found.config(
            text=(f'{len(rows)} segment(s) listed' if rows
                  else 'nothing in the index matched that search'),
            fg='#003366' if rows else '#aa0000')

    # -- playback --

    def on_pick(self, _event=None):
        picked = self.tree.selection()
        if not picked:
            return
        row = self.rows[int(picked[0])]
        self.open_file(row)

    def open_file(self, row):
        self.on_pause()
        if self.capture is not None:
            self.capture.release()
        self.current = row
        self.capture = cv2.VideoCapture(row['path'])
        if not self.capture.isOpened():
            self.video.itemconfigure(self.video_item, image='')
            self.video.itemconfigure(
                self.video_text,
                text='that file will not open - it may still be being written')
            self.capture = None
            return
        self.frame_count = int(self.capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.video_fps = self.capture.get(cv2.CAP_PROP_FPS) or 30.0
        self.scrub.config(to=max(1, self.frame_count - 1))
        self.scrub.set(0)
        self.show_next()
        self.on_clip_fill()

    def show_next(self):
        if self.capture is None:
            return False
        ok, frame = self.capture.read()
        if not ok:
            return False
        photo = to_photo(frame, 640, 360)
        if photo is None:
            return False
        self.photo = photo
        self.video.itemconfigure(self.video_item, image=photo)
        self.video.coords(self.video_item, 320, 180)
        self.video.itemconfigure(self.video_text, text='')
        at = int(self.capture.get(cv2.CAP_PROP_POS_FRAMES))
        if not self.seeking:
            self.scrub.set(at)
        seconds = at / max(1.0, self.video_fps)
        rate, draw_fps = self.speed()
        self.at_label.config(
            text=f'{hhmmss(seconds)} of '
                 f'{hhmmss(self.frame_count / max(1.0, self.video_fps))}'
                 + (f'   {self.wall_clock(seconds)}' if self.current else '')
                 + (f'   x{rate:g}' if self.playing and rate != 1.0 else ''))
        return True

    def wall_clock(self, offset):
        """What time it actually was - the point of an archive. The segment's
        start comes from its name, so an offset into the file is a real clock
        time without any guessing."""
        if not self.current:
            return ''
        return time.strftime('%H:%M:%S',
                             time.localtime(self.current['started'] + offset))

    def speed(self):
        """The chosen speed as (rate, draw_fps), falling back to normal."""
        label = self.speed_var.get()
        for entry in PLAYBACK_SPEEDS:
            if entry[0] == label:
                return entry[1], entry[2]
        return PLAYBACK_SPEEDS[PLAYBACK_DEFAULT][1:]

    def on_speed(self, _event=None):
        """Changing speed mid-play keeps the position: the playhead stays
        where it is and only the rate it moves at changes, which is what makes
        slowing down at the interesting moment useful."""
        if self.playing:
            self.playing = False
            self.after(10, self.on_play)

    def frame_step(self):
        """How many source frames to move for each frame drawn.

        At normal speed and below this is 1 and the gap between draws does the
        work. Past normal it has to skip, because a file holds 30 frames per
        second of footage and 60 seconds of footage a second would mean 1800
        of them - so it takes every Nth frame instead, which is exactly what
        fast forward looks like on anything else.
        """
        rate, draw_fps = self.speed()
        step = rate * max(1.0, self.video_fps) / float(draw_fps)
        return max(1, int(round(step)))

    def on_play(self):
        if self.capture is None:
            self.on_pick()
        if self.capture is None:
            return
        if not self.playing:
            self.playing = True
            self.play_tick()

    def on_pause(self):
        self.playing = False

    def play_tick(self):
        if not self.playing:
            return
        step = self.frame_step()
        if step > 1 and self.capture is not None:
            # Skipping is a seek rather than reading and throwing frames away:
            # at 1 min/sec that would be 1800 decodes a second to show 15.
            at = self.capture.get(cv2.CAP_PROP_POS_FRAMES)
            target = at + step - 1
            if target >= self.frame_count - 1:
                self.playing = False
                return
            try:
                self.capture.set(cv2.CAP_PROP_POS_FRAMES, int(target))
            except (cv2.error, ValueError):
                self.playing = False
                return
        if not self.show_next():
            self.playing = False
            return
        _rate, draw_fps = self.speed()
        self.after(max(10, int(1000.0 / max(1.0, draw_fps))), self.play_tick)

    def on_scrub(self, value):
        if self.capture is None or not self.seeking:
            return
        try:
            self.capture.set(cv2.CAP_PROP_POS_FRAMES, int(float(value)))
        except (cv2.error, ValueError):
            return
        self.show_next()

    def nudge(self, seconds):
        if self.capture is None:
            return
        at = self.capture.get(cv2.CAP_PROP_POS_FRAMES)
        target = max(0, min(self.frame_count - 1,
                            at + seconds * self.video_fps))
        self.capture.set(cv2.CAP_PROP_POS_FRAMES, int(target))
        self.show_next()

    # -- clips --

    def stamp(self, when):
        return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(when))

    def on_clip_fill(self):
        if not self.current:
            return
        self.clip_from.delete(0, 'end')
        self.clip_from.insert(0, self.stamp(self.current['started']))
        self.clip_to.delete(0, 'end')
        self.clip_to.insert(0, self.stamp(self.current['ended']))
        self.clip_cam.config(
            text=f"from camera {self.current.get('camera', 1)}")

    def playhead(self):
        if self.capture is None or not self.current:
            return None
        at = self.capture.get(cv2.CAP_PROP_POS_FRAMES)
        return self.current['started'] + at / max(1.0, self.video_fps)

    def on_mark_start(self):
        when = self.playhead()
        if when:
            self.clip_from.delete(0, 'end')
            self.clip_from.insert(0, self.stamp(when))

    def on_mark_end(self):
        when = self.playhead()
        if when:
            self.clip_to.delete(0, 'end')
            self.clip_to.insert(0, self.stamp(when))

    def on_export(self):
        try:
            start = webcam_archive.parse_when(self.clip_from.get())
            end = webcam_archive.parse_when(self.clip_to.get())
        except ValueError as problem:
            messagebox.showerror('Clip', str(problem))
            return
        if end <= start:
            messagebox.showerror('Clip', 'The end time is not after the start '
                                         'time.')
            return
        export, extension = webcam_archive.format_for(self.format_var.get())
        # A clip comes from one camera: whichever one the selected segment
        # belongs to, since that is the view being watched when the span was
        # marked. Without a selection, camera 1.
        camera = int(self.current.get('camera', 1)) if self.current else 1
        suggested = 'clip-cam%d-%s-to-%s%s' % (
            camera, time.strftime('%Y%m%d-%H%M%S', time.localtime(start)),
            time.strftime('%H%M%S', time.localtime(end)), extension)
        os.makedirs(webcam_archive.CLIPS_DIR, exist_ok=True)
        out = filedialog.asksaveasfilename(
            title='Save clip', initialdir=webcam_archive.CLIPS_DIR,
            initialfile=suggested, defaultextension=extension,
            filetypes=[('MP4 video', '*.mp4'), ('Matroska video', '*.mkv'),
                       ('AVI video', '*.avi'), ('All files', '*')])
        if not out:
            return
        # The chosen format wins over whatever the save dialog left on the
        # name: picking MKV and ending up with a file called .mp4 holding
        # matroska would be a file that will not open on half of what opens
        # mp4, which is the opposite of the point.
        if not out.lower().endswith(extension):
            out = os.path.splitext(out)[0] + extension
        self.clip_state.config(text='cutting...')

        def work():
            try:
                path = webcam_archive.clip(
                    start, end, out, export=export, camera=camera,
                    progress=lambda text: self.after(
                        0, lambda: self.clip_state.config(text=text)))
            except Exception as problem:                          # noqa: BLE001
                log_error(f'clip: {problem}')
                self.after(0, lambda: self.clip_state.config(
                    text=f'clip failed: {problem}'))
                return
            size = os.path.getsize(path) / 1e6
            self.after(0, lambda: self.clip_state.config(
                text=f'saved {os.path.basename(path)} ({size:.0f} MB) - '
                     f'{hhmmss(end - start)} long'))
        threading.Thread(target=work, daemon=True).start()


# ---- the window that holds the pages ----

class App:

    def __init__(self, root):
        self.root = root
        root.title('Webcam - live video, archive and settings')
        self.recorder = webcam_record.RecorderSet()

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill='both', expand=True)

        self.live = LivePage(self.notebook, self)
        self.notebook.add(self.live, text='  Live video  ')

        self.archive = ArchivePage(self.notebook, self)
        self.notebook.add(self.archive, text='  Archive  ')

        # The settings page is the old window, and it is built the first time
        # the tab is opened rather than at startup: it reads every control off
        # the camera, probes the brain and starts the scheduler's terminal,
        # which is a second or two that should not sit in front of the video.
        self.settings_host = tk.Frame(self.notebook)
        self.notebook.add(self.settings_host, text='  Settings  ')
        self.settings = None
        self.notebook.bind('<<NotebookTabChanged>>', self.on_tab)

        root.protocol('WM_DELETE_WINDOW', self.on_close)
        root.after(300, self.autostart)

    def autostart(self):
        """Open recording, on every enabled camera.

        This is a security recorder, and a security recorder that waits to be
        told to record has already missed whatever happened while it waited.
        The loop cap is what makes that safe to do unattended: recording from
        the moment it opens can only ever use the archive's own cap, and the
        oldest footage is recycled rather than the disk filling up.
        """
        if not self.recorder.is_running():
            self.recorder.start(record=True)

    def on_tab(self, _event=None):
        if self.notebook.index('current') != 2 or self.settings is not None:
            return
        tk.Label(self.settings_host, text='opening the settings page...').pack()
        self.root.update_idletasks()
        try:
            import webcam_settings_page
            for child in self.settings_host.winfo_children():
                child.destroy()
            self.settings = webcam_settings_page.Window(self.root,
                                              container=self.settings_host)
        except Exception as problem:                              # noqa: BLE001
            log_error(f'settings page: {problem}')
            for child in self.settings_host.winfo_children():
                child.destroy()
            tk.Label(self.settings_host, fg='#aa0000', wraplength=700,
                     justify='left',
                     text=('The settings page would not open:\n\n'
                           f'{problem}\n\nwebcam_settings_page.py still runs on '
                           'its own with  python3 webcam_settings_page.py'
                           )).pack(padx=10,
                                                                pady=10)

    def on_close(self):
        self.recorder.stop()
        if self.settings is not None:
            try:
                self.settings.terminal.stop()
            except Exception:                                     # noqa: BLE001
                pass
        self.root.destroy()


def main():
    webcam_record.ensure_dirs()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
