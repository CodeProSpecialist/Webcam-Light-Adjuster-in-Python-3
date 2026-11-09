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
