"""AAX file probing and cover art extraction."""

import os
from subprocess import check_output, PIPE
from json import loads
import re

from . import CODECS


def probe_metadata(args, fn):
    """Get file metadata, eg. chapters, titles, codecs. Recent version of ffprobe
    can emit json which is ever so helpful."""
    if not os.path.exists(fn):
        print("Derp! Input file does not exist!")
        return None
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-activation_bytes",
        args.auth,
        "-i",
        fn,
        "-of",
        "json",
        "-show_chapters",
        "-show_programs",
        "-show_format",
    ]

    buf = check_output(cmd).decode("utf-8")

    buf = re.sub(r"\s*[(](Una|A)bridged[)]", "", buf)  # I don't care about abridged or not
    buf = re.sub(r"\s+", " ", buf)  # squish all whitespace runs

    ffprobe = loads(buf)
    return ffprobe


def extract_image(args, destdir, fn):
    """Extract cover image from an AAX file."""
    output = os.path.join(destdir, "cover.jpg")
    cmd = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-stats",
        "-activation_bytes",
        args.auth,
        "-n",
        "-i",
        fn,
        "-an",
        "-codec:v",
        "copy",
        f"{output}",
    ]
    if os.path.exists(output) and args.overwrite:
        os.unlink(output)

    if args.test or args.verbose:
        print("extracting cover art")
        print(" ".join(cmd))
    if not args.test:
        check_output(cmd)
