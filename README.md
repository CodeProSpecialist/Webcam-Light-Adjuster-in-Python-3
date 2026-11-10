# Webcam Light Adjuster in Python 3

The Python 3 version of the webcam light adjuster, matching the Perl repository
(Perl-Outdoor-Webcam-Light-Adjuster) value for value.

Automatically adjusts an outdoor USB webcam's settings in Ubuntu Linux, 24.04
and 26.04: it
switches between tuned presets on today's real sunrise and sunset times, and
corrects whichever preset is in force by measuring the picture the camera is
actually producing. Written for a 1080p HD USB webcam, and it drives two
different control families, so it suits an older webcam and a current one.

<!-- SCREENSHOT: run it, screenshot the terminal, and save it as
     docs/screenshot-terminal.png, then delete this comment and the one
     around the image line below.

       mkdir -p docs
       python3 python3-sunrise-sunset.py          # let it print a few cycles
       import -window $(xdotool getactivewindow) docs/screenshot-terminal.png

     (import is ImageMagick; the Ubuntu screenshot tool saved to the same path
     works just as well.)
-->
<!-- ![The scheduler running](docs/screenshot-terminal.png) -->

## Start this before the recording software

**Run `python3-sunrise-sunset.py` first, then open Xeoma or whatever else records the camera.**

That order is what makes the snapshots work. A V4L2 camera lets one program
stream at a time, so the snapshot this program takes to measure the light can
only be grabbed while the camera is still free. Start it first and it takes one
every five seconds and corrects the light from them. Start the recorder first
and the grab is impossible - the status line then names the program holding the
camera, like `Xeoma (pid 2317) has /dev/video0`, and nothing is sent to the
device while it is in use.

The order stops mattering if the recording software writes its own stills into
the snapshot folder, since those are measured just the same. Either arrangement
works; one of them has to.

## Install

The programs check their own modules on the first run. Start one with a module
missing and it names the module and the apt package that provides it, shows the
command, and offers to run it:

```
webcam: these Python modules are needed and not installed:
  astral

This can install them now with:

    sudo apt-get -y install python3-astral

sudo will ask for your password.
Install them? [y/N]
```

Answer anything but yes and it exits having changed nothing. Once they are
installed it says nothing on any later run, and `sudo` is only ever invoked
when something is actually missing - an ordinary start never asks for a
password.

Under cron, systemd or a pipe it does not ask and does not install: there is no
terminal to answer on, and a prompt there would wait for an answer nobody can
give while the scheduler silently never started. It prints the command and
exits instead, so run it by hand once before putting any of this in a crontab.

It installs from apt and not pip, for the reason given below. The
check lives in `webcam_bootstrap.py`; `WEBCAM_NO_BOOTSTRAP=1` skips it, which
is what to use when the modules come from a virtual environment.

To install everything yourself first:

```
sudo apt-get -y install v4l-utils imagemagick ffmpeg curl python3-astral python3-tk
```

Keep all of the project files in the same folder.

`astral` is taken from apt rather than pip because Ubuntu 24.04 and 26.04
refuse `pip3 install` into the system Python - the error is
`externally-managed-environment`. If apt does not have it, either use a virtual
environment or pass `--break-system-packages`:

```
python3 -m venv ~/webcam-venv && ~/webcam-venv/bin/pip install astral
# or
pip3 install --break-system-packages astral
```

`imagemagick` measures the picture for the automatic light adjustment and
`ffmpeg` takes the snapshots. On Ubuntu 26.04 the `imagemagick` package
installs ImageMagick 7, whose command is `magick`; 24.04 and earlier install
version 6, whose command is `convert`. Either is accepted - the command name is
worked out at startup, because getting this wrong does not produce an error, it
just measures nothing.

`imagemagick` is what measures the picture for the automatic light adjustment.
Without it the schedule still runs; only the correction stays idle.

## Run

```
python3 python3-sunrise-sunset.py
```

Start this before the recording software - see above for why.

The scripts resolve each other next to the file being run, then in `$HOME`,
then in the working directory, so they work both from the repository and copied
into the home directory, and under cron either way. Each preset is run with the
same interpreter this scheduler is running under, rather than whatever
`python3` happens to mean, and its exit status is checked - a preset that
failed used to look exactly like one that worked, which mattered most for the
one preset nobody is awake to notice.

### The clock

Every light change here is worked out from the clock - the sunrise and sunset
offsets, the 04:46 refresh, which preset belongs to this minute. A computer
whose clock has drifted applies the night preset in daylight, and nobody
notices until the recording is useless.

So the real time is fetched at startup and once a day after that, and the
difference from this machine's clock is carried as an offset. Every decision
goes through that corrected time.

The source is the `Date` header of an HTTPS response - every web server sends
one, it needs no API key and cannot be rate limited away - from google.com,
then cloudflare.com, then timeapi.io as a third try.

**The system clock is not changed.** That needs root, and a camera program has
no business asking for it: Ubuntu already runs systemd-timesyncd, so if this
reports a large offset for days on end, `timedatectl status` is the thing to
look at rather than something for this program to override.

Being offline is not a failure - the fetch simply does not answer, the previous
offset stays, and everything carries on with the system clock as before. The
fetch is also how "are we connected" gets answered; a separate check that
passed while the time server was unreachable would tell us nothing useful.
The clock status is shown at startup, and an offset of ten seconds or more goes
to the error log.

### Where it thinks it is

Sunrise and sunset depend on where you are, and there is no place written down
anywhere in these programs. The location is worked out at startup, first answer
wins:

1. `WEBCAM_LAT` and `WEBCAM_LON`, for a machine that should not be guessing -
   a camera on a tower a county away from the computer driving it, say.
2. The cached answer in `~/.webcam-location.conf`, if it is less than a week
   old. Nothing goes out to the network on an ordinary run.
3. An IP lookup, which gives the city the connection comes out of.
4. The last cached answer whatever its age, since a stale location beats none -
   sunrise moves by about a minute a day, not an hour.

Worth being plain about the lookup: it tells a third party (ipinfo.io, and
ip-api.com if that one is not answering) this machine's IP address, and what
comes back is where the internet connection surfaces, not where the camera is.
On a VPN it is wrong by however far the VPN reaches. That is why the answer is
cached, shown, and overridable:

```
export WEBCAM_LAT=41.2619
export WEBCAM_LON=-95.8608
export WEBCAM_TZ=America/Chicago      # optional; the system's zone otherwise
```

Set those and no IP lookup is ever made. Accuracy only has to be rough: a
tenth of a degree of latitude moves sunrise by well under a minute, and these
presets switch on offsets of 7 to 122 minutes, so a city-level answer is as
good as a surveyed one.

**The time zone comes from the coordinates**, not from the system clock. The
two are different things: the system zone is whatever this computer was set up
with, and a camera reporting to a machine in another state, or a laptop carried
across a zone line, would have its sun times worked out for the wrong one. The
coordinates are where the camera is, so the zone belonging to those coordinates
is the right answer - looked up once and cached beside them, from the system's
own zone only if both services are unreachable. `WEBCAM_TZ` overrides it.

The settings file is written on the first successful lookup and again whenever
the sunrise and sunset times are fetched over the internet, so the place those
times were worked out for is what stays saved:

```
$ cat ~/.webcam-location.conf
lat=41.2619
lon=-95.8608
tz=America/Chicago
source=confirmed by the sun-times fetch
saved=2026-11-10 11:13
```

Delete that file to make it look the location up again.

## The control window

```
python3 webcam_GUI.py
```

A window with the same presets, the same engine and the same automatic light
adjustment as the scheduler. Both can run at once: neither opens the video
stream, they only set controls, and each re-reads the other's work rather than
fighting over it - press a preset here and the light meter notices within five
seconds and re-learns it as its baseline.

It builds itself from the camera. Every control on the window comes from
`v4l2-ctl -L` at startup, with the range the camera reported, which is the only
way one window serves both the older webcam on 0-255 ranges and the current
ones where brightness runs -64 to 64. Integer and menu controls get a slider
over the camera's own range, booleans get a checkbox, and a control the camera
has that this program has never heard of still appears rather than being
quietly dropped. Nothing is sent that the camera did not confirm it has.

The window scrolls - a camera with every control the kernel defines makes a
taller column than most screens have room for - with a scrollbar on the right,
one along the bottom, the wheel, Page Up and Page Down, Home and End.

There is no preview, deliberately: a V4L2 camera lets one program stream at a
time and the recording software has it. Judge the picture in whatever is
recording it.

It needs `python3-tk`, which is in the install line above.

## The schedule

Presets switch on offsets from today's real sunrise and sunset:

| Time          | Preset         |
|---------------|----------------|
| sunrise − 7   | golden morning |
| sunrise + 26  | day            |
| sunrise + 122 | bright sun     |
| sunset − 39   | day again      |
| sunset − 10   | golden evening |
| sunset + 14   | night          |
| sunset + 30   | night again    |

Night is applied twice because a camera does not always take the settings on
the first attempt. Edit the minute offsets in `python3-sunrise-sunset.py`.

Today's times are refreshed at 04:46, which is before the earliest sunrise of
the year, so the new times are always in place before the morning's first
change. The lookup tries the internet first, then the times saved in
`~/.webcam-sun-times.conf`, then a local calculation; if all three fail,
yesterday's times stay in use rather than being wiped.

## Camera settings engine

The per-preset scripts hold no numbers of their own. They all call
`webcam_presets.py`, which reads the control list from whichever camera is
plugged in and picks the matching values:

* The older webcam uses the pre-5.10 kernel control names (`exposure_auto`,
  `exposure_absolute`, `focus_auto`) on 0-255 ranges.
* The Walmart ONN webcam uses the current names (`auto_exposure`,
  `exposure_time_absolute`) on much smaller ranges.

The same number means a different brightness on each, so the engine keeps one
value table per family, clamps every value to the range the camera reports, and
skips controls a camera does not have. It prints `(asked 175, clamped)` when it
has to clamp.

Run a preset directly:

```
python3 webcam_presets.py night
python3 webcam_presets.py night-mono
python3 webcam_presets.py golden-am
python3 webcam_presets.py day
python3 webcam_presets.py bright-sun
python3 webcam_presets.py golden-pm
```

Set `WEBCAM_DEVICE`, or pass a second argument, for a camera other than
`/dev/video0`. The wrapper scripts - `night-sharp-webcam.py` and the rest - are
kept so existing habits and the scheduler keep working; each is the same as one
`webcam_presets.py` call.

## Night tuning and white balance

Night brightness comes from a long exposure rather than the brightness control,
which saturates. The night preset asks for a long exposure, reads it back, and
steps down until the driver accepts one, because the driver silently clamps any
exposure that will not fit inside the current frame interval.

Night was retuned to match the ONN settings, mapped onto the older camera's
0-255 ranges rather than copied as raw numbers: brightness and gain to the top,
the longest exposure the driver will take, backlight compensation on, and
sharpness cut back from 250 to 109.

`night-mono-sharp-webcam.py` is the same thing without colour. Colour noise is
most of what makes a dark frame look muddy, so dropping saturation to 0 trades
colour for a cleaner, apparently brighter picture.

Every preset that asks for it uses a cold white balance of 2800. Note that
`white_balance_temperature` sets the colour temperature the camera **assumes**
the light to be, not the look of the picture: a low value tells the camera the
light is warm, so it compensates with blue and the result looks cold. Change
`COLD_WB` at the top of `webcam_presets.py` to adjust it. Day, golden morning
and golden evening use automatic white balance instead - the evening light
swings from white through orange to dusk inside the hour that preset covers, so
no single temperature is right for more than a few minutes of it.

## Automatic light adjustment (24/7)

The schedule above still picks the preset. On top of it, the scheduler measures
the picture the camera is actually producing, every 5 seconds, and corrects
whatever the preset set so the result is neither too bright nor too dark. A
cloudy afternoon, a security light coming on at 2am, snow glare at noon - none
of that is on a clock, so none of it can be handled by one.

It is a correction and not a replacement. Every preset stays exactly as it is,
including its white balance, focus and exposure mode. The correction moves five
light controls away from the preset's own values by a trim of −5 to +5 steps:

* **gain** carries most of it, being the control that actually amplifies signal
* **brightness** moves least, since on these webcams it is a black level lift
  rather than exposure
* **contrast** rises with the trim to keep the range spread out
* **saturation** comes *down* as the trim goes up, so there is colour day and
  night, with less of it at night where most of what is left is noise
* **backlight compensation** rises with the trim

When a preset fires, or a control is moved by hand, the loop notices the
settings are not what it last wrote and re-learns them as its new baseline,
with the trim back at zero. Nothing has to tell it a preset ran. The comparison
allows two units of slack, because a driver may round a value to its own step
size - without it, a value that came back as 99 after 100 was sent would look
like an outside change, the baseline would be re-learnt with the trim already
inside it, and the next correction would trim again from there: a ratchet that
walks the picture off in one direction.

### What it measures

Average grey level, plus two clipping figures: how much of the frame has gone
to pure white and how much to pure black. The mean cannot see either problem on
its own - a frame that is half blown-out floodlight and half pitch black
averages to a respectable middle grey while showing nothing at all. Either
figure can override the mean; the limits are 2% blown white and 30% crushed
black, against a target grey level of 100 of 255. When both are out at once,
which is what a security light over black tarmac looks like, it holds where it
is and says so, rather than chasing a picture the sensor cannot hold.

It takes the median of three samples, needs two consecutive samples to agree
before it moves, and waits two samples after each change, so passing headlights
do not drag the picture dark for the next minute.

A status line goes to the terminal as it works:

```
light meter: trim +2, mean 91 of target 100, white 0.0%, black 14% - picture is where it should be
```

The loop itself lives in `webcam_light_meter.py`, and is imported
defensively: if that module or ImageMagick is missing, the scheduler starts and
runs the clock-driven presets exactly as before. The schedule's 20-second wait
is handed to the meter so the picture is sampled four times during it, instead
of the scheduler sitting blind for the whole 20.

### Sharpness

Sharpness is set outright rather than trimmed, and it overrides whatever the
preset asked for: full sharpness in daylight and through the golden hours,
eased to 80-90% of the control's range at night.

What decides night from day is gain rather than the clock. Gain is the only
control that amplifies sensor noise along with the signal, so a camera near the
top of its gain range is working in the dark whatever the time - and the golden
hour presets, which sit under half the gain range, come out at full sharpness.
Sharpening is contrast enhancement at the pixel level and it sharpens noise as
happily as it sharpens the edge of a number plate, which is the only reason to
ease off at all.

If no snapshots appear in the folder, the status line now says why rather than
guessing. The reasons it can give:

| Message | What to do |
|---------|------------|
| `<program> (pid N) has /dev/video0` | That program has the camera. Start this one before it to take snapshots here, or have it write stills into the folder. |
| `no permission to open /dev/video0` | `sudo usermod -aG video $USER`, then log out and back in. |
| `/dev/video0 does not exist` | The camera is unplugged, or it is on another node - check `v4l2-ctl --list-devices`. |
| `ffmpeg is not installed` | `sudo apt install ffmpeg`. |
| `ffmpeg could not take a snapshot: ...` | ffmpeg's own words for anything else. |

To see the whole error yourself:

```
ffmpeg -nostdin -f v4l2 -i /dev/video0 -frames:v 1 -y /tmp/test.jpg
```

A busy camera is retried on every pass rather than written off for the session,
so if the recording software lets go of the device the snapshots start on their
own.

### The error log

Everything that goes wrong is written to `~/.webcam-light-meter.log` with a
timestamp, so a failure at three in the morning can be read back in the
morning - the status lines only ever show the latest thing. Set `WEBCAM_LOG`
to put it somewhere else.

```
tail -f ~/.webcam-light-meter.log
```

Logged: the startup probe and what it found, the snapshot folder, every
snapshot failure with ffmpeg's own words, every trim applied and what the
measurement was, every time a preset is re-learnt as the baseline, and anything
that stops the loop working. Not logged: the routine pass where the picture is
already where it should be, which would be twelve lines a minute and would bury
everything worth reading.

Repeats are collapsed - a busy camera says so every five seconds, and an error
identical to the last one is written again only after ten minutes. The file is
trimmed to its newest 500 lines once it passes a megabyte, so it cannot grow
without end.

### Focus

Continuous autofocus is on at all times, and nothing in these programs ever
leaves the camera in manual focus. Every preset ends by switching autofocus off
and straight back on, and so does every correction the light meter makes.

That toggle is the point of it. Changing gain, brightness or contrast changes
what the autofocus is looking at, and the camera does not always re-run its
search on its own - so the first seconds after a light change come out soft and
out of focus, which on a recording is exactly the part you want sharp. Setting
the control to 1 when it is already 1 does nothing; turning it off and back on
starts a fresh search. There is a third of a second between the two writes,
because the driver has to see the control actually change state - land both in
the same instant and nothing is re-triggered.

The preset tables used to carry a fixed focus distance, applied with autofocus
off. That column is gone and `focus_absolute` is no longer set.

### Control ranges

At startup the camera is asked what its controls can actually do
(`v4l2-ctl -L`) and those ranges are what get used, because a trim step is a
fraction of a control's range: assume 0-64 for a contrast control that really
runs 0-95 and every correction lands a third short. Anything the camera does
not answer for falls back to the ranges these programs were already using - the
ONN set for a modern camera, the 0-255 set for the older one - so an unanswered
control still gets a sensible step instead of being dropped. A line at startup
says which ranges came from the camera and which were assumed:

```
light meter: modern camera, ranges read from the camera for backlight_compensation, brightness, contrast, gain, saturation, sharpness
```

### Setting up the snapshot

The camera cannot be opened for this: V4L2 allows only one program to stream,
and the recording software holds the device. So the still comes from that
software instead. Set `WEBCAM_SNAPSHOT` to one of:

* a **directory** - the newest `.jpg` in it is measured, which is what a "save
  snapshots to folder" module produces; anything older than ten minutes is
  ignored
* a **single file** - for software that overwrites the same still each time
* an **`http://` URL** - a snapshot URL from the recording software's web server

```
sudo apt install imagemagick
export WEBCAM_SNAPSHOT=$HOME/webcam-snapshots
export WEBCAM_TARGET=100        # target grey level out of 255, optional
export WEBCAM_DEVICE=/dev/video0
```

The default folder is
`~/Pictures/onn-webcam/program-snapshots-for-auto-adjust-picture` under
whoever's home directory the program is running as - no user name is written
into any of this. `$HOME` is used when it is set, and the password database
when it is not, which is the case under some cron and systemd setups.

**Taking the snapshots.** That one folder is created at startup if it is not
already there, parent folders and all. Nothing else is ever created.
If the recording software is writing stills into it, those are what get
measured. If it is not - an empty folder on the first run - a frame is grabbed
here instead with ffmpeg. That only works while the camera is free: a V4L2
camera lets one program stream at a time, so if the recording software has the
device the grab fails with a busy error, reported once rather than every five
seconds all night.

**Which program opens the camera first decides this.** A V4L2 camera lets one
program stream at a time, so:

* Start this program **before** the recording software and the snapshots are
  taken here, every five seconds, for as long as the camera stays free.
* Once the recording software opens the camera, the grab stops - and the
  status line names the process holding it, something like
  `Xeoma (pid 2317) has /dev/video0`. Nothing is sent to the device while
  another program has it: retrying would achieve nothing, and if the recorder
  dropped and re-opened its stream at the instant a grab landed, the grab
  would win and the recorder would fail to come back. The check is repeated
  once a minute rather than every five seconds.
* While the recorder has the camera, the folder is still read - so stills that
  the recording software writes there are measured exactly as before, and the
  light keeps being corrected.

The practical arrangement is either one: let this program take the snapshots
by starting it first, or have the recording software write stills into that
folder and start it whenever you like.

**Only the last 30 are kept.** Each new snapshot prunes the folder back to the
newest 30, oldest deleted first, and the same happens once at startup. Every
`.jpg` in the folder counts, whoever wrote it.

With no snapshot available, nothing is corrected and the presets stand as they
always did.

## Files

| File | What it is |
|------|------------|
| `python3-sunrise-sunset.py` | the scheduler |
| `webcam_GUI.py` | the control window: every camera control, presets, light meter |
| `webcam_location.py` | works out where the camera is, for the sun times |
| `webcam_time.py` | checks the clock against a time server |
| `webcam_light_meter.py` | the automatic light adjustment loop |
| `webcam_presets.py` | the settings engine: reads the camera, applies a preset |
| `webcam_bootstrap.py` | the first-run check: names missing modules, offers the apt packages |
| `night-sharp-webcam.py` | wrapper: night |
| `night-mono-sharp-webcam.py` | wrapper: night without colour |
| `post-sunrise-sharp-webcam.py` | wrapper: golden morning |
| `day-sharp-webcam.py` | wrapper: day |
| `bright-sun-sharp-webcam.py` | wrapper: bright sun |
| `pre-sunset-sharp-webcam.py` | wrapper: golden evening |
| `~/.webcam-sun-times.conf` | the last sunrise and sunset times, for offline use |
