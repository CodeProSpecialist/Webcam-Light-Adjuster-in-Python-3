import subprocess
import time

subprocess.call(['v4l2-ctl', '--set-ctrl', 'sharpness=250'])
subprocess.call(['v4l2-ctl', '--set-ctrl', 'brightness=128'])
subprocess.call(['v4l2-ctl', '--set-ctrl', 'contrast=32'])
subprocess.call(['v4l2-ctl', '--set-ctrl', 'saturation=32'])
subprocess.call(['v4l2-ctl', '--set-ctrl', 'gain=64'])
subprocess.call(['v4l2-ctl', '--set-ctrl', 'exposure_auto=1'])
# time.sleep(4)
subprocess.call(['v4l2-ctl', '--set-ctrl', 'exposure_absolute=2047'])
subprocess.call(['v4l2-ctl', '--set-ctrl', 'exposure_auto_priority=0'])
# time.sleep(4)
subprocess.call(['v4l2-ctl', '--set-ctrl', 'focus_absolute=0'])
# time.sleep(4)
subprocess.call(['v4l2-ctl', '--set-ctrl', 'focus_auto=0'])
# time.sleep(4)
subprocess.call(['v4l2-ctl', '--set-ctrl', 'zoom_absolute=1'])

time.sleep(2)

# BELOW IS DAY SETTINGS
subprocess.call(['v4l2-ctl', '--set-ctrl', 'sharpness=250'])
subprocess.call(['v4l2-ctl', '--set-ctrl', 'brightness=153'])
subprocess.call(['v4l2-ctl', '--set-ctrl', 'contrast=32'])
subprocess.call(['v4l2-ctl', '--set-ctrl', 'saturation=32'])
subprocess.call(['v4l2-ctl', '--set-ctrl', 'gain=104'])
subprocess.call(['v4l2-ctl', '--set-ctrl', 'exposure_auto=1'])
# time.sleep(4)
subprocess.call(['v4l2-ctl', '--set-ctrl', 'exposure_absolute=168'])
subprocess.call(['v4l2-ctl', '--set-ctrl', 'exposure_auto_priority=0'])
# time.sleep(4)
subprocess.call(['v4l2-ctl', '--set-ctrl', 'focus_absolute=0'])
# time.sleep(4)
subprocess.call(['v4l2-ctl', '--set-ctrl', 'focus_auto=0'])
# time.sleep(4)
subprocess.call(['v4l2-ctl', '--set-ctrl', 'zoom_absolute=1'])

time.sleep(3)

subprocess.call(['v4l2-ctl', '--set-ctrl', 'exposure_absolute=100'])

time.sleep(3)

subprocess.call(['v4l2-ctl', '--set-ctrl', 'exposure_absolute=50'])

time.sleep(2)

raise SystemExit
