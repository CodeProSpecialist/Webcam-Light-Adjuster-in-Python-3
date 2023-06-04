import subprocess
import time

def set_control(parameter, value):
    subprocess.run(['v4l2-ctl', '--set-ctrl', f'{parameter}={value}'])

# BELOW IS DAY SETTINGS BEFORE NIGHT SETTINGS WORK
set_control('sharpness', 250)
set_control('brightness', 128)
set_control('contrast', 32)
set_control('saturation', 32)
set_control('gain', 64)
set_control('exposure_auto', 1)
time.sleep(4)
set_control('exposure_absolute', 166)
set_control('exposure_auto_priority', 0)
time.sleep(4)
set_control('focus_absolute', 0)
time.sleep(4)
set_control('focus_auto', 0)
time.sleep(4)
set_control('zoom_absolute', 1)
time.sleep(1)

# BELOW IS NIGHT SETTINGS
set_control('sharpness', 250)
set_control('brightness', 193)
set_control('contrast', 32)
set_control('saturation', 32)
set_control('gain', 255)
set_control('exposure_auto', 1)
time.sleep(4)
set_control('exposure_absolute', 1873)
set_control('exposure_auto_priority', 1)
set_control('focus_absolute', 51)
set_control('focus_auto', 0)
time.sleep(4)
set_control('zoom_absolute', 1)

######################  repeat
time.sleep(2)

set_control('sharpness', 250)
set_control('brightness', 193)
set_control('contrast', 32)
set_control('saturation', 32)
set_control('gain', 255)
set_control('exposure_auto', 1)
time.sleep(4)
set_control('exposure_absolute', 1873)
set_control('exposure_auto_priority', 1)
set_control('focus_absolute', 51)
set_control('focus_auto', 0)
time.sleep(4)
set_control('zoom_absolute', 1)

time.sleep(3)

set_control('exposure_absolute', 166)

time.sleep(3)

set_control('exposure_absolute', 1873)

time.sleep(2)

# End the program
raise SystemExit
