# Webcam-Light-Adjuster-in-Python-3
The Python 3 version of a Webcam Light Adjuster that is based on the sunrise and sunset times. 

This software is currently configured for an Outdoor based webcam view. 

This software has been tested to work in Ubuntu Linux to automatically adjust webcam light 
depending upon the sunrise and sunset times. It was designed to work best with a 1080p HD usb webcam. 

This Python 3 code is going to work well by keeping all of the project files in the same folder or directory. 

Next, we can utilize pip3 to install the required python 3 modules like this:

pip3 install datetime

pip3 install astral

( Continue installing each of the Python 3 modules listed below by installing them one by one with pip3. 


In Linux, install the required webcam software dependencies:

sudo apt-get -y install v4l-utils python3-pip

Run the main command in a command line terminal:

python3 python3-sunrise-sunset.py

## Camera settings engine

The per-preset scripts no longer hold hard-coded numbers. They all call
`webcam_presets.py`, which reads the control list from whichever camera is
plugged in and picks the matching values:

* The older webcam uses the pre-5.10 kernel control names (`exposure_auto`,
  `exposure_absolute`, `focus_auto`) on 0-255 ranges.
* The Walmart ONN webcam uses the current names (`auto_exposure`,
  `exposure_time_absolute`) on much smaller ranges.

The same number means a different brightness on each, so the engine keeps one
value table per family and clamps every value to the range the camera reports.
Controls a camera does not have are skipped.

Run a preset directly:

    python3 webcam_presets.py night
    python3 webcam_presets.py night-mono
    python3 webcam_presets.py golden-am
    python3 webcam_presets.py day
    python3 webcam_presets.py bright-sun
    python3 webcam_presets.py golden-pm

Set `WEBCAM_DEVICE` or pass a second argument for a camera other than
`/dev/video0`.

Night brightness comes from a long manual exposure rather than the brightness
control, which saturates. The night preset asks for a long exposure, reads it
back, and steps down until the driver accepts one, because the driver silently
clamps any exposure that will not fit inside the current frame interval.

`post-sunrise-sharp-webcam.py` is the golden morning preset, so the schedule
now changes four kinds of light per day: golden morning, day, bright sun,
golden evening, then night.

These values match the Perl version in Perl-Outdoor-Webcam-Light-Adjuster
exactly.

## Night tuning and white balance

Night was retuned to match the ONN settings, mapped onto this camera's 0-255
ranges rather than copied as raw numbers: brightness and gain to the top, the
longest exposure the driver will take, backlight compensation on, and sharpness
cut back from 250 to 109. Heavy sharpening amplifies sensor noise in the dark,
which is why the ONN night preset sits at 3 of 7 rather than near maximum.
Set sharpness back to 250 in the night table if you prefer the old look.

`night-mono-sharp-webcam.py` is the same thing without colour. Colour noise is
most of what makes a dark frame look muddy, so dropping saturation to 0 trades
colour for a cleaner, apparently brighter picture.

Every preset uses a cold white balance (manual 2800). Note that
`white_balance_temperature` sets the colour temperature the camera ASSUMES the
light to be, not the look of the picture: a low value tells the camera the light
is warm, so it compensates with blue and the result looks cold. Change `COLD_WB`
at the top of `webcam_presets.py` to adjust it.

## Light change times

All three webcam programs now change the light at the same moments, taken from
the legacy Perl repo (Perl-Outdoor-Webcam-Light-Adjuster):

| Time          | Preset         |
|---------------|----------------|
| sunrise - 7   | golden morning |
| sunrise + 26  | day            |
| sunrise + 122 | bright sun     |
| sunset - 39   | day again      |
| sunset - 10   | golden evening |
| sunset + 14   | night          |
| sunset + 30   | night again    |

Night is applied twice because a camera does not always take the settings on
the first attempt. Edit the minute offsets in `python3-sunrise-sunset.py`.


## Automatic light adjustment (new)

The schedule above is still what picks the preset. On top of it, the scheduler
now measures the picture the camera is actually producing, every 5 seconds, and
corrects whatever the preset set so the result is neither too bright nor too
dark. A cloudy afternoon, a security light coming on at 2am, snow glare at
noon - none of that is on a clock, so none of it can be handled by one.

It is a correction and not a replacement. Every preset stays exactly as it is,
including its white balance, focus and exposure mode; the correction moves five
light controls - gain, brightness, contrast, saturation and backlight
compensation - away from the preset's own values by a trim of -5 to +5 steps.
Gain carries most of it, because it is the control that actually amplifies
signal. Brightness moves least, since on these webcams it is a black level lift
rather than exposure. Saturation comes *down* as the trim goes up, so there is
colour day and night, with less of it at night where most of the colour left is
noise. Each step is a fraction of the control's own range, read from
`v4l2-ctl -L`, so the same code suits cameras with different ranges.

When a preset fires, or you move a control by hand, the loop notices that the
settings are not what it last wrote and re-learns them as its new baseline,
with the trim back at zero. Nothing has to tell it a preset ran.

### What it measures

Average grey level, plus two clipping figures: how much of the frame has gone
to pure white and how much to pure black. The mean cannot see either problem on
its own - a frame that is half blown-out floodlight and half pitch black
averages to a respectable middle grey while showing nothing at all. Either
clipping figure can override the mean. When both are out at once, which is what
a security light over black tarmac looks like, it holds where it is and says
so, rather than chasing a picture the sensor cannot hold.

It takes the median of three samples, needs two consecutive samples to agree
before it moves, and waits two samples after each change, so passing headlights
do not drag the picture dark for the next minute.

### Setting it up

The camera cannot be opened for this: V4L2 allows only one program to stream,
and the recording software holds the device. So the still comes from that
software instead. Set `WEBCAM_SNAPSHOT` to one of:

* a directory - the newest `.jpg` in it is measured, which is what a "save
  snapshots to folder" module produces
* a single file - for software that overwrites the same still each time
* an `http://` URL - a snapshot URL from the recording software's web server

```
sudo apt install imagemagick
export WEBCAM_SNAPSHOT=$HOME/webcam-snapshots
export WEBCAM_TARGET=100        # target grey level out of 255, optional
```

With no snapshot set, nothing is corrected and the presets stand as they always
did. ImageMagick does the measuring; without it the correction stays idle and
the schedule keeps working.

The loop itself lives in `webcam_light_meter.py`, which is imported
defensively: if it or ImageMagick is missing, the scheduler starts and runs the
clock-driven presets exactly as before.
