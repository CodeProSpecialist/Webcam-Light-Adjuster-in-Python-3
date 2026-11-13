#!/usr/bin/env python3
"""The archive: an index of every recorded segment, and clips cut out of it.

Two jobs.

The index. A month of five-minute segments is eight thousand files, and
answering "what was on camera last Tuesday between 9 and 11 at night" by
walking eight thousand files and ffprobing each one takes long enough to feel
broken. So every segment goes into a small SQLite table once - start time from
its name, duration from one ffprobe, size from the filesystem - and searches
run against the table. Re-scanning is cheap because a segment already in the
table is skipped unless its size changed.

The clips. "Download this bit of the archive" almost never lines up with the
segment boundaries, and the bit wanted usually spans two or three files. So a
clip is cut by absolute wall-clock time, not by file: the index finds every
segment that overlaps the wanted span, each one is trimmed to its overlapping
part, and the pieces are concatenated into one mp4. Where the span sits inside
a single segment it is a stream copy, which is instant and lossless.

    python3 webcam_archive.py scan
    python3 webcam_archive.py search --date 2026-11-12 --from 21:00 --to 23:00
    python3 webcam_archive.py clip "2026-11-12 21:35:00" "2026-11-12 21:41:30"
    python3 webcam_archive.py stats
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time

try:
    import webcam_bootstrap
except ImportError:
    pass
else:
    webcam_bootstrap.ensure('ffmpeg')

try:
    from webcam_errors import log_error
except Exception:                                                 # noqa: BLE001
    def log_error(text):
        print(text, file=sys.stderr)

try:
    import webcam_record
    ARCHIVE_DIR = webcam_record.read_settings()['archive_dir']
    CLIPS_DIR = webcam_record.CLIPS_DIR
    SEGMENT_RE = webcam_record.SEGMENT_RE
except Exception:                                                 # noqa: BLE001
    HOME = os.path.expanduser('~')
    ARCHIVE_DIR = os.path.join(HOME, 'Videos', 'webcam-archive')
    CLIPS_DIR = os.path.join(HOME, 'Videos', 'webcam-clips')
    SEGMENT_RE = re.compile(r'^(\d{4})-(\d\d)-(\d\d)_(\d\d)-(\d\d)-(\d\d)\.mp4$')

DB_FILE = os.environ.get(
    'WEBCAM_ARCHIVE_DB',
    os.path.join(os.path.expanduser('~'), '.config', 'webcam', 'archive.db'))

SCHEMA = """
CREATE TABLE IF NOT EXISTS segments (
    path      TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    day       TEXT NOT NULL,      -- YYYY-MM-DD, for the date filter
    clock     TEXT NOT NULL,      -- HH:MM:SS, for the time-of-day filter
    started   REAL NOT NULL,      -- unix seconds, from the file name
    duration  REAL NOT NULL,      -- seconds, from ffprobe
    ended     REAL NOT NULL,
    size      INTEGER NOT NULL,
    camera    INTEGER DEFAULT 1,  -- which camera recorded it, 1..4
    note      TEXT DEFAULT ''     -- yours: typed against a segment in the window
);
CREATE INDEX IF NOT EXISTS seg_started ON segments(started);
CREATE INDEX IF NOT EXISTS seg_day ON segments(day);
CREATE INDEX IF NOT EXISTS seg_camera ON segments(camera);
"""

# An index written before there were several cameras has no camera column. It
# is added rather than the table being rebuilt, so an index of eight thousand
# segments is not thrown away and re-probed over an upgrade.
MIGRATIONS = (
    'ALTER TABLE segments ADD COLUMN camera INTEGER DEFAULT 1',
)


def connect():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for statement in MIGRATIONS:
        try:
            conn.execute(statement)
        except sqlite3.OperationalError:
            pass            # already there, which is the normal case
    conn.commit()
    return conn


def camera_of(path):
    """Which camera a segment came from, read off its folder.

    Segments sitting loose in the archive root are camera 1 - that is where a
    single camera writes, and where every segment recorded before there were
    several cameras still lives.
    """
    folder = os.path.basename(os.path.dirname(os.path.abspath(path)))
    match = re.match(r'^cam(\d+)$', folder)
    return int(match.group(1)) if match else 1


# ---- reading a segment ----

def start_from_name(name):
    """The start time off the file name. This is the whole reason the recorder
    writes -strftime names: no file has to be opened to be placed in time."""
    match = SEGMENT_RE.match(os.path.basename(name))
    if not match:
        return None
    try:
        return time.mktime((
            int(match.group(1)), int(match.group(2)), int(match.group(3)),
            int(match.group(4)), int(match.group(5)), int(match.group(6)),
            0, 0, -1))
    except (ValueError, OverflowError):
        return None


def probe_duration(path):
    """Seconds, from ffprobe. Zero when the file is still being written or is
    broken - a zero-duration row is kept rather than dropped, so that the
    segment still shows in a listing and gets its duration on the next scan."""
    try:
        done = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=nw=1:nk=1', path],
            capture_output=True, text=True, timeout=30)
        return max(0.0, float((done.stdout or '0').strip() or 0))
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0.0


def scan(folder=None, verbose=False, reprobe=False):
    """Bring the index up to date with what is on disk.

    Three things happen: new files are added, files whose size has changed
    (the newest segment, while it grows) are re-probed, and rows whose file
    has gone - deleted by the janitor - are dropped.
    """
    folder = folder or ARCHIVE_DIR
    conn = connect()
    known = {row['path']: (row['size'], row['duration'])
             for row in conn.execute('SELECT path, size, duration FROM segments')}
    seen = set()
    added = updated = 0

    for root, _dirs, files in os.walk(folder):
        for name in sorted(files):
            if not SEGMENT_RE.match(name):
                continue
            path = os.path.join(root, name)
            started = start_from_name(name)
            if started is None:
                continue
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            seen.add(path)
            was = known.get(path)
            if was and was[0] == size and was[1] > 0 and not reprobe:
                continue
            duration = probe_duration(path)
            stamp = time.localtime(started)
            conn.execute(
                'INSERT INTO segments (path, name, day, clock, started, '
                'duration, ended, size, camera, note) '
                'VALUES (?,?,?,?,?,?,?,?,?, '
                'COALESCE((SELECT note FROM segments WHERE path=?), "")) '
                'ON CONFLICT(path) DO UPDATE SET size=excluded.size, '
                'duration=excluded.duration, ended=excluded.ended, '
                'camera=excluded.camera',
                (path, name, time.strftime('%Y-%m-%d', stamp),
                 time.strftime('%H:%M:%S', stamp), started, duration,
                 started + duration, size, camera_of(path), path))
            if was:
                updated += 1
            else:
                added += 1
            if verbose:
                print(f'{"updated" if was else "added  "} {name} '
                      f'{duration:.0f}s {size / 1e6:.0f} MB')

    gone = [path for path in known if path not in seen]
    for path in gone:
        conn.execute('DELETE FROM segments WHERE path=?', (path,))
    conn.commit()
    conn.close()
    if verbose:
        print(f'{added} added, {updated} updated, {len(gone)} gone')
    return {'added': added, 'updated': updated, 'removed': len(gone)}


# ---- searching ----

def search(day_from=None, day_to=None, time_from=None, time_to=None,
           text='', min_seconds=0, limit=2000, newest_first=True,
           camera=None):
    """The index search mode.

    Date range and time-of-day range are separate on purpose, because the
    question is usually "between nine and eleven at night, across the last
    week" rather than one continuous span - and a time-of-day range that wraps
    midnight (22:00 to 02:00) is handled as the two pieces it really is.
    """
    conn = connect()
    where = []
    args = []
    if day_from:
        where.append('day >= ?')
        args.append(day_from)
    if day_to:
        where.append('day <= ?')
        args.append(day_to)
    if time_from and time_to:
        if time_from <= time_to:
            where.append('clock >= ? AND clock <= ?')
            args += [time_from, time_to]
        else:
            where.append('(clock >= ? OR clock <= ?)')
            args += [time_from, time_to]
    elif time_from:
        where.append('clock >= ?')
        args.append(time_from)
    elif time_to:
        where.append('clock <= ?')
        args.append(time_to)
    if min_seconds:
        where.append('duration >= ?')
        args.append(float(min_seconds))
    if camera:
        where.append('camera = ?')
        args.append(int(camera))
    if text:
        where.append('(name LIKE ? OR note LIKE ? OR path LIKE ?)')
        like = f'%{text}%'
        args += [like, like, like]
    sql = 'SELECT * FROM segments'
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += f" ORDER BY started {'DESC' if newest_first else 'ASC'} LIMIT ?"
    args.append(int(limit))
    rows = [dict(row) for row in conn.execute(sql, args)]
    conn.close()
    return rows


def overlapping(start, end, camera=1):
    """Every segment of one camera covering any part of a wall-clock span.

    One camera, because a clip is one view: joining two cameras' segments by
    time would cut between views every few minutes, which is not footage
    anybody can watch.

    The duration > 0 test keeps the segment currently being written out of a
    clip: its mp4 has no index yet and trimming it produces nothing playable.
    """
    conn = connect()
    rows = [dict(row) for row in conn.execute(
        'SELECT * FROM segments WHERE duration > 0 AND camera = ? AND '
        'ended > ? AND started < ? ORDER BY started ASC',
        (int(camera), float(start), float(end)))]
    conn.close()
    return rows


def set_note(path, note):
    conn = connect()
    conn.execute('UPDATE segments SET note=? WHERE path=?', (note, path))
    conn.commit()
    conn.close()


def stats():
    conn = connect()
    row = conn.execute(
        'SELECT COUNT(*) n, COALESCE(SUM(size),0) bytes, '
        'COALESCE(SUM(duration),0) secs, MIN(started) first, MAX(ended) last '
        'FROM segments').fetchone()
    days = conn.execute('SELECT COUNT(DISTINCT day) d FROM segments').fetchone()
    cams = conn.execute('SELECT camera, COUNT(*) n FROM segments '
                        'GROUP BY camera ORDER BY camera').fetchall()
    conn.close()
    return {
        'segments': row['n'],
        'gigabytes': round(row['bytes'] / (1024.0 ** 3), 2),
        'hours': round(row['secs'] / 3600.0, 1),
        'days': days['d'],
        'first': (time.strftime('%Y-%m-%d %H:%M:%S',
                                time.localtime(row['first']))
                  if row['first'] else ''),
        'last': (time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(row['last']))
                 if row['last'] else ''),
        'cameras': {int(entry['camera']): entry['n'] for entry in cams},
    }


# ---- cutting a clip out ----

def parse_when(text):
    """"2026-11-12 21:35:00", "21:35" for today, or a unix number."""
    text = str(text).strip()
    try:
        return float(text) if float(text) > 100000 else None
    except ValueError:
        pass
    for pattern in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return time.mktime(time.strptime(text, pattern))
        except ValueError:
            continue
    for pattern in ('%H:%M:%S', '%H:%M'):
        try:
            piece = time.strptime(text, pattern)
        except ValueError:
            continue
        now = time.localtime()
        return time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                            piece.tm_hour, piece.tm_min, piece.tm_sec,
                            0, 0, -1))
    raise ValueError(f'cannot read a time out of {text!r}')


# What a clip can be exported as. mp4 first and by default: it plays in every
# browser, every phone, Windows, a TV stick and anything that opens a video at
# all, and it is what the archive already holds, so writing it costs nothing
# but the copy.
EXPORT_FORMATS = (
    ('mp4', '.mp4', 'MP4 - no re-encode, instant, plays everywhere'),
    ('mp4-reencode', '.mp4', 'MP4 - re-encoded, exact trim, slower'),
    ('mkv', '.mkv', 'MKV - no re-encode, survives a bad segment'),
    ('avi', '.avi', 'AVI - no re-encode, for older players'),
)


def format_for(label):
    """The export key behind whatever the window is showing."""
    for key, extension, text in EXPORT_FORMATS:
        if label in (key, text):
            return key, extension
    return 'mp4', '.mp4'


def clip(start, end, out_path=None, progress=None, export='mp4', camera=1):
    """One video covering start..end, cut from however many segments it spans.

    Four exports, and the difference between them is worth knowing:

    mp4 and mkv and avi copy the recorded h264 through untouched, so they take
    a second whatever the clip's length and lose nothing at all. The cost is
    that a copy can only start on a keyframe, so the clip can open up to two
    seconds before the time asked for - with the recorder's two-second
    keyframe spacing, never more than that.

    mp4-reencode re-encodes, which starts on the exact frame asked for and
    costs roughly real time to write. Worth it when the moment matters, when
    the clip is going somewhere fussy about its first frame, or when a copied
    clip opened on a frozen picture.
    """
    start = float(start)
    end = float(end)
    if end <= start:
        raise ValueError('the clip ends before it starts')
    rows = overlapping(start, end, camera)
    if not rows:
        raise ValueError(f'camera {camera} has nothing recorded over that '
                         'time - try Rescan, widen the span, or pick the '
                         'camera that was watching')

    os.makedirs(CLIPS_DIR, exist_ok=True)
    if not out_path:
        out_path = os.path.join(CLIPS_DIR, 'clip-cam%d-%s-to-%s%s' % (
            int(camera),
            time.strftime('%Y%m%d-%H%M%S', time.localtime(start)),
            time.strftime('%H%M%S', time.localtime(end)),
            format_for(export)[1]))

    def say(text):
        if progress:
            try:
                progress(text)
            except Exception:                                     # noqa: BLE001
                pass

    if len(rows) == 1:
        row = rows[0]
        offset = max(0.0, start - row['started'])
        length = min(end, row['ended']) - max(start, row['started'])
        say(f"trimming {row['name']}"
            + (' and re-encoding' if export == 'mp4-reencode' else ''))
        done = _ffmpeg(['-ss', f'{offset:.3f}', '-i', row['path'],
                        '-t', f'{length:.3f}'] + _codec(export)
                       + ['-avoid_negative_ts', 'make_zero', out_path])
        if not done:
            raise RuntimeError('ffmpeg could not write the clip - see the '
                               'error log')
        return out_path

    work = tempfile.mkdtemp(prefix='webcam-clip-')
    pieces = []
    try:
        for index, row in enumerate(rows):
            piece_start = max(start, row['started'])
            piece_end = min(end, row['ended'])
            offset = piece_start - row['started']
            length = piece_end - piece_start
            if length <= 0.05:
                continue
            # The pieces are always copied, however the clip is being
            # exported: re-encoding each piece and then joining them would
            # encode the same frames twice and show a seam at every join. Any
            # re-encode happens once, at the join below.
            piece = os.path.join(work, f'part{index:03d}.mp4')
            say(f"trimming {row['name']} ({index + 1} of {len(rows)})")
            if _ffmpeg(['-ss', f'{offset:.3f}', '-i', row['path'],
                        '-t', f'{length:.3f}', '-c', 'copy',
                        '-avoid_negative_ts', 'make_zero', piece]):
                pieces.append(piece)
        if not pieces:
            raise RuntimeError('nothing could be trimmed out of those segments')
        if len(pieces) == 1 and export != 'mp4-reencode':
            os.replace(pieces[0], out_path)
            return out_path
        listing = os.path.join(work, 'parts.txt')
        with open(listing, 'w', encoding='utf-8') as handle:
            for piece in pieces:
                handle.write(f"file '{piece}'\n")
        say(f'joining {len(pieces)} pieces'
            + (' and re-encoding' if export == 'mp4-reencode' else ''))
        if not _ffmpeg(['-f', 'concat', '-safe', '0', '-i', listing]
                       + _codec(export) + [out_path]):
            raise RuntimeError('the pieces were cut but would not join - see '
                               'the error log')
        return out_path
    finally:
        try:
            for name in os.listdir(work):
                os.remove(os.path.join(work, name))
            os.rmdir(work)
        except OSError:
            pass


def _codec(export):
    """What to hand ffmpeg for each export. Everything but mp4-reencode is a
    straight copy - the container changes, the video does not."""
    if export == 'mp4-reencode':
        return ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
                '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-an']
    return ['-c', 'copy']


def _ffmpeg(args):
    try:
        done = subprocess.run(
            ['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y']
            + args, capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.SubprocessError) as problem:
        log_error(f'ffmpeg: {problem}')
        return False
    if done.returncode != 0:
        log_error(f"ffmpeg: {(done.stderr or '').strip()[:400]}")
        return False
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = parser.add_subparsers(dest='action')

    scanner = sub.add_parser('scan', help='index what is on disk')
    scanner.add_argument('--reprobe', action='store_true',
                         help='ffprobe every segment again, not just new ones')

    finder = sub.add_parser('search', help='search the index')
    finder.add_argument('--date', help='a single day, YYYY-MM-DD')
    finder.add_argument('--from-date', dest='day_from')
    finder.add_argument('--to-date', dest='day_to')
    finder.add_argument('--from', dest='time_from', help='time of day HH:MM')
    finder.add_argument('--to', dest='time_to', help='time of day HH:MM')
    finder.add_argument('--text', default='')
    finder.add_argument('--min-seconds', type=float, default=0)
    finder.add_argument('--limit', type=int, default=200)
    finder.add_argument('--camera', type=int, help='1 to 4')
    finder.add_argument('--json', action='store_true')

    cutter = sub.add_parser('clip', help='cut a clip out by wall-clock time')
    cutter.add_argument('start')
    cutter.add_argument('end')
    cutter.add_argument('--out')
    cutter.add_argument('--camera', type=int, default=1, help='1 to 4')
    cutter.add_argument('--format', default='mp4',
                        choices=[key for key, _e, _t in EXPORT_FORMATS],
                        help='mp4 (default), mp4-reencode, mkv or avi')

    sub.add_parser('stats', help='what the archive holds')

    args = parser.parse_args(argv)
    action = args.action or 'stats'

    if action == 'scan':
        scan(verbose=True, reprobe=args.reprobe)
        return 0

    if action == 'stats':
        for key, value in stats().items():
            print(f'{key:10} {value}')
        return 0

    if action == 'search':
        day_from = args.day_from or args.date
        day_to = args.day_to or args.date
        rows = search(day_from, day_to, _hhmm(args.time_from),
                      _hhmm(args.time_to), args.text, args.min_seconds,
                      args.limit, camera=args.camera)
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        if not rows:
            print('nothing in the index matches that - run scan first?')
            return 0
        for row in rows:
            print(f"cam{row.get('camera', 1)}  "
                  f"{row['day']} {row['clock']}  {row['duration']:6.0f}s  "
                  f"{row['size'] / 1e6:7.0f} MB  {row['name']}"
                  + (f"  {row['note']}" if row['note'] else ''))
        print(f'{len(rows)} segment(s)')
        return 0

    if action == 'clip':
        out = clip(parse_when(args.start), parse_when(args.end), args.out,
                   progress=print, export=args.format, camera=args.camera)
        print(f'wrote {out}  ({os.path.getsize(out) / 1e6:.0f} MB)')
        return 0

    parser.print_help()
    return 1


def _hhmm(text):
    """HH:MM typed in becomes HH:MM:SS, because the column holds seconds and
    a string compare of '21:00' against '21:00:00' is not equal."""
    if not text:
        return None
    parts = str(text).strip().split(':')
    while len(parts) < 3:
        parts.append('00')
    return ':'.join(f'{int(part):02d}' for part in parts[:3])


if __name__ == '__main__':
    sys.exit(main())
