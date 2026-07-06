"""Multi-file concatenation and chapter embedding."""

import os
import shutil
from subprocess import run as subrun
from json import loads

from . import CODECS, HAS_TQDM, tqdm
from .utils import parse_ffmpeg_time, parse_exclude_chapters, sanitize
from .converter import run_ffmpeg_with_progress


def concat_files(args, intermediate_m4as, destdir, all_md, cover_file):
    """Concatenate multiple intermediate m4a files into a single output file."""
    concat_list = os.path.join(destdir, "concat_list.txt")
    with open(concat_list, "w") as fd:
        for m4a_path in intermediate_m4as:
            rel_path = os.path.relpath(m4a_path, destdir)
            fd.write(f"file '{rel_path}'\n")

    codec = CODECS[args.container][0]
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

    ext = CODECS[args.container][2]
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
                        ["ffprobe", "-v", "error", "-show_entries",
                         "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", seg_file],
                        capture_output=True, text=True
                    )
                    duration = float(result.stdout.strip())
                except Exception:
                    continue

                all_chapters.append((time_offset, f"Chapter {len(all_chapters) + 1}"))
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
                            ["ffprobe", "-v", "error", "-show_entries",
                             "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", intermediate_m4as[i]],
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
                        ["ffprobe", "-v", "error", "-show_entries",
                         "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", intermediate_m4as[i]],
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
