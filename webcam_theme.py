#!/usr/bin/env python3
"""One dark skin for every page of the webcam window.

Why this is a file of its own rather than colours typed into each page.

The window is three pages built at different times - the live video, the
archive, and the old settings window embedded as a tab - plus a handful of
dialogs that open on top of them. Each was written with whatever background
Tk gave it, which is the system grey, and the parts that show video or
terminal output were given dark backgrounds of their own because a bright
slab around a dark picture is unreadable at night. The result was a window
that was dark in the middle and grey at the edges, and a settings page that
was grey everywhere except the two terminals.

So the colours live here, once, and every page asks for them by name. The
palette is the one the terminal panes already used, so nothing that was
already dark has to change to match.

There are two halves to applying it:

  * ``apply(root)`` sets the Tk option database and the ttk styles, which is
    what makes a widget created LATER come out dark without being told. That
    covers the ordinary widgets - frames, labels, buttons, entries - on every
    page, including pages built minutes after the window opened.

  * ``recolour(widget)`` walks a tree that already exists and fixes anything
    created before the options were set, or created with an explicit colour.
    The option database is only consulted at creation time, so a page built
    before apply() ran would otherwise stay grey.

Both are safe to call more than once, and both are deliberately forgiving:
a widget that does not take a background (a ttk widget, a canvas item) is
skipped rather than raising, because a theme is not worth a traceback.
"""

import tkinter as tk
from tkinter import ttk


# The skin. These are the terminal pane's own colours, kept so that the
# video canvas, the scheduler's terminal and the brain suite all sit on the
# same background as the page around them.
BG = '#0d1117'          # the page itself - near black, cool rather than warm
PANEL = '#141d26'       # a tile or card lifted off the page
EDGE = '#1f2a35'        # borders, separators, the edge of a card
TEXT = '#c9d4de'        # ordinary text
DIM = '#8b9bab'         # secondary text, units, hints
ACCENT = '#4fd1c5'      # headings - the teal the badges and bars are built on
LINK = '#67b7f7'        # anything that behaves like a link
SELECT = '#1e3a4a'      # selected text and list rows
BUTTON = '#18222d'      # buttons and other raised controls
BUTTON_ON = '#223040'   # a button under the pointer or being pressed
ENTRY = '#0a0f15'       # typed-into fields, darker than the page
TROUGH = '#0a0f15'      # the groove of a scale or a scrollbar
GOOD = '#4ade80'        # ready, trained, running
WARN = '#ffd166'        # works, but not the way it should
BAD = '#ff8c8c'         # not there, or stopped

# Widget classes that take -background and -foreground. Anything not in here
# is left alone, which is how canvases keep their own dark colour and ttk
# widgets are left to the styles below.
_TEXTY = ('Label', 'Button', 'Checkbutton', 'Radiobutton', 'Menubutton',
          'Menu', 'Message', 'Text', 'Listbox', 'Spinbox', 'Entry',
          'LabelFrame')


def apply(root):
    """Set the option database and the ttk styles on this root window.

    Call it once, before the pages are built. Every widget created after it
    comes out dark without the page having to say so, which is the point:
    the pages stay about what they do and the colours stay here.
    """
    try:
        root.configure(bg=BG)
    except tk.TclError:
        pass

    # The option database. '*Class.option' matches every widget of that class
    # in this application, including ones that do not exist yet.
    options = {
        '*background': BG,
        '*foreground': TEXT,
        '*highlightBackground': BG,
        '*highlightColor': EDGE,
        '*activeBackground': BUTTON_ON,
        '*activeForeground': TEXT,
        '*selectBackground': SELECT,
        '*selectForeground': TEXT,
        '*insertBackground': TEXT,
        '*troughColor': TROUGH,
        '*Button.background': BUTTON,
        '*Button.activeBackground': BUTTON_ON,
        '*Button.highlightBackground': BG,
        '*Entry.background': ENTRY,
        '*Entry.readonlyBackground': ENTRY,
        '*Spinbox.background': ENTRY,
        '*Text.background': ENTRY,
        '*Listbox.background': ENTRY,
        '*Checkbutton.selectColor': ENTRY,
        '*Radiobutton.selectColor': ENTRY,
        '*Menu.activeBackground': SELECT,
        '*Scrollbar.background': BUTTON,
        '*Scrollbar.activeBackground': BUTTON_ON,
        '*Scale.background': BG,
        '*Scale.activeBackground': BUTTON_ON,
        '*LabelFrame.foreground': ACCENT,
    }
    for name, value in options.items():
        try:
            root.option_add(name, value)
        except tk.TclError:
            pass

    style_ttk(root)
    return root


def style_ttk(root):
    """The ttk half: the notebook tabs, the scrollbars, the progress bars.

    ttk ignores the option database entirely - it has its own themes - so the
    tab strip across the top of the window would stay grey however the rest
    of the page is painted. 'clam' is used because it is the only theme that
    ships everywhere and lets every colour be set; the native themes draw
    tabs with the system's own images and refuse most of this.
    """
    style = ttk.Style(root)
    try:
        style.theme_use('clam')
    except tk.TclError:
        pass

    style.configure('.', background=BG, foreground=TEXT,
                    fieldbackground=ENTRY, bordercolor=EDGE,
                    darkcolor=BG, lightcolor=BG, troughcolor=TROUGH,
                    focuscolor=ACCENT, insertcolor=TEXT)
    style.configure('TFrame', background=BG)
    style.configure('TLabel', background=BG, foreground=TEXT)
    style.configure('TLabelframe', background=BG, bordercolor=EDGE)
    style.configure('TLabelframe.Label', background=BG, foreground=ACCENT)
    style.configure('TButton', background=BUTTON, foreground=TEXT,
                    bordercolor=EDGE)
    style.map('TButton',
              background=[('active', BUTTON_ON), ('pressed', BUTTON_ON)])
    style.configure('TCheckbutton', background=BG, foreground=TEXT)
    style.configure('TEntry', fieldbackground=ENTRY, foreground=TEXT)
    style.configure('TCombobox', fieldbackground=ENTRY, foreground=TEXT,
                    background=BUTTON)
    style.configure('TNotebook', background=BG, bordercolor=EDGE,
                    tabmargins=(2, 4, 2, 0))
    style.configure('TNotebook.Tab', background=PANEL, foreground=DIM,
                    bordercolor=EDGE, padding=(12, 5))
    # The open tab is the lit one, and it is the page's own background so the
    # tab reads as part of the page rather than as a button above it.
    style.map('TNotebook.Tab',
              background=[('selected', BG), ('active', BUTTON_ON)],
              foreground=[('selected', TEXT), ('active', TEXT)])
    style.configure('Vertical.TScrollbar', background=BUTTON,
                    troughcolor=TROUGH, bordercolor=EDGE,
                    arrowcolor=DIM)
    style.configure('Horizontal.TScrollbar', background=BUTTON,
                    troughcolor=TROUGH, bordercolor=EDGE, arrowcolor=DIM)
    style.configure('Horizontal.TProgressbar', background=ACCENT,
                    troughcolor=TROUGH, bordercolor=EDGE)
    return style


def recolour(widget, skip_classes=()):
    """Repaint a tree that already exists, leaf by leaf.

    For pages built before apply() ran, and for the handful of widgets that
    were given an explicit colour when they were written. Anything already
    carrying one of this palette's own colours is left alone, so a deliberate
    red Stop button, a status dot or a video canvas keeps its meaning.
    """
    ours = {BG, PANEL, EDGE, BUTTON, BUTTON_ON, ENTRY, TROUGH}
    try:
        name = widget.winfo_class()
    except tk.TclError:
        return
    if name in skip_classes:
        return

    if name in ('Frame', 'Toplevel', 'Tk', 'Canvas', 'PanedWindow'):
        _set(widget, bg=BG)
        if name == 'Frame':
            _set(widget, highlightbackground=EDGE)
    elif name in _TEXTY:
        current = ''
        try:
            current = str(widget.cget('bg'))
        except tk.TclError:
            current = ''
        # A colour that was chosen on purpose - a red record button, a green
        # badge, a status dot - stays. Only the system grey is replaced.
        if not current or current in ours or _is_grey(current):
            if name == 'Button':
                _set(widget, bg=BUTTON, activebackground=BUTTON_ON)
            elif name in ('Entry', 'Text', 'Listbox', 'Spinbox'):
                _set(widget, bg=ENTRY)
            else:
                _set(widget, bg=BG)
        _set(widget, fg=_readable(_cget(widget, 'fg'),
                                  ACCENT if name == 'LabelFrame' else TEXT))
        if name in ('Checkbutton', 'Radiobutton'):
            _set(widget, selectcolor=ENTRY, activebackground=BG)
    elif name == 'Scale':
        _set(widget, bg=BG, fg=TEXT, troughcolor=TROUGH,
             activebackground=BUTTON_ON, highlightbackground=BG)
    elif name == 'Scrollbar':
        _set(widget, bg=BUTTON, troughcolor=TROUGH,
             activebackground=BUTTON_ON, highlightbackground=BG)

    for child in widget.winfo_children():
        recolour(child, skip_classes)


def _cget(widget, option):
    """One widget option, or '' if this widget has no such option."""
    try:
        return str(widget.cget(option))
    except tk.TclError:
        return ''


def _readable(colour, default):
    """A foreground that can actually be read on this background.

    The pages were written for a light background, so their text colours are
    dark inks - near-black greys for ordinary text, a dark amber or red where
    something is wrong. Dropped onto a near-black page those range from hard
    to read to invisible, and simply forcing every label to the same grey
    would throw away the one thing those colours carry, which is whether this
    line is ordinary, a warning or a failure.

    So a dark grey becomes the ordinary text colour, and a dark colour that
    is actually coloured is brightened instead - same hue, enough light in it
    to read. Anything already light is left exactly as it was, which is how
    the green on the active brain and the red on a failed library survive.
    """
    parts = _rgb(colour)
    if parts is None:
        return default
    red, green, blue = parts
    if max(parts) >= 150:
        return colour                       # already light enough to read
    if max(parts) - min(parts) <= 18:
        # A grey ink. The darker it was, the more ordinary it was meant to
        # look, which on this background means the plain text colour.
        return default if max(parts) <= 90 else DIM
    scale = 225.0 / max(1, max(parts))
    return '#%02x%02x%02x' % (min(255, int(red * scale)),
                              min(255, int(green * scale)),
                              min(255, int(blue * scale)))


def _rgb(colour):
    """(r, g, b) from '#rgb' or '#rrggbb', or None for anything else."""
    text = str(colour).strip().lower()
    if not text.startswith('#') or len(text) not in (4, 7):
        return None
    try:
        if len(text) == 4:
            return tuple(int(c * 2, 16) for c in text[1:])
        return tuple(int(text[i:i + 2], 16) for i in (1, 3, 5))
    except ValueError:
        return None


def _is_grey(colour):
    """Whether a colour is one of Tk's own greys rather than a chosen one.

    Tk hands out '#d9d9d9' and its relatives as the default background. Those
    are the ones worth replacing; '#cc3333' on the record button is not.
    """
    text = str(colour).strip().lower()
    if text in ('systembuttonface', 'systemwindowbody', 'white', ''):
        return True
    parts = _rgb(text)
    if parts is None:
        return False
    # Grey means the three channels are close together, and light means it
    # would glare next to the video.
    return max(parts) - min(parts) <= 12 and min(parts) >= 160


def _set(widget, **options):
    """configure() that shrugs off an option this widget does not have."""
    for name, value in options.items():
        try:
            widget.configure(**{name: value})
        except tk.TclError:
            pass
