import datetime
import subprocess
import time
from astral.sun import sun
from astral.location import LocationInfo

# Infinite loop to continuously check the calculated times
while True:
    current_time = datetime.datetime.now().replace(second=0, microsecond=0)


    # Method to calculate sunrise and sunset times using Astral library
    def get_sunrise_sunset():
        # Create a location object with latitude, longitude, and timezone values
        location = LocationInfo(
            "Emporia", "Kansas", "US/Central",
            latitude=38.4039, longitude=-96.1817
        )

        # Calculate sunrise and sunset times for today
        s = sun(location.observer, date=datetime.date.today(), tzinfo=location.timezone)
        sunrise_time = s["sunrise"].replace(second=0, microsecond=0, tzinfo=None)
        sunset_time = s["sunset"].replace(second=0, microsecond=0, tzinfo=None)

        return sunrise_time, sunset_time

        # Define the classes for each subprocess


    class PreSunsetSharpWebcam:
        def __init__(self):
            self.target_time = get_sunrise_sunset()[0] - datetime.timedelta(minutes=7)
            self.target_time = self.target_time.replace(second=0, microsecond=0)

        def execute(self):
            subprocess.run(['python3', 'pre-sunset-sharp-webcam.py'])


    class DaySharpWebcam:
        def __init__(self):
            self.target_time = get_sunrise_sunset()[0] + datetime.timedelta(minutes=26)
            self.target_time = self.target_time.replace(second=0, microsecond=0)

        def execute(self):
            subprocess.run(['python3', 'day-sharp-webcam.py'])


    class BrightSunSharpWebcam:
        def __init__(self):
            self.target_time = get_sunrise_sunset()[0] + datetime.timedelta(minutes=122)
            self.target_time = self.target_time.replace(second=0, microsecond=0)

        def execute(self):
            subprocess.run(['python3', 'bright-sun-sharp-webcam.py'])


    class DaySharpWebcam2:
        def __init__(self):
            self.target_time = get_sunrise_sunset()[1] - datetime.timedelta(minutes=39)
            self.target_time = self.target_time.replace(second=0, microsecond=0)

        def execute(self):
            subprocess.run(['python3', 'day-sharp-webcam.py'])


    class PreSunsetSharpWebcam2:
        def __init__(self):
            self.target_time = get_sunrise_sunset()[1] - datetime.timedelta(minutes=10)
            self.target_time = self.target_time.replace(second=0, microsecond=0)

        def execute(self):
            subprocess.run(['python3', 'pre-sunset-sharp-webcam.py'])


    class NightSharpWebcam:
        def __init__(self):
            self.target_time = get_sunrise_sunset()[1] + datetime.timedelta(minutes=14)
            self.target_time = self.target_time.replace(second=0, microsecond=0)

        def execute(self):
            subprocess.run(['python3', 'night-sharp-webcam.py'])


    # Create instances of each subprocess class
    pre_sunset_sharp_webcam = PreSunsetSharpWebcam()
    day_sharp_webcam = DaySharpWebcam()
    bright_sun_sharp_webcam = BrightSunSharpWebcam()
    day_sharp_webcam2 = DaySharpWebcam2()
    pre_sunset_sharp_webcam2 = PreSunsetSharpWebcam2()
    night_sharp_webcam = NightSharpWebcam()

    # Check if the current time matches any of the target times and execute the corresponding subprocess
    if current_time == pre_sunset_sharp_webcam.target_time:
        pre_sunset_sharp_webcam.execute()
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

    # code debugging

    # day_sharp_webcam.execute()

    # Print status information
    print(f"Current Time: {current_time}")
    print(f"Sunrise Time: {get_sunrise_sunset()[0]}")
    print(f"Sunset Time: {get_sunrise_sunset()[1]}")
    print(f"Timezone: US/Central")

    print("Pre-Sunrise Sharp Webcam Time: ", pre_sunset_sharp_webcam.target_time)

    print("Day Sharp Webcam Time: ", day_sharp_webcam.target_time)

    print("Bright Sun Sharp Webcam Time: ", bright_sun_sharp_webcam.target_time)

    print("End Bright Sun Sharp Webcam Time: ", day_sharp_webcam2.target_time)

    print("Pre-Sunset Sharp Webcam Time: ", pre_sunset_sharp_webcam2.target_time)

    print("Night Sharp Webcam Time: ", night_sharp_webcam.target_time)

    # code debugging

    # Delay between target_time checks (adjust as needed)
    # time.sleep(30) for 30 seconds to check twice per minute
    time.sleep(30)
