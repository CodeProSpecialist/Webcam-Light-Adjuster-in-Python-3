import datetime
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from astral.sun import sun
from astral.location import LocationInfo

# Sunrise and sunset: online first, then the shared file.
#
# All three webcam programs now get the times the same way:
#   1. fetch today's times from api.sunrise-sunset.org (free, no key)
#   2. if there is no internet, use the newest times saved in the text file
#   3. if there is no file either, work them out locally with astral, which
#      needs no network
#
# Every successful fetch or calculation is written to the shared file, so the
# ONN settings GUI and the Perl program can fall back on it as well.
LATITUDE = 39.0997
LONGITUDE = -94.5786
TIMEZONE = 'US/Central'
SUN_TZID = 'America/Chicago'
SUN_FILE = os.path.join(os.path.expanduser('~'), '.webcam-sun-times.conf')


def save_sun_file(sunrise_hhmm, sunset_hhmm, source):
    try:
        with open(SUN_FILE, 'w') as fh:
            stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
            fh.write(f'# webcam sunrise/sunset times - written {stamp}\n')
            fh.write(f'sunrise={sunrise_hhmm}\n')
            fh.write(f'sunset={sunset_hhmm}\n')
            fh.write(f'lat={LATITUDE}\n')
            fh.write(f'lng={LONGITUDE}\n')
            fh.write(f'source={source}\n')
        return True
    except OSError:
        return False


def load_sun_file():
    """Return (sunrise_hhmm, sunset_hhmm, written_text) or None."""
    try:
        with open(SUN_FILE) as fh:
            lines = fh.read().splitlines()
    except OSError:
        return None
    kv, written = {}, None
    for line in lines:
        m = re.match(r'^#.*written\s+(.+)$', line)
        if m:
            written = m.group(1)
            continue
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, _, value = line.partition('=')
        kv[key.strip()] = value.strip()
    for key in ('sunrise', 'sunset'):
        if not re.match(r'^\d{1,2}:\d{2}$', kv.get(key, '')):
            return None
    return kv['sunrise'], kv['sunset'], written or 'an unknown date'


def fetch_sun_online():
    """Return (sunrise_hhmm, sunset_hhmm) from the web, or None.

    formatted=0 returns ISO timestamps already converted to the given time
    zone, so the local clock times can be read straight out of the string.
    """
    url = ('https://api.sunrise-sunset.org/json'
           f'?lat={LATITUDE}&lng={LONGITUDE}&tzid={SUN_TZID}&formatted=0')
    try:
        with urllib.request.urlopen(url, timeout=12) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    if data.get('status') != 'OK':
        return None
    out = []
    for key in ('sunrise', 'sunset'):
        m = re.search(r'T(\d{2}):(\d{2})', str(data.get('results', {}).get(key, '')))
        if not m:
            return None
        out.append(f'{m.group(1)}:{m.group(2)}')
    return tuple(out)


def hhmm_to_today(hhmm):
    hour, minute = (int(x) for x in hhmm.split(':'))
    return datetime.datetime.now().replace(
        hour=hour, minute=minute, second=0, microsecond=0)

# Daily time refresh at 04:46, the same minute as the other two webcam programs.
REFRESH_MINUTE = 4 * 60 + 46

# Sunrise and sunset for the day, refreshed at REFRESH_MINUTE.
SUN_CACHE = {'date': None, 'times': None}

# Infinite loop to continuously check the calculated times
while True:
    current_time = datetime.datetime.now().replace(second=0, microsecond=0)

    # Method to calculate sunrise and sunset times using Astral library.
    #
    # The times are worked out once and then reused until the 04:46 refresh, the
    # same minute the other two webcam programs refresh at. 04:46 is before the
    # earliest sunrise of the year, so the new times are always in place before
    # the morning's first light change. Holding them steady through the day also
    # keeps the astral calculation from running on every pass of the loop.
    def get_sunrise_sunset():
        now = datetime.datetime.now()
        today = now.date()
        cached = SUN_CACHE.get('date')
        due = (cached is None
               or (cached != today
                   and now.hour * 60 + now.minute >= REFRESH_MINUTE))
        if not due:
            return SUN_CACHE['times']

        # 1. the internet
        fetched = fetch_sun_online()
        if fetched:
            times = (hhmm_to_today(fetched[0]), hhmm_to_today(fetched[1]))
            save_sun_file(fetched[0], fetched[1], 'online')
            print(f"Fetched sunrise/sunset at {now:%H:%M}: "
                  f"{fetched[0]} / {fetched[1]}")
            SUN_CACHE['date'], SUN_CACHE['times'] = today, times
            return times

        # 2. the newest saved times, which another webcam program may have
        #    written locally today
        saved = load_sun_file()
        if saved:
            times = (hhmm_to_today(saved[0]), hhmm_to_today(saved[1]))
            print(f"No internet; using the times saved {saved[2]}: "
                  f"{saved[0]} / {saved[1]}")
            SUN_CACHE['date'], SUN_CACHE['times'] = today, times
            return times

        # 3. work them out locally, which needs no network
        location = LocationInfo(
            "Kansas City", "Missouri", TIMEZONE,
            latitude=LATITUDE, longitude=LONGITUDE
        )
        s = sun(location.observer, date=today, tzinfo=location.timezone)
        sunrise_time = s["sunrise"].replace(second=0, microsecond=0, tzinfo=None)
        sunset_time = s["sunset"].replace(second=0, microsecond=0, tzinfo=None)
        save_sun_file(f'{sunrise_time:%H:%M}', f'{sunset_time:%H:%M}', 'calculated')
        print("No internet and no saved times; worked them out locally: "
              f"{sunrise_time:%H:%M} / {sunset_time:%H:%M}")
        SUN_CACHE['date'] = today
        SUN_CACHE['times'] = (sunrise_time, sunset_time)
        return SUN_CACHE['times']

        # Define the classes for each subprocess


    class GoldenMorningSharpWebcam:
        def __init__(self):
            self.target_time = get_sunrise_sunset()[0] - datetime.timedelta(minutes=7)   # (minutes = 7)
            self.target_time = self.target_time.replace(second=0, microsecond=0)

        def execute(self):
            subprocess.run(['python3', 'post-sunrise-sharp-webcam.py'])


    class DaySharpWebcam:
        def __init__(self):
            self.target_time = get_sunrise_sunset()[0] + datetime.timedelta(minutes=26)   # (minutes = 26)
            self.target_time = self.target_time.replace(second=0, microsecond=0)

        def execute(self):
            subprocess.run(['python3', 'day-sharp-webcam.py'])


    class BrightSunSharpWebcam:
        def __init__(self):
            self.target_time = get_sunrise_sunset()[0] + datetime.timedelta(minutes=122)    # (minutes = 122)
            self.target_time = self.target_time.replace(second=0, microsecond=0)

        def execute(self):
            subprocess.run(['python3', 'bright-sun-sharp-webcam.py'])


    class DaySharpWebcam2:
        def __init__(self):
            self.target_time = get_sunrise_sunset()[1] - datetime.timedelta(minutes=39)    # (minutes = 39)
            self.target_time = self.target_time.replace(second=0, microsecond=0)

        def execute(self):
            subprocess.run(['python3', 'day-sharp-webcam.py'])


    class PreSunsetSharpWebcam2:
        def __init__(self):
            self.target_time = get_sunrise_sunset()[1] - datetime.timedelta(minutes=10)     # (minutes = 10)
            self.target_time = self.target_time.replace(second=0, microsecond=0)

        def execute(self):
            subprocess.run(['python3', 'pre-sunset-sharp-webcam.py'])


    class NightSharpWebcam:
        def __init__(self):
            self.target_time = get_sunrise_sunset()[1] + datetime.timedelta(minutes=14)  # (minutes = 14)
            self.target_time = self.target_time.replace(second=0, microsecond=0)

        def execute(self):
            subprocess.run(['python3', 'night-sharp-webcam.py'])


    # The legacy Perl repo applies night a second time at sunset + 30, because a
    # camera does not always take the settings on the first attempt. Same here.
    class NightSharpWebcam2:
        def __init__(self):
            self.target_time = get_sunrise_sunset()[1] + datetime.timedelta(minutes=30)  # (minutes = 30)
            self.target_time = self.target_time.replace(second=0, microsecond=0)

        def execute(self):
            subprocess.run(['python3', 'night-sharp-webcam.py'])


    # Create instances of each subprocess class
    golden_morning_sharp_webcam = GoldenMorningSharpWebcam()
    day_sharp_webcam = DaySharpWebcam()
    bright_sun_sharp_webcam = BrightSunSharpWebcam()
    day_sharp_webcam2 = DaySharpWebcam2()
    pre_sunset_sharp_webcam2 = PreSunsetSharpWebcam2()
    night_sharp_webcam = NightSharpWebcam()
    night_sharp_webcam2 = NightSharpWebcam2()

    # Check if the current time matches any of the target times and execute the corresponding subprocess
    if current_time == golden_morning_sharp_webcam.target_time:
        golden_morning_sharp_webcam.execute()
    elif current_time == day_sharp_webcam.target_time:
        day_sharp_webcam.execute()
    elif current_time == bright_sun_sharp_webcam.target_time:
        bright_sun_sharp_webcam.execute()
    elif current_time == day_sharp_webcam2.target_time:
        day_sharp_webcam2.execute()
    elif current_time == pre_sunset_sharp_webcam2.target_time:
        pre_sunset_sharp_webcam2.execute()
    elif current_time == night_sharp_webcam.target_time:
        night_sharp_webcam.execute()
    elif current_time == night_sharp_webcam2.target_time:
        night_sharp_webcam2.execute()

    # code debugging

#    day_sharp_webcam.execute()

    # Print status information
    print(f"Current Time: {current_time}")
    print(f"Sunrise Time: {get_sunrise_sunset()[0]}")
    print(f"Sunset Time: {get_sunrise_sunset()[1]}")
    print(f"Timezone: US/Central")

    print("Golden Morning Sharp Webcam Time: ", golden_morning_sharp_webcam.target_time)

    print("Day Sharp Webcam Time: ", day_sharp_webcam.target_time)

    print("Bright Sun Sharp Webcam Time: ", bright_sun_sharp_webcam.target_time)

    print("End Bright Sun Sharp Webcam Time: ", day_sharp_webcam2.target_time)

    print("Pre-Sunset Sharp Webcam Time: ", pre_sunset_sharp_webcam2.target_time)

    print("Night Sharp Webcam Time: ", night_sharp_webcam.target_time)

    print("Second Night Sharp Webcam Time: ", night_sharp_webcam2.target_time)

    # code debugging

    # Delay between target_time checks (adjust as needed)
    # time.sleep(30) for 30 seconds to check twice per minute
    time.sleep(20)
