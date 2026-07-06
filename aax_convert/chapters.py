"""Chapter file generation and embedding."""

import os
from subprocess import run as subrun
from json import loads

from . import CODECS, HAS_TQDM, tqdm
from .utils import sanitize
from .converter import run_ffmpeg_with_progress


def write_chapter_file(chapters, chapter_file):
    """Write chapter data to a file for FFmpeg chapter embedding."""
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


def extract_chapters_from_m4a(m4a_path):
    """Extract chapter data from an m4a file using ffprobe."""
    try:
        result = subrun(
            ["ffprobe", "-v", "error", "-show_entries",
             "chapters=id,start,end,tags:title", "-of", "json", m4a_path],
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


def embed_chapters(args, destdir, src, md, cover_file=None):
    """Embed chapters into a single M4B file using ffmpeg."""
    from .converter import run_ffmpeg_with_progress
    from . import CODECS

    chapters = md["chapters"]
    t = md["format"]["tags"]
    codec = CODECS[args.container][0]
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

    ext = CODECS[args.container][2]
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
