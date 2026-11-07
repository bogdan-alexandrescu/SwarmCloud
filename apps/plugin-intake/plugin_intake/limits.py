"""The intake's caps.

The three size caps are the owner's answer to Q8 (the owner, 6 November 2026): 100 MiB
packed, 500 MiB unpacked and 20,000 files, larger than the design's recommended
25 MiB / 100 MiB / 5,000 because gstack and security-audit move to the managed
path. The ratio is the design's (§2.3 step 3).
"""

from __future__ import annotations

MIB = 1024 * 1024

#: The quarantined object, as fetched or uploaded.
MAX_PACKED_BYTES = 100 * MIB
#: File bytes actually written while unpacking, hard links counted once per link.
MAX_UNPACKED_BYTES = 500 * MIB
#: Regular files, symbolic links and hard links in the archive.
MAX_FILES = 20_000
#: Every entry, directories included: an archive of nothing but directory headers
#: is bounded too.
MAX_ENTRIES = 2 * MAX_FILES
#: Per entry in a zip, over the whole stream in a .tar.gz (gzip compresses the
#: stream, not the entry).
MAX_EXPANSION_RATIO = 100
#: Below this many unpacked bytes the ratio is not judged: a few kilobytes of
#: repeated text compress past 100:1 and are no bomb.
RATIO_FLOOR_BYTES = 1 * MIB
#: Tar headers and padding the decompressed stream may hold beyond its file
#: bytes (512-byte headers, 512-byte blocks, pax records).
TAR_OVERHEAD_BYTES = 64 * MIB
#: Path shape (§2.3 step 2).
MAX_SEGMENTS = 32
MAX_SEGMENT_BYTES = 255
#: A symbolic link's target, and every JSON or Markdown file the intake parses.
MAX_LINK_TARGET_BYTES = 4096
MAX_PARSED_FILE_BYTES = 1 * MIB
#: The overlay arrives in the execution's environment.
MAX_OVERLAY_BYTES = 64 * 1024
