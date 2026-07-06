"""Command-line interface for aax-convert."""

import os
import shutil
import argparse
from subprocess import check_output

from . import CODECS, HAS_TQDM, tqdm
from .utils import parse_exclude_chapters, get_splitpoints, sanitize
from .audiobook import probe_metadata, extract_image
from .converter import run_ffmpeg_with_progress, split_file, convert_file
from .chapters import embed_chapters
from .merger import concat_files

# Multiprocessing support (optional)
try:
    import multiprocessing
except ImportError:
    multiprocessing = None


# Module-level args for backward compatibility with nested calls
args = None


def check_missing_authcode(args):
    """Ensure that an authcode is available."""
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
    """Ensure that various dependencies are available."""
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


def process_wrapper(fn):
    """Multiprocessing worker: probe and convert a single file."""
    global args
    try:
        from setproctitle import setproctitle
    except ImportError:
        def setproctitle(x):
            pass

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


def build_parser():
    """Build and return the argument parser."""
    ap = argparse.ArgumentParser()
    # arbitrary parameters
    ap.add_argument("-a", "--authcode", default=None, dest="auth", help="Authorization Bytes")
    ap.add_argument(
        "-f",
        "--format",
        default="mp3",
        choices=CODECS.keys(),
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
    return ap


def main(argv=None):
    """Entry point for aax-convert."""
    global args

    ap = build_parser()
    args = ap.parse_args(argv)

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
                        "-codec:a", CODECS[args.container][0],
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
                    "-i", fn, "-vn", "-codec:a", CODECS[args.container][0],
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
            try:
                from setproctitle import setproctitle
            except ImportError:
                def setproctitle(x):
                    pass
            proc_pool = multiprocessing.Pool(processes=args.processes, maxtasksperchild=1)
            setproctitle("transcode_dispatcher")
            proc_pool.map(process_wrapper, args.input, chunksize=1)

    os.system("stty echo")
