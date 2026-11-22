#!/usr/bin/env python3
"""Build serve/web/index.html from the upstream page plus serve/web/local/patch.html.

The local views (chat, settings and the tabs that switch to them) are kept out of the stock
page: patch.html holds them between three marker pairs, and this script puts each region at
its anchor in the upstream file --

    head   -> before </head>     styles and scripts
    nav    -> after  </header>   the view tabs
    views  -> before </body>     the chat and settings sections

The upstream page is read from git, so an upstream rewrite of the layout (0.5.5 rebuilt it
whole) never becomes a hand merge: the anchors survive, and one build puts the regions back.
Re-running is idempotent -- the regions already in the file are stripped before they are
inserted again, so the result is the same whether the base came from git or from the file.

    python serve/web/local/build_index.py --from v0.5.5
    python serve/web/local/build_index.py --from-file page.html --out page.html
    python serve/web/local/build_index.py --check        # exit 1 when index.html is out of date
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]          # .../serve/web/local/build_index.py -> the repo
WEB = ROOT / "serve" / "web"
PATCH = WEB / "local" / "patch.html"
PAGE = WEB / "index.html"

# a whole region, marker lines included, with the newline that follows it
REGION_RE = re.compile(r"[ \t]*<!-- local-ui:(?P<name>[a-z]+) -->.*?<!-- /local-ui:(?P=name) -->[ \t]*\n?", re.S)
PATCH_RE = re.compile(r"(?P<block><!-- local-ui:(?P<name>[a-z]+) -->\n.*?<!-- /local-ui:(?P=name) -->)", re.S)

ANCHORS = (("head", "</head>", "before"),
           ("nav", "</header>", "after"),
           ("views", "</body>", "before"))


def read_patch(path: Path = PATCH) -> dict[str, str]:
    """The three regions of patch.html by name, markers included."""
    blocks: dict[str, str] = {}
    for m in PATCH_RE.finditer(path.read_text(encoding="utf-8")):
        name = m.group("name")
        if name in blocks:
            raise SystemExit(f"{path}: two '{name}' regions")
        blocks[name] = m.group("block")
    missing = [name for name, _, _ in ANCHORS if name not in blocks]
    if missing:
        raise SystemExit(f"{path}: no region for {', '.join(missing)}")
    return blocks


def strip_regions(text: str) -> str:
    """The page without the local regions, so a rebuild starts from the upstream text."""
    return REGION_RE.sub("", text)


def insert(text: str, anchor: str, where: str, block: str) -> str:
    found = text.count(anchor)
    if found != 1:
        raise SystemExit(f"anchor {anchor!r} appears {found} times, expected once: "
                         f"the upstream page changed shape, check it before building")
    i = text.index(anchor)
    if where == "before":
        return text[:i] + block + "\n" + text[i:]
    return text[: i + len(anchor)] + "\n" + block + text[i + len(anchor):]


def build(base: str, blocks: dict[str, str]) -> str:
    text = strip_regions(base)
    for name, anchor, where in ANCHORS:
        text = insert(text, anchor, where, blocks[name])
    return text


def read_base(args) -> tuple[str, str]:
    """The upstream page and where it came from."""
    if args.from_file:
        p = Path(args.from_file)
        return p.read_text(encoding="utf-8"), str(p)
    if args.source:
        try:
            out = subprocess.run(["git", "-C", str(ROOT), "show", f"{args.source}:serve/web/index.html"],
                                 capture_output=True, check=True)
        except (OSError, subprocess.CalledProcessError) as e:
            detail = getattr(e, "stderr", b"") or b""
            raise SystemExit(f"git show {args.source}:serve/web/index.html failed: "
                             f"{detail.decode('utf-8', 'replace').strip() or e}")
        return out.stdout.decode("utf-8"), f"git {args.source}:serve/web/index.html"
    return PAGE.read_text(encoding="utf-8"), str(PAGE)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Splice the local views into the stock Basalt page.")
    ap.add_argument("--from", dest="source", metavar="TAG",
                    help="upstream page from git, e.g. v0.5.5 or origin/local")
    ap.add_argument("--from-file", metavar="PATH", help="upstream page from a file instead of git")
    ap.add_argument("--out", metavar="PATH", default=str(PAGE), help=f"where to write (default {PAGE})")
    ap.add_argument("--check", action="store_true",
                    help="do not write; exit 1 if the file differs from a fresh build")
    args = ap.parse_args(argv)
    if args.source and args.from_file:
        ap.error("--from and --from-file are exclusive")

    blocks = read_patch()
    base, origin = read_base(args)
    text = build(base, blocks)

    out = Path(args.out)
    if args.check:
        current = out.read_text(encoding="utf-8") if out.is_file() else ""
        if current == text:
            print(f"{out}: up to date ({origin})")
            return 0
        print(f"{out}: out of date against {origin}; run this script without --check")
        return 1
    out.write_text(text, encoding="utf-8", newline="\n")
    print(f"{out}: built from {origin} ({len(blocks)} regions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
