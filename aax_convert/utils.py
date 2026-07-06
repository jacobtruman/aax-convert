"""Utility functions for aax-convert."""

import re
from unicodedata import normalize


def sanitize(s):
    """Replace any unsafe characters with underscores."""
    s = normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii", "ignore")
    s = s.replace("'", "").replace('"', "")
    s = re.sub("[^a-zA-Z0-9._/-]", "_", s)
    s = re.sub("_+", "_", s)
    return s


def parse_exclude_chapters(exclude_str):
    """Parse exclude-chapters string into a set of global chapter numbers.

    Accepts comma-separated numbers and ranges: '1,2,5,10-15'
    """
    if not exclude_str:
        return set()
    excluded = set()
    for part in exclude_str.split(","):
        part = part.strip()
        if "-" in part:
            try:
                start, end = part.split("-", 1)
                for i in range(int(start), int(end) + 1):
                    excluded.add(i)
            except ValueError:
                pass
        else:
            try:
                excluded.add(int(part))
            except ValueError:
                pass
    return excluded


def parse_ffmpeg_time(time_str):
    """Parse ffmpeg time string (HH:MM:SS.ms) to seconds."""
    try:
        parts = time_str.split(':')
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return float(hours) * 3600 + float(minutes) * 60 + float(seconds)
        elif len(parts) == 2:
            minutes, seconds = parts
            return float(minutes) * 60 + float(seconds)
        else:
            return float(time_str)
    except (ValueError, AttributeError):
        return 0


def numfix(n):
    """Convert the number of seconds into the format that mp3splt prefers."""
    n = float(n)
    m = int(n / 60)
    s = n - (m * 60)
    return f"{m}.{s:.2f}"


def get_splitpoints(container, md):
    """Figure out where mp3splt should split the file."""
    splitpoints = [float(x["start_time"]) for x in md["chapters"]]
    if container == "mp3":
        splitpoints.append(md["chapters"][-1]["end_time"])
        splitpoints = [numfix(x) for x in splitpoints]
    return splitpoints
