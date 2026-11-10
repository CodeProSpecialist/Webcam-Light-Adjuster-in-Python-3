# Webcam Light Adjuster in Python 3

The Python 3 version of the webcam light adjuster, matching the Perl repository
(Perl-Outdoor-Webcam-Light-Adjuster) value for value.

Automatically adjusts an outdoor USB webcam's settings in Ubuntu Linux: it
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

## Install

```
pip3 install astral
sudo apt-get -y install v4l-utils python3-pip imagemagick curl
```

Keep all of the project files in the same folder.

`imagemagick` is what measures the picture for the automatic light adjustment.
Without it the schedule still runs; only the correction stays idle.

## Run

```
python3 python3-sunrise-sunset.py
```

The scripts resolve each other next to the file being run, then in `$HOME`,
then in the working directory, so they work both from the repository and copied
into the home directory, and under cron either way. Each preset is run with the
same interpreter this scheduler is running under, rather than whatever
`python3` happens to mean, and its exit status is checked - a preset that
failed used to look exactly like one that worked, which mattered most for the
one preset nobody is awake to notice.

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

With no snapshot set, nothing is corrected and the presets stand as they always
did.

## Files

| File | What it is |
|------|------------|
| `python3-sunrise-sunset.py` | the scheduler |
| `webcam_light_meter.py` | the automatic light adjustment loop |
| `webcam_presets.py` | the settings engine: reads the camera, applies a preset |
| `night-sharp-webcam.py` | wrapper: night |
| `night-mono-sharp-webcam.py` | wrapper: night without colour |
| `post-sunrise-sharp-webcam.py` | wrapper: golden morning |
| `day-sharp-webcam.py` | wrapper: day |
| `bright-sun-sharp-webcam.py` | wrapper: bright sun |
| `pre-sunset-sharp-webcam.py` | wrapper: golden evening |
| `~/.webcam-sun-times.conf` | the last sunrise and sunset times, for offline use |
