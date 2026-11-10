"""Where this camera is.

The sunrise and sunset times depend on where you are, and nowhere in these
scripts is there a place written down. The location is worked out at startup,
in this order, and the first answer wins:

  1. WEBCAM_LAT and WEBCAM_LON in the environment, for a machine that should
     not be guessing at all - a camera on a tower a county away from the
     computer driving it, say.
  2. The cached answer in ~/.webcam-location.conf, if it is less than a week
     old. Nothing goes out to the network on an ordinary run.
  3. An IP lookup, which gives the city the connection comes out of.
  4. The last cached answer whatever its age, since a stale location beats
     none at all - sunrise moves by about a minute a day, not an hour.

The IP lookup is worth being plain about: it tells a third party (ipinfo.io,
and ip-api.com if that one is not answering) this machine's IP address, and
what comes back is where the internet connection surfaces, not where the
camera is. On a VPN it is wrong by however far the VPN reaches. That is why
the answer is cached, shown, and overridable - and if neither is wanted, set
WEBCAM_LAT and WEBCAM_LON and no lookup is ever made.

Accuracy only has to be rough. A tenth of a degree of latitude moves sunrise
by well under a minute, and these presets switch on offsets of 7 to 122
minutes, so a city-level answer is as good as a surveyed one here.
"""

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

LOCATION_FILE = os.environ.get(
    'WEBCAM_LOCATION_FILE',
    os.path.join(os.path.expanduser('~'), '.webcam-location.conf'))
LOCATION_MAX_AGE = 7 * 24 * 3600        # a week, then look again

LOOKUPS = (
    'https://ipinfo.io/json',
    'http://ip-api.com/json/?fields=status,lat,lon,timezone',
)


def system_timezone():
    """The system's own idea of its time zone, which needs no network."""
    try:
        out = subprocess.run(['timedatectl', 'show', '-p', 'Timezone', '--value'],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        if '/' in out:
            return out
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        with open('/etc/timezone', encoding='utf-8') as handle:
            name = handle.read().strip()
        if '/' in name:
            return name
    except OSError:
        pass
    try:
        link = os.readlink('/etc/localtime')
        found = re.search(r'zoneinfo/(\w+/[\w/+-]+)$', link)
        if found:
            return found.group(1)
    except OSError:
        pass
    return 'UTC'


def save_location(lat, lon, timezone, source):
    try:
        with open(LOCATION_FILE, 'w', encoding='utf-8') as handle:
            handle.write(f'lat={lat}\nlon={lon}\ntz={timezone}\nsource={source}\n'
                         f'saved={time.strftime("%Y-%m-%d %H:%M")}\n')
    except OSError:
        return False
    return True


def load_location():
    """Returns (lat, lon, timezone, source, age in seconds) or None."""
    try:
        values = {}
        with open(LOCATION_FILE, encoding='utf-8') as handle:
            for line in handle:
                found = re.match(r'^(\w+)=(.*?)\s*$', line)
                if found:
                    values[found.group(1)] = found.group(2)
        lat, lon = float(values['lat']), float(values['lon'])
    except (OSError, KeyError, ValueError):
        return None
    age = time.time() - os.path.getmtime(LOCATION_FILE)
    return lat, lon, values.get('tz') or system_timezone(), values.get('source', 'saved'), age


def lookup_online():
    """Ask where this connection comes out. Returns (lat, lon, tz) or None.

    Two services, because one of them is always rate limiting somebody. Short
    timeouts: this runs at startup and must never be what stops the program
    coming up.
    """
    for url in LOOKUPS:
        try:
            with urllib.request.urlopen(url, timeout=6) as response:
                data = json.loads(response.read().decode('utf-8', 'replace'))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            continue

        timezone = data.get('timezone') or system_timezone()
        if data.get('loc'):                       # ipinfo.io
            try:
                lat, lon = (float(part) for part in data['loc'].split(','))
                return lat, lon, timezone
            except ValueError:
                continue
        if data.get('status') == 'success':       # ip-api.com
            try:
                return float(data['lat']), float(data['lon']), timezone
            except (KeyError, TypeError, ValueError):
                continue
    return None


def detect_location(force=False):
    """The whole order above, in one call.

    Returns (lat, lon, timezone, how it was found). lat and lon are None when
    there is no saved location and the lookup could not be made.
    """
    env_lat, env_lon = os.environ.get('WEBCAM_LAT'), os.environ.get('WEBCAM_LON')
    if env_lat and env_lon:
        try:
            return (float(env_lat), float(env_lon),
                    os.environ.get('WEBCAM_TZ') or system_timezone(),
                    'WEBCAM_LAT and WEBCAM_LON')
        except ValueError:
            pass

    saved = load_location()
    if saved and not force and saved[4] < LOCATION_MAX_AGE:
        return saved[0], saved[1], saved[2], 'the saved location'

    found = lookup_online()
    if found:
        save_location(found[0], found[1], found[2], 'ip lookup')
        return found[0], found[1], found[2], 'an IP lookup'

    if saved:
        return (saved[0], saved[1], saved[2],
                'the saved location, which could not be refreshed')

    return None, None, system_timezone(), 'nothing - no saved location and no lookup'
