"""FFmpeg operations: transcoding, splitting, and conversion."""

import os
import time
from subprocess import Popen, PIPE

from . import CODECS, HAS_TQDM, tqdm
from .utils import parse_ffmpeg_time, get_splitpoints, sanitize


def run_ffmpeg_with_progress(cmd, total_duration, description="Processing", show_progress=True):
    """Run ffmpeg command with progress bar.

    Args:
        cmd: List of command arguments
        total_duration: Total duration in seconds for progress calculation
        description: Description to show in progress bar
        show_progress: Whether to show progress bar (requires tqdm)

    Returns:
        Return code from ffmpeg
    """
    if not show_progress or not HAS_TQDM or total_duration <= 0:
        # Fall back to simple execution
        cmd_str = " ".join([f'"{arg}"' if " " in str(arg) else str(arg) for arg in cmd])
        return os.system(cmd_str.encode("utf-8"))

    # Add progress output to ffmpeg command
    # Insert -progress pipe:1 after ffmpeg
    progress_cmd = cmd.copy()
    # Find where to insert progress args (after 'ffmpeg')
    if progress_cmd[0] == "ffmpeg":
        progress_cmd.insert(1, "-progress")
        progress_cmd.insert(2, "pipe:1")
        # Change loglevel to quiet to avoid mixing output
        for i, arg in enumerate(progress_cmd):
            if arg == "-loglevel":
                progress_cmd[i + 1] = "quiet"
                break

    try:
        process = Popen(progress_cmd, stdout=PIPE, stderr=STDOUT, universal_newlines=True)

        with tqdm(total=100, desc=description, unit="%", ncols=80,
                  bar_format='{desc}: {percentage:3.0f}%|{bar}| [{elapsed}<{remaining}]') as pbar:
            current_progress = 0

            for line in process.stdout:
                line = line.strip()
                if line.startswith("out_time="):
                    time_str = line.split("=")[1]
                    current_time = parse_ffmpeg_time(time_str)
                    new_progress = min(int((current_time / total_duration) * 100), 100)
                    if new_progress > current_progress:
                        pbar.update(new_progress - current_progress)
                        current_progress = new_progress
                elif line == "progress=end":
                    pbar.update(100 - current_progress)
                    break

            process.wait()

        return process.returncode
    except Exception as e:
        # Fall back to simple execution on error
        cmd_str = " ".join([f'"{arg}"' if " " in str(arg) else str(arg) for arg in cmd])
        return os.system(cmd_str.encode("utf-8"))


def split_with_ffmpeg(args, destdir, src, md, cover_file=None):
    """Split non-MP3 files using ffmpeg."""
    chapters = md["chapters"]
    t = md["format"]["tags"]
    ext = CODECS[args.container][1]
    codec = CODECS[args.container][0]
    num_chapters = len(chapters)

    if args.verbose:
        print(f"Splitting {src} into {num_chapters} chapters using ffmpeg")

    # Use tqdm for chapter progress if available and not in verbose mode
    use_chapter_progress = HAS_TQDM and not args.verbose and not args.test
    chapter_iter = chapters
    if use_chapter_progress:
        chapter_iter = tqdm(chapters, desc="Splitting chapters", unit="ch", ncols=80)

    # Check if we can embed cover art (supported in m4a/m4b/mp3, not in flac/opus via this method)
    embed_cover = cover_file and os.path.exists(cover_file) and args.container in ["m4a", "m4b", "aac"]

    success = True
    for i, chapter in enumerate(chapter_iter):
        chapter_num = i + 1
        chapter_title = chapter["tags"].get("title", f"Chapter {chapter_num}")
        start_time = float(chapter["start_time"])
        end_time = float(chapter["end_time"])
        duration = end_time - start_time

        # Sanitize chapter title for filename (replace underscores with spaces for readability)
        safe_title = sanitize(chapter_title).replace("_", " ")
        output_file = os.path.join(destdir, f"{chapter_num:02d} - {safe_title}.{ext}")

        # Build ffmpeg command
        # Note: -ss and -t must come BEFORE -i to apply as input options (faster seeking)
        # and to avoid affecting the cover image input
        cmd = [
            "ffmpeg",
            "-loglevel", "error",
            "-ss", str(start_time),
            "-t", str(duration),
            "-i", src,
        ]

        # Add cover art input if available (no seek options for cover)
        if embed_cover:
            cmd.extend(["-i", cover_file])

        # Map streams: audio from first input, cover from second if available
        if embed_cover:
            cmd.extend([
                "-map", "0:a",
                "-map", "1:v",
                "-c:v", "copy",
                "-disposition:v:0", "attached_pic",
            ])

        cmd.extend([
            "-c:a", codec,
            "-map_metadata", "-1",  # Clear existing metadata
            "-metadata", f'title={chapter_title}',
            "-metadata", f'artist={t.get("artist", "")}',
            "-metadata", f'album={t.get("title", "")}',
            "-metadata", f'album_artist={t.get("album_artist", "")}',
            "-metadata", f'date={t.get("date", "")}',
            "-metadata", f'genre={t.get("genre", "")}',
            "-metadata", f'track={chapter_num}/{num_chapters}',
            output_file
        ])

        if args.verbose or args.test:
            print(f"Chapter {chapter_num}: {chapter_title}")
            print(" ".join(cmd))
            if args.test:
                continue

        # Run ffmpeg quietly when using progress bar
        cmd_str = " ".join([f'"{arg}"' if " " in str(arg) else str(arg) for arg in cmd])
        rv = os.system(cmd_str.encode("utf-8"))
        if rv != 0:
            print(f"Error splitting chapter {chapter_num}")
            success = False

    if success and not args.test and not args.keep:
        os.unlink(src)
    elif success and args.keep and args.verbose:
        print(f"Keeping intermediate file: {src}")


def split_file(args, destdir, src, md, cover_file=None):
    """Split the file into chapters."""
    splitpoints = get_splitpoints(args.container, md)
    t = md["format"]["tags"]
    if args.container == "mp3":
        # Escape special characters in metadata for mp3splt
        artist = t.get("artist", "Unknown").replace('"', '\\"')
        title = t.get("title", "Unknown").replace('"', '\\"')
        date = t.get("date", "")

        cmd = [
            "mp3splt",
            "-T",
            "12",
            "-o",
            '"Chapter @n"',
            "-g",
            f'"r%[@N=1,@a={artist},@b={title},@y={date},@t=Chapter @n,@g=183]"',
            "-d",
            f'"{destdir}"',
            f'"{src}"',
            " ".join(splitpoints),
        ]
        if args.verbose or args.test:
            print(cmd)
            if args.test:
                return
        cmd = " ".join(cmd)
        rv = os.system(cmd.encode("utf-8"))
        if rv == 0 and not args.keep:
            os.unlink(src)
        elif rv == 0 and args.keep and args.verbose:
            print(f"Keeping intermediate file: {src}")
    else:
        # Use ffmpeg for non-MP3 formats (AAC, M4A, FLAC, Opus)
        # M4B is handled separately with chapter embedding
        split_with_ffmpeg(args, destdir, src, md, cover_file)


def convert_file(args, fn, md):
    """Full conversion pipeline for a single AAX file."""
    from .chapters import embed_chapters

    destdir = None
    try:
        destdir = os.path.join(
            args.outdir, md["format"]["tags"]["artist"], md["format"]["tags"]["title"].replace("/", "-")
        )
    except KeyError:
        print(f"Metadata Error in {fn}")
        return
    destdir = sanitize(destdir)

    if not os.path.exists(destdir):
        os.makedirs(destdir)

    # XXX figure out how to hook up decrypt-only, eg:
    # XXX ffmpeg -activation_bytes $AUTHCODE -i input.aax -c:a copy -vn -f mp4 output.mp4
    from json import dump as jdump
    with open(f"{destdir}/metadata.json", "w") as fd:
        jdump(md, fd, sort_keys=True, indent=4, separators=(",", ": "))

    if args.metadata:
        return

    # Extract cover image (will be embedded in chapter files if supported)
    cover_file = os.path.join(destdir, "cover.jpg")
    try:
        args.extract_cover(destdir, fn)
    except Exception:
        cover_file = None

    if args.coverimage:
        return

    if "Chapter " in str(os.listdir(destdir)):
        if args.verbose:
            print(f"Already processed {fn}")
        return

    destfn = fn.replace(".aax", f".{CODECS[args.container][1]}")
    output = os.path.join(destdir, destfn)
    if os.path.exists(output) and args.overwrite:
        print(f"removing transcoded file: {output}")
        os.unlink(output)

    ac = "2"
    ab = md["format"]["bit_rate"]
    if args.mono:
        ac = "1"
        ab = str(int(ab) / 2)

    # Build metadata arguments safely
    tags = md["format"]["tags"]
    metadata_args = []

    # Add metadata fields if they exist
    if "title" in tags:
        metadata_args.extend(["-metadata", f'title={tags["title"]}'])
    if "artist" in tags:
        metadata_args.extend(["-metadata", f'artist={tags["artist"]}'])
    if "album_artist" in tags:
        metadata_args.extend(["-metadata", f'album_artist={tags["album_artist"]}'])
    if "album" in tags:
        metadata_args.extend(["-metadata", f'album={tags["album"]}'])
    if "date" in tags:
        metadata_args.extend(["-metadata", f'date={tags["date"]}'])
    if "genre" in tags:
        metadata_args.extend(["-metadata", f'genre={tags["genre"]}'])
    if "copyright" in tags:
        metadata_args.extend(["-metadata", f'copyright={tags["copyright"]}'])

    metadata_args.extend(["-metadata", "track=1/1"])

    cmd = [
        "ffmpeg",
        "-loglevel",
        "error",
        "-stats",
        "-n",
        "-activation_bytes",
        args.auth,
        "-i",
        fn,
        "-vn",
        "-codec:a",
        CODECS[args.container][0],
        "-ab",
        ab,
        "-ac",
        ac,
        "-map_metadata",
        "-1",
    ] + metadata_args + [output]
    if args.test or args.verbose:
        print(" ".join([f'"{arg}"' if " " in str(arg) else str(arg) for arg in cmd]))
        print("splitpoints:", get_splitpoints(args.container, md))
        if args.test:
            return split_file(args, destdir, output, md, cover_file)

    t = time.time()
    # Get total duration for progress bar
    total_duration = float(md["format"].get("duration", 0))
    title = md["format"]["tags"].get("title", "audio")

    # Use progress bar for transcoding (especially useful for FLAC/Opus encoding)
    show_progress = HAS_TQDM and not args.verbose
    run_ffmpeg_with_progress(cmd, total_duration, f"Transcoding {title}", show_progress)

    t = time.time() - t
    if args.verbose:
        print(f"transcoding time: {t:0.2f}s")
    if args.single == True:
        return

    # M4B natively supports chapters - embed chapters instead of splitting
    if args.container == "m4b":
        embed_chapters(args, destdir, output, md, cover_file)
    else:
        split_file(args, destdir, output, md, cover_file)
