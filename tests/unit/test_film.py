import json

import pytest
import responses

from familysearch.auth import Environment
from familysearch.client import FSClient
from familysearch.errors import FSError
from familysearch.images import ImageReader, validate_das_url

TOKEN = "sekrit-token-123"
WWW = "https://www.familysearch.org"
ARK = "/ark:/61903/3:1:ABCD-EF1-2G3"
HOST = "https://sg30p0.familysearch.org"
DAS = HOST + "/service/records/storage/dascloud/das/v2/"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 20


def prod():
    return FSClient(TOKEN, Environment.PRODUCTION, min_write_interval=0)


# ---- validate_das_url ----

@pytest.mark.parametrize("u", [
    DAS + "TH-1", DAS + "TH-1/parents", DAS + "TH-1/children",
    DAS + "TH-1/children?start=0&count=50", DAS + "TH-1/children?start=10&count=2000",
])
def test_das_accepts(u):
    assert validate_das_url(u) == u


@pytest.mark.parametrize("u", [
    (DAS + "TH-1").replace("https", "http"),
    (DAS + "TH-1").replace("sg30p0.familysearch.org", "evil.example"),
    (DAS + "TH-1").replace("sg30p0.familysearch.org", "sg30p0.familysearch.org.evil.com"),
    (DAS + "TH-1").replace("sg30p0.familysearch.org", "www.familysearch.org"),
    (DAS + "TH-1").replace("sg30p0.familysearch.org", "u:p@sg30p0.familysearch.org"),
    (DAS + "TH-1").replace("sg30p0.familysearch.org", "sg30p0.familysearch.org:8443"),
    (DAS + "TH-1").replace("sg30p0.familysearch.org", "sg30p0.familysearch.org:443"),
    DAS + "TH-1?start=0&count=5", DAS + "TH-1/parents?start=0&count=5",
    DAS + "TH-1/children?start=0", DAS + "TH-1/children?count=5&start=0",
    DAS + "TH-1/children?start=0&count=2001", DAS + "TH-1/children?start=0&count=0",
    DAS + "TH-1/children?start=0&count=5&x=1", DAS + "TH-1/children?start=-1&count=5",
    DAS + "TH-1/children?start=0&count=5#f", DAS + "TH-1#f",
    DAS + "TH-1/children?start=0&count=5 ", " " + DAS + "TH-1", DAS + "TH-1\n",
    DAS + "TH-1/par\tents",
    DAS + "TH-1/children?start=٠&count=5", DAS + "TH-١",
    (DAS + "TH-1").replace("sg30p0", "sg３p0"),
    DAS + "TH-1/parents/x", DAS + "TH-1/dist.jpg", DAS + "TH-1/", DAS + "A/../B",
    DAS.replace("/v2/", "/v3/") + "TH-1", DAS, "", None,
])
def test_das_rejects(u):
    with pytest.raises(FSError):
        validate_das_url(u)


# ---- get_das_json ----

@responses.activate
def test_das_json_ok_headers():
    responses.add(responses.GET, DAS + "TH-1", json={"name": "x"})
    assert prod().get_das_json(DAS + "TH-1") == {"name": "x"}
    sent = responses.calls[0].request
    assert sent.headers["Authorization"] == f"Bearer {TOKEN}"
    assert sent.headers["Accept"] == "application/json"


@responses.activate
def test_das_json_refuses_redirect():
    responses.add(responses.GET, DAS + "TH-1", status=302,
                  headers={"Location": "https://evil.example/x"})
    with pytest.raises(FSError, match="redirect") as ei:
        prod().get_das_json(DAS + "TH-1")
    assert TOKEN not in str(ei.value) and "evil" not in str(ei.value)
    assert len(responses.calls) == 1


@responses.activate
@pytest.mark.parametrize("kw", [
    dict(body="<html>", content_type="text/html"),
    dict(body="[1]", content_type="application/json"),
    dict(body="{nope", content_type="application/json"),
    dict(body="secret body", status=500, content_type="application/json"),
])
def test_das_json_bad_responses(kw):
    responses.add(responses.GET, DAS + "TH-1", **kw)
    with pytest.raises(FSError) as ei:
        prod().get_das_json(DAS + "TH-1")
    assert TOKEN not in str(ei.value) and "secret body" not in str(ei.value)


@responses.activate
@pytest.mark.parametrize("body", [b"[" * 200000, b'{"a":' * 100000])
def test_das_json_deep_nesting(body):
    responses.add(responses.GET, DAS + "TH-1", body=body, content_type="application/json")
    with pytest.raises(FSError):
        prod().get_das_json(DAS + "TH-1", max_bytes=10_000_000)


@responses.activate
def test_das_json_deadline(monkeypatch):
    import time as t
    responses.add(responses.GET, DAS + "TH-1", body=b"{}", content_type="application/json")
    clock = iter([1000.0, 1061.0, 1062.0, 1063.0])
    monkeypatch.setattr(t, "monotonic", lambda: next(clock))
    with pytest.raises(FSError, match="time limit"):
        prod().get_das_json(DAS + "TH-1")


@responses.activate
def test_das_json_oversize():
    responses.add(responses.GET, DAS + "TH-1", body=b"{" + b" " * 500 + b"}",
                  content_type="application/json")
    with pytest.raises(FSError, match="size limit"):
        prod().get_das_json(DAS + "TH-1", max_bytes=100)


@responses.activate
def test_das_json_validates_and_production_only():
    with pytest.raises(FSError):
        prod().get_das_json("https://evil.example/service/records/storage/dascloud/das/v2/A")
    beta = FSClient(TOKEN, Environment.BETA, min_write_interval=0)
    with pytest.raises(FSError, match="production"):
        beta.get_das_json(DAS + "TH-1")
    assert not responses.calls


# ---- ImageReader film ----

def setup_film(n=5, child="FILM1_00003", kids=None, count=None, parents=None, own=3):
    responses.add(responses.GET, WWW + ARK, json={
        "apid": "TH-IMG", "link": [{"rel": "parents", "href": DAS + "TH-IMG/parents"}]})
    responses.add(responses.GET, DAS + "TH-IMG/parents",
                  json={"parents": parents if parents is not None else [
                      {"parentApid": "TH-DGS", "childName": child, "nameSpace": "dgs"}]})
    responses.add(responses.GET, DAS + "TH-DGS",
                  json={"name": "FILM1", "childCount": n if count is None else count})
    if kids is None:
        kids = [{"apid": "TH-IMG" if i == own else f"TH-P{i}",
                 "name": f"FILM1_{i:05d}", "order": i} for i in range(1, n + 1)]
        kids.reverse()
    responses.add(responses.GET, DAS + f"TH-DGS/children?start=0&count={n}",
                  json={"children": kids})


@responses.activate
def test_film_info():
    setup_film()
    assert ImageReader(prod()).film_info(ARK) == {
        "film": "FILM1", "image_number": 3, "total_images": 5, "dgs_apid": "TH-DGS"}


@responses.activate
def test_position_from_apid_not_suffix():
    setup_film(child="FILM1_00001", own=4)
    assert ImageReader(prod()).film_info(ARK)["image_number"] == 4


@responses.activate
def test_own_apid_missing_or_twice():
    setup_film(kids=[{"apid": f"TH-P{i}", "order": i} for i in range(1, 6)])
    with pytest.raises(FSError):
        ImageReader(prod()).film_info(ARK)


@responses.activate
@pytest.mark.parametrize("parents", [
    [], [{"parentApid": "TH-DGS", "childName": "F_00001", "nameSpace": "other"}],
    [{"parentApid": "TH-DGS", "childName": "F_00001", "nameSpace": "dgs"},
     {"parentApid": "TH-DG2", "childName": "F_00001", "nameSpace": "dgs"}],
])
def test_exactly_one_dgs_parent(parents):
    setup_film(parents=parents)
    with pytest.raises(FSError):
        ImageReader(prod()).film_info(ARK)


@responses.activate
def test_picks_dgs_among_parents():
    setup_film(parents=[{"parentApid": "TH-OTH", "nameSpace": "x"},
                        {"parentApid": "TH-DGS", "nameSpace": "dgs"}])
    assert ImageReader(prod()).film_info(ARK)["dgs_apid"] == "TH-DGS"


@responses.activate
def test_child_count_cap():
    setup_film(count=20001)
    with pytest.raises(FSError):
        ImageReader(prod()).film_info(ARK)


@responses.activate
def test_short_page_rejected():
    setup_film(n=5, kids=[{"apid": "TH-IMG", "order": 1}, {"apid": "B", "order": 2}])
    with pytest.raises(FSError):
        ImageReader(prod()).film_info(ARK)


@responses.activate
def test_listing_cached_per_reader():
    setup_film()
    r = ImageReader(prod())
    a = r.list_film_images(ARK)
    a[0]["apid"] = "mutated"
    b = r.list_film_images(ARK)
    assert b[0]["apid"] == "TH-P1"
    kids_calls = [c for c in responses.calls if "/children" in c.request.url]
    assert len(kids_calls) == 1


@responses.activate
def test_list_ordered():
    setup_film()
    rows = ImageReader(prod()).list_film_images(ARK)
    assert [r["order"] for r in rows] == [1, 2, 3, 4, 5]
    assert rows[0] == {"order": 1, "name": "FILM1_00001", "apid": "TH-P1"}


@responses.activate
@pytest.mark.parametrize("kids", [
    [{"apid": "A", "order": 1}, {"apid": "B", "order": 1}],
    [{"apid": "A", "order": 1}, {"apid": "A", "order": 2}],
    [{"apid": "A", "order": 1}, {"apid": "B", "order": 3}],
    [{"apid": "A", "order": 1}, {"apid": "B"}],
    [{"apid": "A", "order": 1}, {"apid": "B/../x", "order": 2}],
    [{"apid": "A", "order": 1}, {"apid": "B١", "order": 2}],
    [{"apid": "A", "order": 1}, {"apid": 7, "order": 2}],
])
def test_list_rejects_bad(kids):
    setup_film(n=2, kids=kids, child="F_00001")
    with pytest.raises(FSError):
        ImageReader(prod()).list_film_images(ARK)


@responses.activate
def test_evil_parents_link_never_requested():
    responses.add(responses.GET, WWW + ARK, json={"apid": "TH-IMG", "link": [
        {"rel": "parents", "href": "https://evil.example/service/records/storage/"
                                   "dascloud/das/v2/TH-IMG/parents"}]})
    with pytest.raises(FSError):
        ImageReader(prod()).film_info(ARK)
    assert len(responses.calls) == 1


@responses.activate
@pytest.mark.parametrize("index", [0, 6, -1, True, "2"])
def test_fetch_index_bounds(index):
    setup_film()
    with pytest.raises(ValueError):
        ImageReader(prod()).fetch_film_image(ARK, index)
    assert not any("dist.jpg" in c.request.url for c in responses.calls)


@responses.activate
def test_fetch_film_image_url_and_size():
    setup_film()
    responses.add(responses.GET, DAS + "TH-P2/thumb_p200.jpg", body=JPEG,
                  content_type="image/jpeg")
    assert ImageReader(prod()).fetch_film_image(ARK, 2, "thumb_p200") == (JPEG, "image/jpeg")
    assert responses.calls[-1].request.url == DAS + "TH-P2/thumb_p200.jpg"
    with pytest.raises(ValueError):
        ImageReader(prod()).fetch_film_image(ARK, 2, "deepzoom")


@responses.activate
def test_fetch_range_and_cap():
    setup_film()
    for i in (2, 3):
        responses.add(responses.GET, DAS + f"{'TH-IMG' if i == 3 else f'TH-P{i}'}/dist.jpg", body=JPEG,
                      content_type="image/jpeg")
    got = list(ImageReader(prod()).fetch_film_range(ARK, 2, 3))
    assert [g[0] for g in got] == [2, 3]
    with pytest.raises(ValueError, match="40"):
        list(ImageReader(prod()).fetch_film_range(ARK, 1, 41))
    with pytest.raises(ValueError):
        list(ImageReader(prod()).fetch_film_range(ARK, 3, 2))
    with pytest.raises(ValueError):
        list(ImageReader(prod()).fetch_film_range(ARK, 4, 6))


# ---- MCP wiring ----

def test_mcp_film_tools():
    from mcp.server.fastmcp import Image
    from familysearch_mcp.tools_read import register_read_tools

    class Obj:
        pass

    class MCP:
        def __init__(self):
            self.tools = {}

        def tool(self):
            return lambda f: self.tools.setdefault(f.__name__, f)

    s = Obj()
    s.client = Obj()
    s.client.base_url = WWW
    fetched = []

    def get_capped(url, accept=None, max_bytes=0):
        return json.dumps({"apid": "TH-IMG", "link": [
            {"rel": "parents", "href": DAS + "TH-IMG/parents"}]}).encode()

    def get_das_json(url):
        if url.endswith("/parents"):
            return {"parents": [{"parentApid": "TH-DGS", "childName": "F_00003",
                                 "nameSpace": "dgs"}]}
        if url.endswith("TH-DGS"):
            return {"name": "F" * 60, "childCount": 5}
        return {"children": [{"apid": "TH-IMG" if i == 3 else f"TH-P{i}", "order": i}
                             for i in range(1, 6)]}

    s.client.get_capped = get_capped
    s.client.get_das_json = get_das_json
    s.client.get_image = lambda url: (fetched.append(url), (b"IMG", "image/jpeg"))[1]

    class Ctx:
        def call(self, fn):
            return fn(s)

    mcp = MCP()
    register_read_tools(mcp, Ctx())
    t = mcp.tools
    info = t["get_film_info"]("ark:/61903/3:1:ABCD-1")
    assert info["total_images"] == 5 and len(info["film"]) == 40
    out = t["get_film_image"]("ark:/61903/3:1:ABCD-1", offset=-1)
    assert isinstance(out, Image) and fetched[-1].endswith("/TH-P2/dist.jpg")
    t["get_film_image"]("ark:/61903/3:1:ABCD-1", index=5, offset=-1, size="thumb_p200")
    assert fetched[-1].endswith("/TH-P5/thumb_p200.jpg")
    with pytest.raises(ValueError):
        t["get_film_image"]("ark:/61903/3:1:ABCD-1", offset=5)
    with pytest.raises(FSError):
        t["get_film_info"]("nope")
    with pytest.raises(ValueError):
        t["get_film_image"]("ark:/61903/3:1:ABCD-1", size="x")


# ---- film_pages CLI ----

def test_film_pages_refuses_repo_output(tmp_path):
    from tools.film import film_pages as fp

    with pytest.raises(SystemExit):
        fp.run(["ark:/61903/3:1:ABCD-1", "--first", "1", "--last", "1",
                "--out", str(fp.REPO_ROOT / "out")], client=object())
    with pytest.raises(SystemExit):
        fp.check_out_dir(fp.REPO_ROOT)
    assert fp.check_out_dir(tmp_path) == tmp_path.resolve()


def test_film_pages_box_and_crop(tmp_path):
    PIL = pytest.importorskip("PIL.Image")
    import io
    from tools.film import film_pages as fp

    assert fp.parse_box(None) == fp.DEFAULT_BOX
    assert fp.parse_box("0,0,0.5,0.5") == (0, 0, 0.5, 0.5)
    with pytest.raises(SystemExit):
        fp.parse_box("0.5,0,0.2,1")
    buf = io.BytesIO()
    PIL.new("RGB", (100, 200), "white").save(buf, "JPEG")
    im = fp.crop_image(buf.getvalue(), (0.1, 0.25, 0.5, 0.75))
    assert im.size == (40, 100)

    class Reader:
        def __init__(self, c):
            pass

        def fetch_film_range(self, ark, a, b, size):
            for i in range(a, b + 1):
                yield i, buf.getvalue(), "image/jpeg"

    fp.ImageReader = Reader
    try:
        fp.run(["ark:/61903/3:1:ABCD-1", "--first", "4", "--last", "5", "--out",
                str(tmp_path), "--crop", "left-parents", "--montage"], client=object())
    finally:
        from familysearch.images import ImageReader as IR
        fp.ImageReader = IR
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "film_00004.jpg", "film_00005.jpg", "montage_00004_00005.jpg"]


def test_check_out_dir_case_symlink_and_worktree(tmp_path):
    import os
    from tools.film import film_pages as fp

    swapped = str(fp.REPO_ROOT).swapcase()
    if os.path.exists(swapped):  # case-insensitive filesystem
        with pytest.raises(SystemExit):
            fp.check_out_dir(swapped + "/sub")
    link = tmp_path / "lnk"
    link.symlink_to(fp.REPO_ROOT)
    with pytest.raises(SystemExit):
        fp.check_out_dir(link / "sub")
    other = tmp_path / "wt"
    (other / ".git").mkdir(parents=True)
    with pytest.raises(SystemExit):
        fp.check_out_dir(other / "out")


def test_write_file_excl_nofollow_overwrite(tmp_path):
    from tools.film import film_pages as fp

    fp.write_file(tmp_path, "a.jpg", b"1", False)
    with pytest.raises(SystemExit):
        fp.write_file(tmp_path, "a.jpg", b"2", False)
    fp.write_file(tmp_path, "a.jpg", b"3", True)
    assert (tmp_path / "a.jpg").read_bytes() == b"3"
    target = tmp_path / "target"
    target.write_bytes(b"keep")
    (tmp_path / "l.jpg").symlink_to(target)
    for ow in (False, True):
        with pytest.raises(SystemExit):
            fp.write_file(tmp_path, "l.jpg", b"x", ow)
    assert target.read_bytes() == b"keep" and (tmp_path / "l.jpg").is_symlink()


def test_extension_from_content_type():
    from tools.film import film_pages as fp

    assert fp.ext_for("image/png") == ".png" and fp.ext_for("image/jpeg") == ".jpg"
    assert fp.ext_for("image/webp") == ".webp" and fp.ext_for("image/gif") == ".gif"


def test_pillow_limits_and_odd_montage(tmp_path):
    PIL = pytest.importorskip("PIL.Image")
    import io
    from tools.film import film_pages as fp

    assert PIL.MAX_IMAGE_PIXELS == 100_000_000
    buf = io.BytesIO()
    PIL.new("RGB", (50, 50), "white").save(buf, "JPEG")

    class Reader:
        def __init__(self, c):
            pass

        def fetch_film_range(self, ark, a, b, size):
            for i in range(a, b + 1):
                yield i, buf.getvalue(), "image/jpeg"

    orig = fp.ImageReader
    fp.ImageReader = Reader
    try:
        fp.run(["ark:/61903/3:1:ABCD-1", "--first", "1", "--last", "3", "--out",
                str(tmp_path), "--crop", "left-parents", "--montage"], client=object())
    finally:
        fp.ImageReader = orig
    assert "montage_00003_00003.jpg" in {p.name for p in tmp_path.iterdir()}


def test_cli_short_error(tmp_path, capsys):
    from tools.film import film_pages as fp
    from familysearch.errors import FSError

    class Reader:
        def __init__(self, c):
            pass

        def fetch_film_range(self, *a):
            raise FSError("boom")
            yield

    orig = fp.ImageReader
    fp.ImageReader = Reader
    try:
        rc = fp.run(["ark:/61903/3:1:ABCD-1", "--first", "1", "--last", "1",
                     "--out", str(tmp_path)], client=object())
    finally:
        fp.ImageReader = orig
    assert rc == 1 and "boom" in capsys.readouterr().err
