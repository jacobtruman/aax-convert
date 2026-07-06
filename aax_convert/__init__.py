"""aax-convert: Convert Audible AAX audiobook files to MP3, M4B, FLAC, Opus and other formats."""

__version__ = "0.2.0"

# Codec definitions: (codec, extension, container)
CODECS = {
    "mp3": ["libmp3lame", "mp3", "mp3"],
    "aac": ["copy", "m4a", "m4a"],
    "m4a": ["copy", "m4a", "m4a"],
    "m4b": ["copy", "m4a", "m4b"],
    "flac": ["flac", "flac", "flac"],
    "opus": ["libopus", "opus", "opus"],
}

# Feature flags
try:
    from tqdm import tqdm
    HAS_TQDM = True
except ImportError:
    HAS_TQDM = False
    tqdm = None

# Re-export key functions
from .utils import (
    sanitize,
    parse_exclude_chapters,
    parse_ffmpeg_time,
    numfix,
    get_splitpoints,
)
from .audiobook import probe_metadata, extract_image
from .converter import run_ffmpeg_with_progress, split_with_ffmpeg, split_file, convert_file
from .chapters import write_chapter_file, extract_chapters_from_m4a, embed_chapters
from .merger import concat_files
from .cli import main, build_parser
