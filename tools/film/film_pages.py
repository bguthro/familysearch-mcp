"""Download a range of film pages (read-only, production) to a directory.

python -m tools.film.film_pages ARK --first N --last M --out DIR
       [--crop left-parents] [--box x0,y0,x1,y1] [--montage]

Never writes inside the repository; prints only counts and file names.
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

from familysearch.auth import Environment, extract_brave_token
from familysearch.client import FSClient
from familysearch.images import ImageReader

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BOX = (0.09, 0.265, 0.335, 0.80)


def check_out_dir(out: str | Path) -> Path:
    p = Path(out).expanduser().resolve()
    if p == REPO_ROOT or REPO_ROOT in p.parents:
        raise SystemExit("refusing to write inside the repository; choose another --out")
    return p


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


def crop_image(data: bytes, box):
    from PIL import Image

    im = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = im.size
    x0, y0, x1, y1 = box
    return im.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))


def make_montage(left, right, left_no: int, right_no: int):
    from PIL import Image, ImageDraw

    label_h = 28
    w = left.width + right.width
    h = max(left.height, right.height) + label_h
    out = Image.new("RGB", (w, h), "white")
    out.paste(left, (0, label_h))
    out.paste(right, (left.width, label_h))
    d = ImageDraw.Draw(out)
    d.text((4, 8), f"image {left_no}", fill="black")
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
    ap.add_argument("--size", default="dist", choices=["dist", "thumb_p200"])
    args = ap.parse_args(argv)

    out = check_out_dir(args.out)
    box = parse_box(args.box)
    if args.montage and not args.crop:
        raise SystemExit("--montage requires --crop")
    out.mkdir(parents=True, exist_ok=True)
    if client is None:
        client = FSClient(extract_brave_token(), Environment.PRODUCTION)
    reader = ImageReader(client)

    names, crops = [], []
    for idx, data, _ctype in reader.fetch_film_range(
            args.ark, args.first, args.last, args.size):
        name = f"film_{idx:05d}.jpg"
        if args.crop:
            im = crop_image(data, box)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=90)
            data = buf.getvalue()
            crops.append((idx, im))
        (out / name).write_bytes(data)
        names.append(name)
    if args.montage:
        for i in range(0, len(crops) - 1, 2):
            (a, ia), (b, ib) = crops[i], crops[i + 1]
            mname = f"montage_{a:05d}_{b:05d}.jpg"
            make_montage(ia, ib, a, b).save(out / mname, "JPEG", quality=90)
            names.append(mname)
    print(f"wrote {len(names)} files")
    for n in names:
        print(n)
    return 0


if __name__ == "__main__":
    sys.exit(run())
