"""Reading the local tabs' log files from a byte offset.

Both long-running local jobs -- the update and a model download -- write a plain log the page tails,
and the tail has the same rules in both cases: whole lines only, so a chunk never splits a character
or shows half a line, and an offset past the end reads as empty rather than as an error.

Not SSE: the update restarts the server in its middle, which would drop an open stream exactly when
the page needs it.  A file the reader has already read stays valid across a restart; the offset is the
reader's, and a log that gets shorter means the next run started over.
"""
from __future__ import annotations

from pathlib import Path


def tail(path: Path, since: int = 0) -> dict:
    """The log at `path` from byte `since`, cut at a line boundary: {"text", "next", "size"}."""
    try:
        data = path.read_bytes()
    except OSError:
        data = b""
    try:
        start = max(0, min(int(since or 0), len(data)))
    except (TypeError, ValueError):
        start = 0
    chunk = data[start:]
    cut = chunk.rfind(b"\n")
    if cut < 0:                                  # a half-written line: hand nothing over, ask again
        text, nxt = "", start
    else:
        text, nxt = chunk[:cut + 1].decode("utf-8", "replace"), start + cut + 1
    return {"text": text.lstrip("\ufeff"), "next": nxt, "size": len(data)}


def append(path: Path, line: str) -> None:
    """One line at the end, with the folders made if this is the log's first line."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # newline="\n": the page's offsets are bytes, and a log that turned \n into \r\n on Windows
        # would carry one byte per line that nothing asked for
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(line.rstrip("\n") + "\n")
    except OSError:
        pass                                     # a log is best effort: it must never break the job
