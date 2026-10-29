"""Extract URLs embedded in PDFs and images. Never raises; returns () on any problem."""

from __future__ import annotations

import re
from pathlib import Path

_URL_RE = re.compile(r"https?://[^\s<>\"'\\)\]]+")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".gif", ".bmp", ".webp", ".heic"}


def _urls_in(strings) -> list[str]:
    found: list[str] = []
    for s in strings:
        if isinstance(s, bytes):
            s = s.decode("utf-8", "replace")
        if not isinstance(s, str):
            continue
        for m in _URL_RE.findall(s):
            m = m.rstrip(".,;")
            if m not in found:
                found.append(m)
    return found


def _pdf_strings(path: Path) -> list[str]:
    from pypdf import PdfReader  # lazy: optional dependency

    reader = PdfReader(str(path))
    out: list[str] = []
    meta = reader.metadata
    if meta:
        out.extend(str(v) for v in meta.values())
    for page in reader.pages:
        for ref in page.get("/Annots") or []:
            try:
                annot = ref.get_object()
                action = annot.get("/A")
                if action is not None:
                    uri = action.get_object().get("/URI")
                    if uri:
                        out.append(str(uri))
            except Exception:
                continue
        try:
            out.append(page.extract_text() or "")
        except Exception:
            continue
    return out


def _image_strings(path: Path) -> list[str]:
    from PIL import Image  # lazy: optional dependency

    out: list[str] = []
    with Image.open(path) as img:
        for v in img.info.values():
            if isinstance(v, (str, bytes)):
                out.append(v)
            elif isinstance(v, dict):
                out.extend(x for x in v.values() if isinstance(x, (str, bytes)))
        try:
            exif = img.getexif()
            out.extend(v for v in exif.values() if isinstance(v, (str, bytes)))
        except Exception:
            pass
    return out


def embedded_urls(path: Path) -> tuple[str, ...]:
    path = Path(path)
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            strings = _pdf_strings(path)
        elif ext in IMAGE_EXTS:
            strings = _image_strings(path)
        else:
            return ()
        return tuple(_urls_in(strings))
    except Exception:
        return ()
