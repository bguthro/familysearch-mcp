"""Download a range of film pages (read-only, production) to a directory.

python -m tools.film.film_pages ARK --first N --last M --out DIR
       [--crop left-parents] [--box x0,y0,x1,y1] [--montage]

Never writes inside the repository; prints only counts and file names.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import warnings
from pathlib import Path

from familysearch.auth import Environment, extract_brave_token
from familysearch.client import FSClient
from familysearch.errors import FSError
from familysearch.images import ImageReader

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BOX = (0.09, 0.265, 0.335, 0.80)


EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
              "image/webp": ".webp"}
MAX_PIXELS = 100_000_000


def ext_for(content_type: str) -> str:
    return EXTENSIONS[content_type]


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return str(a).casefold() == str(b).casefold()


def check_out_dir(out: str | Path) -> Path:
    """Refuse the repository, anything inside it (any case, any symlink path),
    and any directory inside a git work tree."""
    p = Path(out).expanduser().resolve()
    for q in (p, *p.parents):
        if _same(q, REPO_ROOT) or (q / ".git").exists():
            raise SystemExit("refusing to write inside a repository; choose another --out")
    return p


def write_file(out: Path, name: str, data: bytes, overwrite: bool) -> None:
    """Create out/name exclusively, never following a symlink."""
    path = out / name
    if overwrite and os.path.lexists(path):
        if path.is_symlink() or not path.is_file():
            raise SystemExit(f"refusing to replace {name}: not a regular file")
        path.unlink()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        raise SystemExit(f"{name} already exists (use --overwrite)") from None
    except OSError:
        raise SystemExit(f"cannot create {name}") from None
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def parse_box(text: str | None) -> tuple[float, float, float, float]:
    if not text:
        return DEFAULT_BOX
    try:
        x0, y0, x1, y1 = (float(v) for v in text.split(","))
    except ValueError:
        raise SystemExit("--box must be x0,y0,x1,y1 as fractions") from None
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        raise SystemExit("--box fractions must satisfy 0 <= x0 < x1 <= 1, 0 <= y0 < y1 <= 1")
    return x0, y0, x1, y1


def _pillow():
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    warnings.simplefilter("error", Image.DecompressionBombWarning)
    return Image


def crop_image(data: bytes, box):
    Image = _pillow()
    im = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = im.size
    x0, y0, x1, y1 = box
    return im.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))


def make_montage(left, right, left_no: int, right_no: int):
    """Side by side with labels; right may be None for an odd last crop."""
    from PIL import ImageDraw

    Image = _pillow()
    label_h = 28
    rw = right.width if right is not None else 0
    rh = right.height if right is not None else 0
    out = Image.new("RGB", (left.width + rw, max(left.height, rh) + label_h), "white")
    out.paste(left, (0, label_h))
    d = ImageDraw.Draw(out)
    d.text((4, 8), f"image {left_no}", fill="black")
    if right is not None:
        out.paste(right, (left.width, label_h))
        d.text((left.width + 4, 8), f"image {right_no}", fill="black")
    return out


def run(argv=None, *, client: FSClient | None = None) -> int:
    ap = argparse.ArgumentParser(prog="film_pages")
    ap.add_argument("ark")
    ap.add_argument("--first", type=int, required=True)
    ap.add_argument("--last", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--crop", choices=["left-parents"])
    ap.add_argument("--box")
    ap.add_argument("--montage", action="store_true")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--size", default="dist", choices=["dist", "thumb_p200"])
    args = ap.parse_args(argv)

    out = check_out_dir(args.out)
    box = parse_box(args.box)
    if args.montage and not args.crop:
        raise SystemExit("--montage requires --crop")
    try:
        out.mkdir(parents=True, exist_ok=True)
        if client is None:
            client = FSClient(extract_brave_token(), Environment.PRODUCTION)
        names, crops = [], []
        for idx, data, ctype in ImageReader(client).fetch_film_range(
                args.ark, args.first, args.last, args.size):
            ext = ext_for(ctype)
            if args.crop:
                im = crop_image(data, box)
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=90)
                data, ext = buf.getvalue(), ".jpg"
                crops.append((idx, im))
            name = f"film_{idx:05d}{ext}"
            write_file(out, name, data, args.overwrite)
            names.append(name)
        if args.montage:
            for i in range(0, len(crops), 2):
                a, ia = crops[i]
                b, ib = crops[i + 1] if i + 1 < len(crops) else (a, None)
                buf = io.BytesIO()
                make_montage(ia, ib, a, b).save(buf, "JPEG", quality=90)
                mname = f"montage_{a:05d}_{b:05d}.jpg"
                write_file(out, mname, buf.getvalue(), args.overwrite)
                names.append(mname)
    except (FSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - short message only, no paths or hosts
        print(f"error: {type(e).__name__}", file=sys.stderr)
        return 1
    print(f"wrote {len(names)} files")
    for n in names:
        print(n)
    return 0


if __name__ == "__main__":
    sys.exit(run())
