#!/usr/bin/env python3
"""Golden evening settings: low warm sun before sunset.

The camera settings themselves now live in webcam_presets.py, which reads the
control list from whichever camera is plugged in and picks the matching
values. The older webcam and the ONN webcam use different control names and
different ranges, so one hard-coded set of numbers cannot serve both.

This wrapper is kept so existing habits and the scheduler keep working:
    python3 pre-sunset-sharp-webcam.py
is the same as
    python3 webcam_presets.py golden-pm
"""

import os
import sys

# Look for the engine next to this script, then in the home directory, then in
# the current directory, because these scripts get copied to ~ and run both ways.
for d in (os.path.dirname(os.path.abspath(__file__)),
          os.path.expanduser('~'), os.getcwd()):
    if os.path.isfile(os.path.join(d, 'webcam_presets.py')):
        sys.path.insert(0, d)
        break
else:
    sys.exit('webcam_presets.py not found next to this script, in $HOME, '
             'or in the current directory.')

from webcam_presets import apply_preset   # noqa: E402

device = os.environ.get('WEBCAM_DEVICE', '/dev/video0')
sys.exit(0 if apply_preset('golden_pm', device) else 1)
