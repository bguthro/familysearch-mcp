import sys

import pytest

from familysearch_import.embedded import embedded_urls

pypdf = pytest.importorskip("pypdf")
PIL = pytest.importorskip("PIL")


def make_pdf(path, link=None, title=None):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, NumberObject, ArrayObject, FloatObject, TextStringObject

    w = PdfWriter()
    page = w.add_blank_page(200, 200)
    if link:
        annot = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Link"),
                NameObject("/Rect"): ArrayObject([FloatObject(0), FloatObject(0), FloatObject(50), FloatObject(50)]),
                NameObject("/A"): DictionaryObject(
                    {
                        NameObject("/S"): NameObject("/URI"),
                        NameObject("/URI"): TextStringObject(link),
                    }
                ),
            }
        )
        page[NameObject("/Annots")] = ArrayObject([w._add_object(annot)])
    if title:
        w.add_metadata({"/Subject": title})
    with open(path, "wb") as f:
        w.write(f)


def test_pdf_link_annotation(tmp_path):
    p = tmp_path / "a.pdf"
    make_pdf(p, link="https://example.org/orig/123")
    assert embedded_urls(p) == ("https://example.org/orig/123",)


def test_pdf_metadata(tmp_path):
    p = tmp_path / "a.pdf"
    make_pdf(p, title="from https://example.org/meta")
    assert "https://example.org/meta" in embedded_urls(p)


def test_pdf_without_url(tmp_path):
    p = tmp_path / "a.pdf"
    make_pdf(p)
    assert embedded_urls(p) == ()


def test_corrupt_files(tmp_path):
    p = tmp_path / "bad.pdf"
    p.write_bytes(b"not a pdf at all")
    assert embedded_urls(p) == ()
    q = tmp_path / "bad.jpg"
    q.write_bytes(b"nope")
    assert embedded_urls(q) == ()
    assert embedded_urls(tmp_path / "missing.pdf") == ()
    r = tmp_path / "x.txt"
    r.write_text("https://example.org/x")
    assert embedded_urls(r) == ()


def test_image_png_text_chunk(tmp_path):
    from PIL import Image, PngImagePlugin

    p = tmp_path / "a.png"
    info = PngImagePlugin.PngInfo()
    info.add_text("Source", "https://example.org/img/9")
    Image.new("RGB", (4, 4)).save(p, pnginfo=info)
    assert embedded_urls(p) == ("https://example.org/img/9",)


def test_image_exif(tmp_path):
    from PIL import Image

    p = tmp_path / "a.jpg"
    img = Image.new("RGB", (4, 4))
    exif = img.getexif()
    exif[270] = "see https://example.org/exif/1"
    img.save(p, exif=exif)
    assert "https://example.org/exif/1" in embedded_urls(p)


def test_missing_library(tmp_path, monkeypatch):
    p = tmp_path / "a.pdf"
    make_pdf(p, link="https://example.org/x")
    monkeypatch.setitem(sys.modules, "pypdf", None)
    assert embedded_urls(p) == ()
    q = tmp_path / "a.png"
    from PIL import Image

    Image.new("RGB", (2, 2)).save(q)
    monkeypatch.setitem(sys.modules, "PIL", None)
    monkeypatch.setitem(sys.modules, "PIL.Image", None)
    assert embedded_urls(q) == ()
