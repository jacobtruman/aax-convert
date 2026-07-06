#!/usr/bin/env python3
# vim: tabstop=4:softtabstop=4:shiftwidth=4:expandtab:
# -*- coding: utf-8 -*-

import os
from subprocess import check_output, Popen, PIPE, STDOUT, run as subrun
import re
import argparse
from json import loads
from json import dump as jdump
import shutil
import time
from unicodedata import normalize

try:
    import multiprocessing
except ImportError:
    multiprocessing = None

try:
    from setproctitle import setproctitle
except ImportError:

    def setproctitle(x):
        pass

try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False
    tqdm = None


args = None

codecs = {  # codec, ext, container
    "mp3": ["libmp3lame", "mp3", "mp3"],
    "aac": ["copy", "m4a", "m4a"],
    "m4a": ["copy", "m4a", "m4a"],
    "m4b": ["copy", "m4a", "m4b"],
    "flac": ["flac", "flac", "flac"],
    "opus": ["libopus", "opus", "opus"],
}


def parse_ffmpeg_time(time_str):
    """Parse ffmpeg time string (HH:MM:SS.ms) to seconds"""
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


def run_ffmpeg_with_progress(cmd, total_duration, description="Processing", show_progress=True):
    """
    Run ffmpeg command with progress bar.

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


def check_missing_authcode(args):
    """ensure that an authcode is available"""
    if args.auth:
        return False

    tmp = os.environ.get("AUTHCODE", None)
    if tmp:
        args.auth = tmp
        return False

    for f in [".authcode", "~/.authcode"]:
        f = os.path.expanduser(f)
        if os.path.exists(f):
            with open(f) as fd:
                args.auth = fd.read().strip()
                return False
    print('authcode not found in ".authcode", "~/.authcode", "$AUTHCODE", or the command line')
    return True


def missing_required_programs(args):
    """ensure that various dependencies are available"""
    error = False
    required = ["ffmpeg", "ffprobe"]

    # mp3splt is only needed for MP3 format AND when actually converting (not just extracting metadata/cover)
    # M4B uses ffmpeg chapter embedding instead of mp3splt
    if args.container == "mp3" and not args.metadata and not args.coverimage:
        required.append("mp3splt")

    for p in required:
        try:
            check_output(["which", p])
        except Exception:
            error = True
            print(f"missing dependency - {p}")
    return error


def numfix(n):
    """convert the number of seconds into the format that mp3splt prefers"""
    n = float(n)
    m = int(n / 60)
    s = n - (m * 60)
    return f"{m}.{s:.2f}"


def get_splitpoints(container, md):
    """figure out where mp3splt should split the file"""
    splitpoints = [float(x["start_time"]) for x in md["chapters"]]
    if container == "mp3":
        splitpoints.append(
            md["chapters"][-1]["end_time"]
        )  # mp3splt needs to know the end of the split. it can't assume EOF
        splitpoints = [numfix(x) for x in splitpoints]

    return splitpoints


def probe_metadata(args, fn):
    """
    get file metadata, eg. chapters, titles, codecs. Recent version of ffprobe
    can emit json which is ever so helpful
    """
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


def split_with_ffmpeg(args, destdir, src, md, cover_file=None):
    """Split non-MP3 files using ffmpeg"""
    chapters = md["chapters"]
    t = md["format"]["tags"]
    ext = codecs[args.container][1]
    codec = codecs[args.container][0]
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
    """Split the file into chapters"""
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


def embed_chapters(args, destdir, src, md, cover_file=None):
    """Embed chapters into a single M4B file using ffmpeg"""
    chapters = md["chapters"]
    t = md["format"]["tags"]
    codec = codecs[args.container][0]
    num_chapters = len(chapters)

    # Build chapter metadata for ffmpeg
    chapter_metadata = []
    for i, chapter in enumerate(chapters):
        chapter_title = chapter["tags"].get("title", f"Chapter {i + 1}")
        start_time = float(chapter["start_time"])
        chapter_metadata.append((chapter_title, start_time))

    if args.verbose:
        print(f"Embedding {num_chapters} chapters into single file")

    # Build ffmpeg command for embedding chapters
    cmd = [
        "ffmpeg",
        "-loglevel", "error",
        "-stats",
        "-n",
        "-activation_bytes",
        args.auth,
        "-i",
        src,
    ]

    # Add cover art input if available
    if cover_file and os.path.exists(cover_file):
        cmd.extend(["-i", cover_file])

    # Write chapter text file in FFmpeg's expected format
    chapter_file = os.path.join(destdir, "chapters.txt")
    with open(chapter_file, "w") as fd:
        for i, (chapter_title, start_time) in enumerate(chapter_metadata):
            hours = int(start_time // 3600)
            minutes = int((start_time % 3600) // 60)
            seconds = int(start_time % 60)
            ms = int((start_time % 1) * 1000)
            time_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}.{ms:03d}"
            safe_title = sanitize(chapter_title).replace("_", " ")
            fd.write(f"CHAPTER{i+1:02d}={time_str}\n")
            fd.write(f"CHAPTER{i+1:02d}name={safe_title}\n")

    chapter_input_idx = 2 if (cover_file and os.path.exists(cover_file)) else 1

    cmd.extend([
        "-i", chapter_file,
        "-c:a", codec,
    ])

    # Add cover art streams if available
    if cover_file and os.path.exists(cover_file):
        cmd.extend([
            "-map", "0:a:0",
            "-map", "1:v:0",
            "-c:v", "copy",
            "-disposition:v:0", "attached_pic",
        ])
    else:
        cmd.extend([
            "-map", "0:a:0",
        ])

    tags = md["format"]["tags"]
    metadata_args = []
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

    ext = codecs[args.container][2]
    output = os.path.join(destdir, f"{sanitize(t.get('title', 'audiobook'))}.{ext}")

    cmd.extend(metadata_args + [output, "-map_chapters", str(chapter_input_idx)])

    if args.verbose or args.test:
        print(" ".join([f'"{arg}"' if " " in str(arg) else str(arg) for arg in cmd]))
        print(f"Output: {output}")
        if args.test:
            return output

    # Get total duration for progress bar
    total_duration = float(md["format"].get("duration", 0))
    title = md["format"]["tags"].get("title", "audiobook")

    show_progress = HAS_TQDM and not args.verbose
    run_ffmpeg_with_progress(cmd, total_duration, f"Embedding chapters: {title}", show_progress)

    # Clean up chapter file
    if os.path.exists(chapter_file):
        os.unlink(chapter_file)
    # Clean up cover extraction
    cover_output = os.path.join(destdir, "cover.jpg")
    if os.path.exists(cover_output) and not args.keep:
        os.unlink(cover_output)
    # Clean up metadata file
    metadata_file = os.path.join(destdir, "metadata.json")
    if os.path.exists(metadata_file) and not args.keep:
        os.unlink(metadata_file)

    # Clean up intermediate file
    if not args.keep:
        os.unlink(src)
    elif args.verbose:
        print(f"Keeping intermediate file: {src}")

    return output


def extract_image(args, destdir, fn):
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


def sanitize(s):
    """replace any unsafe characters with underscores"""
    s = normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii", "ignore")
    s = s.replace("'", "").replace('"', "")
    s = re.sub("[^a-zA-Z0-9._/-]", "_", s)
    s = re.sub("_+", "_", s)
    return s


def convert_file(args, fn, md):
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
    with open(f"{destdir}/metadata.json", "w") as fd:
        jdump(md, fd, sort_keys=True, indent=4, separators=(",", ": "))

    if args.metadata:
        return

    # Extract cover image (will be embedded in chapter files if supported)
    cover_file = os.path.join(destdir, "cover.jpg")
    try:
        extract_image(args, destdir, fn)
    except Exception:
        cover_file = None

    if args.coverimage:
        return

    if "Chapter " in str(os.listdir(destdir)):
        if args.verbose:
            print(f"Already processed {fn}")
        return

    destfn = fn.replace(".aax", f".{codecs[args.container][1]}")
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
        codecs[args.container][0],
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


def process_wrapper(fn):
    global args
    setproctitle(f"transcode {fn}")
    md = None
    try:
        md = probe_metadata(args, fn)
    except Exception as e:
        print(f"Caught exception {e} while probing metadata")

    try:
        convert_file(args, fn, md)
    except Exception as e:
        print(f"Caught exception {e} while probing metadata")


def extract_chapters_from_m4a(m4a_path):
    """Extract chapter data from an m4a file using ffprobe"""
    try:
        result = subrun(
            ["ffprobe", "-v", "error", "-show_entries", "chapters=id,start,end,tags:title", "-of", "json", m4a_path],
            capture_output=True, text=True
        )
        data = loads(result.stdout)
        chapters = []
        if "chapters" in data:
            for ch in data["chapters"]:
                start = float(ch.get("start", 0))
                end = float(ch.get("end", 0))
                title = ch.get("tags", {}).get("title", "")
                chapters.append((start, end, title))
        return chapters
    except Exception:
        return []


def write_chapter_file(chapters, chapter_file):
    """Write chapter data to a file for AtomicParsley"""
    with open(chapter_file, "w") as fd:
        for i, (start, end, title) in enumerate(chapters):
            hours = int(start // 3600)
            minutes = int((start % 3600) // 60)
            seconds = int(start % 60)
            ms = int((start % 1) * 1000)
            time_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}.{ms:03d}"
            safe_title = sanitize(title).replace("_", " ") if title else f"Chapter {i+1}"
            fd.write(f"CHAPTER{i+1:02d}={time_str}\n")
            fd.write(f"CHAPTER{i+1:02d}name={safe_title}\n")


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


def concat_files(args, intermediate_m4as, destdir, all_md, cover_file):
    """Concatenate multiple intermediate m4a files into a single output file"""
    concat_list = os.path.join(destdir, "concat_list.txt")
    with open(concat_list, "w") as fd:
        for m4a_path in intermediate_m4as:
            rel_path = os.path.relpath(m4a_path, destdir)
            fd.write(f"file '{rel_path}'\n")

    codec = codecs[args.container][0]
    tags = all_md[0]["format"]["tags"]
    metadata_args = []
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

    ext = codecs[args.container][2]
    output = os.path.join(destdir, f"{sanitize(tags.get('title', 'audiobook'))}.{ext}")

    cmd = ["ffmpeg", "-loglevel", "error", "-stats", "-n"]

    # Use concat demuxer to concatenate m4a files
    cmd.extend(["-f", "concat", "-safe", "0"])
    cmd.extend(["-i", concat_list])

    if cover_file and os.path.exists(cover_file):
        cmd.extend(["-i", cover_file])

    cmd.extend(["-map", "0:a:0"])

    # Map cover art if available
    if cover_file and os.path.exists(cover_file):
        cmd.extend(["-map", "1:v:0", "-c:v", "copy", "-disposition:v:0", "attached_pic"])

    cmd.extend(["-c:a", codec])
    cmd.extend(metadata_args)
    cmd.extend([output])

    if args.test or args.verbose:
        print(" ".join([f'"{arg}"' if " " in str(arg) else str(arg) for arg in cmd]))
        print(f"Concatenated output: {output}")
        if args.test:
            return output

    # Calculate total duration for progress bar
    total_duration = sum(float(md["format"].get("duration", 0)) for md in all_md)
    title = tags.get("title", "concatenated audiobook")
    show_progress = HAS_TQDM and not args.verbose
    run_ffmpeg_with_progress(cmd, total_duration, f"Concatenating {title}", show_progress)

    # Embed chapters into the final m4b using MP4Box if available
    if args.container == "m4b" and shutil.which("MP4Box"):
        excluded = parse_exclude_chapters(args.exclude_chapters)
        has_exclusions = len(excluded) > 0

        if has_exclusions:
            # Chapters come from segment files (audio was already split)
            all_chapters = []
            time_offset = 0.0
            for seg_file in intermediate_m4as:
                try:
                    result = subrun(
                        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", seg_file],
                        capture_output=True, text=True
                    )
                    duration = float(result.stdout.strip())
                except Exception:
                    continue

                # Extract chapter number from segment filename (format: "003.m4a")
                base = os.path.basename(seg_file)
                name_part = os.path.splitext(base)[0]
                try:
                    title = f"Chapter {int(name_part)}"
                except ValueError:
                    title = f"Chapter {len(all_chapters) + 1}"

                all_chapters.append((time_offset, title))
                time_offset += duration
        else:
            # Build chapter list from original .aax metadata (m4a files don't preserve chapters via copy codec)
            all_chapters = []
            time_offset = 0.0
            global_chapter_num = 0
            for i, md in enumerate(all_md):
                chapters = md.get("chapters", [])
                if not chapters:
                    # Try to get duration for time offset
                    try:
                        result = subrun(
                            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", intermediate_m4as[i]],
                            capture_output=True, text=True
                        )
                        duration = float(result.stdout.strip())
                    except Exception:
                        duration = 0
                    time_offset += duration
                    continue
                for j, chapter in enumerate(chapters):
                    global_chapter_num += 1
                    if global_chapter_num in excluded:
                        continue
                    start_time = float(chapter["start_time"]) + time_offset
                    all_chapters.append((start_time, global_chapter_num))
                # Get duration for time offset
                try:
                    result = subrun(
                        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", intermediate_m4as[i]],
                        capture_output=True, text=True
                    )
                    duration = float(result.stdout.strip())
                except Exception:
                    duration = 0
                time_offset += duration

        if all_chapters:
            chapter_file = os.path.join(destdir, "chapters.txt")
            with open(chapter_file, "w") as fd:
                for i, (start_time, title_or_num) in enumerate(all_chapters):
                    hours = int(start_time // 3600)
                    minutes = int((start_time % 3600) // 60)
                    seconds = int(start_time % 60)
                    ms = int((start_time % 1) * 1000)
                    time_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}.{ms:03d}"
                    if has_exclusions:
                        fd.write(f"CHAPTER{i+1:02d}={time_str}\n")
                        fd.write(f"CHAPTER{i+1:02d}name={title_or_num}\n")
                    else:
                        fd.write(f"CHAPTER{i+1:02d}={time_str}\n")
                        fd.write(f"CHAPTER{i+1:02d}name=Chapter {title_or_num}\n")
            # Add chapters in-place using MP4Box (preserves metadata and cover art set by FFmpeg concat)
            mp4box_cmd = ["MP4Box", "-chap", chapter_file, output]
            subrun(mp4box_cmd)
            if os.path.exists(chapter_file):
                os.unlink(chapter_file)

    # Clean up intermediate files
    if not args.keep:
        for m4a_path in intermediate_m4as:
            if os.path.exists(m4a_path):
                os.unlink(m4a_path)
        if os.path.exists(concat_list):
            os.unlink(concat_list)
        cover_output = os.path.join(destdir, "cover.jpg")
        if os.path.exists(cover_output):
            os.unlink(cover_output)
    elif args.verbose:
        print(f"Keeping intermediate files")

    return output


def main():
    global args
    ap = argparse.ArgumentParser()
    # arbitrary parameters
    ap.add_argument("-a", "--authcode", default=None, dest="auth", help="Authorization Bytes")
    ap.add_argument(
        "-f",
        "--format",
        default="mp3",
        choices=codecs.keys(),
        dest="container",
        help="output format. Default: %(default)s. M4B creates a single file with embedded chapters.",
    )
    ap.add_argument(
        "-o", "--outputdir", default="Audiobooks", dest="outdir", help="output directory. Default: %(default)s"
    )
    ap.add_argument(
        "-p",
        "--processes",
        default=1,
        type=int,
        dest="processes",
        help="number of parallel transcoder processes to run. Default: %(default)d",
    )
    # binary flags
    ap.add_argument(
        "-c", "--clobber", default=False, dest="overwrite", action="store_true", help="overwrite existing files"
    )
    ap.add_argument(
        "-i", "--coverimage", default=False, dest="coverimage", action="store_true", help="only extract cover image"
    )
    ap.add_argument("-m", "--mono", default=False, dest="mono", action="store_true", help="downmix to mono")
    ap.add_argument(
        "-s", "--single", default=False, dest="single", action="store_true", help="don't split into chapters"
    )
    ap.add_argument(
        "-k", "--keep", default=False, dest="keep", action="store_true",
        help="keep intermediate transcoded file after splitting into chapters"
    )
    ap.add_argument("-t", "--test", default=False, dest="test", action="store_true", help="test input file(s)")
    ap.add_argument("-v", "--verbose", default=False, dest="verbose", action="store_true", help="extra verbose output")
    ap.add_argument(
        "-x", "--extract-metadata", default=False, dest="metadata", action="store_true", help="only extract metadata"
    )
    ap.add_argument(
        "-n", "--concat", default=False, dest="concat", action="store_true",
        help="concatenate all input files into a single output"
    )
    ap.add_argument(
        "-e", "--exclude-chapters", default=None, dest="exclude_chapters",
        help="exclude specific chapters (global numbering). E.g. '1,2,104' or '1-5,100-104'"
    )

    ap.add_argument(nargs="+", dest="input")
    args = ap.parse_args()

    something_is_wrong = False
    if check_missing_authcode(args):
        something_is_wrong = True

    if missing_required_programs(args):
        something_is_wrong = True

    if something_is_wrong:
        exit(1)

    if args.container == "m4b" and args.concat:
        if not shutil.which("MP4Box"):
            print("Error: MP4Box (from gpac) is required for concatenating audiobooks into a single M4B file with chapters.")
            print("Install it with: brew install gpac")
            exit(1)

    if args.mono:
        args.outdir += "-mono"

    if args.concat:
        excluded = parse_exclude_chapters(args.exclude_chapters)
        has_exclusions = len(excluded) > 0

        # Use a single output directory for all concatenated files
        first_md = probe_metadata(args, args.input[0])
        concat_destdir = os.path.join(
            args.outdir,
            first_md["format"]["tags"]["artist"],
            first_md["format"]["tags"]["title"].replace("/", "-")
        )
        concat_destdir = sanitize(concat_destdir)
        os.makedirs(concat_destdir, exist_ok=True)

        intermediate_m4as = []
        all_md = []
        final_cover = None

        if has_exclusions:
            # Split each .aax into chapter segments when chapters need to be removed
            global_chapter_num = 0
            for fn in args.input:
                md = None
                try:
                    md = probe_metadata(args, fn)
                except Exception as e:
                    print(f"Caught exception {e} while probing metadata for {fn}")
                    continue

                chapters = md.get("chapters", [])
                if not chapters:
                    continue

                # Split this .aax into chapter segments (with global numbering)
                for chapter in chapters:
                    global_chapter_num += 1
                    chapter_title = chapter["tags"].get("title", f"Chapter {global_chapter_num}")
                    start_time = float(chapter["start_time"])
                    end_time = float(chapter["end_time"])
                    duration = end_time - start_time

                    # Check if this chapter is excluded
                    if global_chapter_num in excluded:
                        continue

                    safe_title = sanitize(chapter_title).replace("_", " ")
                    output_file = os.path.join(concat_destdir, f"{global_chapter_num:03d}.m4a")

                    cmd = [
                        "ffmpeg",
                        "-loglevel", "error",
                        "-activation_bytes", args.auth,
                        "-ss", str(start_time),
                        "-t", str(duration),
                        "-i", fn,
                        "-vn",
                        "-codec:a", codecs[args.container][0],
                        "-map_metadata", "-1",
                        "-metadata", f'title={chapter_title}',
                        output_file
                    ]

                    tags = md["format"]["tags"]
                    if "artist" in tags:
                        cmd.extend(["-metadata", f'artist={tags["artist"]}'])
                    if "album" in tags:
                        cmd.extend(["-metadata", f'album={tags["title"]}'])
                    if "album_artist" in tags:
                        cmd.extend(["-metadata", f'album_artist={tags["album_artist"]}'])
                    if "date" in tags:
                        cmd.extend(["-metadata", f'date={tags["date"]}'])
                    if "genre" in tags:
                        cmd.extend(["-metadata", f'genre={tags["genre"]}'])

                    cmd_str = " ".join([f'"{arg}"' if " " in str(arg) else str(arg) for arg in cmd])
                    rv = os.system(cmd_str.encode("utf-8"))
                    if rv == 0:
                        intermediate_m4as.append(output_file)

                # Use first cover file found
                if final_cover is None:
                    cover_file = os.path.join(concat_destdir, "cover.jpg")
                    try:
                        extract_image(args, concat_destdir, fn)
                        final_cover = cover_file
                    except Exception:
                        pass
                all_md.append(md)
        else:
            for fn in args.input:
                md = None
                try:
                    md = probe_metadata(args, fn)
                except Exception as e:
                    print(f"Caught exception {e} while probing metadata for {fn}")
                    continue

            destfn = os.path.basename(fn).replace(".aax", ".m4a")
            output = os.path.join(concat_destdir, destfn)
            if os.path.exists(output) and args.overwrite:
                os.unlink(output)

            ac = "2"
            ab = md["format"]["bit_rate"]
            if args.mono:
                ac = "1"
                ab = str(int(ab) / 2)

            tags = md["format"]["tags"]
            metadata_args = []
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
                "ffmpeg", "-loglevel", "error", "-stats", "-n", "-activation_bytes", args.auth,
                "-i", fn, "-vn", "-codec:a", codecs[args.container][0],
                "-ab", ab, "-ac", ac, "-map_metadata", "-1",
            ] + metadata_args + [output]

            if args.test or args.verbose:
                print(" ".join([f'"{arg}"' if " " in str(arg) else str(arg) for arg in cmd]))

            total_duration = float(md["format"].get("duration", 0))
            title = md["format"]["tags"].get("title", "audio")
            show_progress = HAS_TQDM and not args.verbose
            run_ffmpeg_with_progress(cmd, total_duration, f"Transcoding {title}", show_progress)

            intermediate_m4as.append(output)
            all_md.append(md)

            # Use first cover file found
            if final_cover is None:
                cover_file = os.path.join(concat_destdir, "cover.jpg")
                try:
                    extract_image(args, concat_destdir, fn)
                    final_cover = cover_file
                except Exception:
                    pass

        concat_files(args, intermediate_m4as, concat_destdir, all_md, final_cover)
    else:
        if multiprocessing is None:
            args.processes = 1

        if args.processes < 2:
            for fn in args.input:
                process_wrapper(fn)
        else:
            proc_pool = multiprocessing.Pool(processes=args.processes, maxtasksperchild=1)
            setproctitle("transcode_dispatcher")
            proc_pool.map(process_wrapper, args.input, chunksize=1)

    os.system("stty echo")


if __name__ == "__main__":
    main()
