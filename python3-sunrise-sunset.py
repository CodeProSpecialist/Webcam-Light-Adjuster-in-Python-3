import datetime
import subprocess
import time
from astral.sun import sun
from astral.location import LocationInfo

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

        # Create a location object with latitude, longitude, and timezone values
         # latitude + , longitude -
        location = LocationInfo(
            "Kansas City", "Missouri", "US/Central",
            latitude=39.0997, longitude=-94.5786   
        )          

        # Calculate sunrise and sunset times for today
        s = sun(location.observer, date=today, tzinfo=location.timezone)
        sunrise_time = s["sunrise"].replace(second=0, microsecond=0, tzinfo=None)
        sunset_time = s["sunset"].replace(second=0, microsecond=0, tzinfo=None)

        SUN_CACHE['date'] = today
        SUN_CACHE['times'] = (sunrise_time, sunset_time)
        print(f"Refreshed sunrise/sunset at {now:%H:%M}: "
              f"{sunrise_time:%H:%M} / {sunset_time:%H:%M}")
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
