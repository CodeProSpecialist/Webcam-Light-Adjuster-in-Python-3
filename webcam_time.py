"""Keeping time.

Everything in these scripts is driven by the clock: the sunrise and sunset
offsets, the 04:46 refresh, which preset belongs to this minute. A computer
whose clock has drifted applies the night preset in daylight and nobody
notices until the recording is useless.

So the real time is fetched at startup and once a day after that, and the
difference between it and this machine's clock is kept as an offset. Every
decision should go through now() here rather than datetime.datetime.now().

The system clock itself is NOT changed. That needs root, and a camera program
is no business asking for it - Ubuntu already runs systemd-timesyncd, and if
this reports a large offset for days on end that is the thing to look at
(`timedatectl status`) rather than something for this program to override.

Being offline is not a failure: the fetch simply does not answer, the previous
offset stays, and the scripts carry on with the system clock as they always
did. The fetch is also how "are we connected" is answered - no separate check,
because a check that passes while the time server is unreachable would tell us
nothing useful.
"""

import datetime
import email.utils
import json
import time
import urllib.error
import urllib.request

REFRESH = 24 * 3600          # once a day

# The Date header of any HTTPS response is the first choice: every web server
# sends one, it needs no API and no key, and it cannot be rate limited away.
# Two hosts, in case one is unreachable, then a time service as a third.
DATE_HOSTS = ('https://www.google.com', 'https://www.cloudflare.com')
TIME_API = 'https://timeapi.io/api/Time/current/zone?timeZone=UTC'

_offset = 0.0                # seconds to add to this machine's clock
_how = 'not checked yet'
_last_try = 0.0


def fetch_network_time():
    """What time the internet thinks it is, in epoch seconds, or None when
    there is no answer - which is also what being offline looks like."""
    for host in DATE_HOSTS:
        try:
            request = urllib.request.Request(host, method='HEAD')
            with urllib.request.urlopen(request, timeout=6) as response:
                header = response.headers.get('Date')
            if header:
                stamp = email.utils.parsedate_to_datetime(header)
                if stamp is not None:
                    return stamp.timestamp()
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            continue

    try:
        with urllib.request.urlopen(TIME_API, timeout=6) as response:
            data = json.loads(response.read().decode('utf-8', 'replace'))
        stamp = datetime.datetime(
            int(data['year']), int(data['month']), int(data['day']),
            int(data['hour']), int(data['minute']), int(data['seconds']),
            tzinfo=datetime.timezone.utc)
        return stamp.timestamp()
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, TimeoutError):
        return None


def sync(log=None):
    """Fetch the real time and remember how far off this machine is. Returns
    the offset in seconds, or None when there was no answer."""
    global _offset, _how, _last_try
    _last_try = time.time()

    real = fetch_network_time()
    if real is None:
        _how = "no answer from a time server - using this machine's clock"
        return None

    was = _offset
    _offset = real - time.time()
    if abs(_offset) < 1:
        _how = "this machine's clock agrees with the time server"
    else:
        _how = (f"this machine's clock is {abs(_offset):.0f} seconds "
                f"{'slow' if _offset > 0 else 'fast'}, corrected in this program")

    # Worth a line in the log when it matters: a few seconds is nothing, half a
    # minute will start moving the preset changes about.
    if log and (abs(_offset) >= 10 or abs(_offset - was) >= 10):
        log(f'time check: {_how}')
    return _offset


def epoch():
    """The corrected time, in epoch seconds."""
    return time.time() + _offset


def now():
    """The corrected time, as a local datetime - what everything here should
    use in place of datetime.datetime.now()."""
    return datetime.datetime.fromtimestamp(epoch())


def check_due(log=None):
    """Re-check once a day. Call it as often as you like; it does nothing
    until the day is up, so it can live inside a loop that runs every
    second."""
    if _last_try and (time.time() - _last_try) < REFRESH:
        return False
    sync(log=log)
    return True


def status():
    return _how
