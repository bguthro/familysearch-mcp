import pytest
import responses

from familysearch.auth import Environment
from familysearch.client import FSClient
from familysearch.errors import FSError
from familysearch.images import ImageReader, parse_ark, validate_image_url

TOKEN = "sekrit-token-123"
WWW = "https://www.familysearch.org"
ARK = "/ark:/61903/3:1:ABCD-EF1-2G3"
GOOD = ("https://sg30p0.familysearch.org/service/records/storage/dascloud/"
        "das/v2/APID-123/dist.jpg")
THUMB = GOOD.replace("dist.jpg", "thumb_p200.jpg")
INFO = {"name": "Fictional Record", "apid": "APID-123", "link": [
    {"rel": "stream-image-dist", "href": GOOD},
    {"rel": "stream-image-thumb_p200", "href": THUMB},
    {"rel": "deepzoom", "href": "https://sg30p0.familysearch.org/zoom"},
]}


JPEG = b"\xff\xd8\xff\xe0" + b"0" * 20


def prod():
    return FSClient(TOKEN, Environment.PRODUCTION, min_write_interval=0)


# ---- parse_ark ----

@pytest.mark.parametrize("v", [
    "https://www.familysearch.org/ark:/61903/3:1:ABCD-EF1-2G3?wc=xyz&cc=1",
    "/ark:/61903/3:1:ABCD-EF1-2G3",
    "ark:/61903/3:1:ABCD-EF1-2G3",
])
def test_parse_ark_ok(v):
    assert parse_ark(v) == ARK


@pytest.mark.parametrize("v", [
    "", " ark:/61903/3:1:ABCD", "ark:/61903/1:1:ABCD-EF1", "ark:/61903/3:1:abcd", "ark:/61903/3:1:AB/../CD",
    "ark:/61903/3:1:", "ark:/61903/3:1:AB--", "https://evil.example/ark:/61903/3:1:ABCD",
    "http://www.familysearch.org/ark:/61903/3:1:ABCD",
    "https://user@www.familysearch.org/ark:/61903/3:1:ABCD",
    "https://www.familysearch.org:8443/ark:/61903/3:1:ABCD", None,
])
def test_parse_ark_bad(v):
    with pytest.raises(ValueError):
        parse_ark(v)


# ---- URL validator ----

def test_validator_accepts_good():
    assert validate_image_url(GOOD) == GOOD
    assert validate_image_url(THUMB) == THUMB


@pytest.mark.parametrize("u", [
    GOOD.replace("https", "http"),
    GOOD.replace("sg30p0.familysearch.org", "evil.example"),
    GOOD.replace("sg30p0.familysearch.org", "www.familysearch.org.evil.com"),
    GOOD.replace("sg30p0.familysearch.org", "sg30p0.familysearch.org.evil.com"),
    GOOD.replace("sg30p0.familysearch.org", "user:pw@sg30p0.familysearch.org"),
    GOOD.replace("sg30p0.familysearch.org", "evil.example@sg30p0.familysearch.org"),
    GOOD.replace("sg30p0.familysearch.org", "sg30p0.familysearch.org:8443"),
    GOOD.replace("sg30p0.familysearch.org", "sg30p0.familysearch.org:443"),
    GOOD + "?x=1",
    GOOD + "#frag",
    GOOD.replace("/das/v2/", "/das/v3/"),
    GOOD.replace("dist.jpg", "dist.png"),
    GOOD.replace("dist.jpg", "thumb_p100.jpg"),
    GOOD.replace("APID-123", "A/../B"),
    GOOD.replace("sg30p0", "sgxp0"),
    GOOD + "\n", GOOD.replace("/das/", "/das\t/"), " " + GOOD, GOOD + " ",
    GOOD.replace("sg30p0", "sg\uff13p1"), GOOD.replace("APID", "AP\u0661D"),
    "", None,
])
def test_validator_rejects(u):
    with pytest.raises(FSError):
        validate_image_url(u)


# ---- reader / client ----

def add_info():
    responses.add(responses.GET, WWW + ARK, json=INFO)


@responses.activate
def test_info_has_no_credentials_or_hrefs():
    add_info()
    info = ImageReader(prod()).get_image_info(
        WWW + ARK + "?wc=abc")
    assert info["name"] == "Fictional Record" and info["apid"] == "APID-123"
    assert "dist" in info["fetchable_sizes"] and "deepzoom" in info["sizes"]
    assert TOKEN not in repr(info) and "http" not in repr(info)
    assert responses.calls[0].request.headers["Accept"] == "application/json"


@responses.activate
@pytest.mark.parametrize("size,url", [("dist", GOOD), ("thumb_p200", THUMB)])
def test_fetch_ok(size, url):
    add_info()
    responses.add(responses.GET, url, body=JPEG, content_type="image/jpeg; charset=binary")
    data, ctype = ImageReader(prod()).fetch_image(ARK, size)
    assert (data, ctype) == (JPEG, "image/jpeg")
    sent = responses.calls[1].request
    assert sent.url == url and sent.headers["Authorization"] == f"Bearer {TOKEN}"


def test_bad_size():
    with pytest.raises(ValueError):
        ImageReader(prod()).fetch_image(ARK, "deepzoom")


@responses.activate
def test_beta_refused():
    beta = FSClient(TOKEN, Environment.BETA, min_write_interval=0)
    with pytest.raises(FSError, match="production"):
        ImageReader(beta).fetch_image(ARK)
    with pytest.raises(FSError, match="production"):
        beta.get_image(GOOD)
    assert not responses.calls


@responses.activate
def test_evil_href_never_requested():
    body = {"link": [{"rel": "stream-image-dist",
                      "href": "https://evil.example/service/records/storage/"
                              "dascloud/das/v2/A/dist.jpg"}]}
    responses.add(responses.GET, WWW + ARK, json=body)
    with pytest.raises(FSError) as ei:
        ImageReader(prod()).fetch_image(ARK)
    assert TOKEN not in str(ei.value)
    assert len(responses.calls) == 1


@responses.activate
def test_non_image_refused():
    responses.add(responses.GET, GOOD, body=b"<html>", content_type="text/html")
    with pytest.raises(FSError, match="image") as ei:
        prod().get_image(GOOD)
    assert TOKEN not in str(ei.value)


@responses.activate
def test_non_200_refused():
    responses.add(responses.GET, GOOD, status=404, body="nope")
    with pytest.raises(FSError, match="404") as ei:
        prod().get_image(GOOD)
    assert TOKEN not in str(ei.value)


@responses.activate
def test_oversize_refused(monkeypatch):
    import familysearch.images as im
    monkeypatch.setattr(im, "MAX_IMAGE_BYTES", 10)
    responses.add(responses.GET, GOOD, body=JPEG + b"x" * 100, content_type="image/jpeg")
    with pytest.raises(FSError, match="size limit") as ei:
        prod().get_image(GOOD)
    assert TOKEN not in str(ei.value)


def test_get_image_revalidates_url():
    with pytest.raises(FSError):
        prod().get_image("https://evil.example/service/records/storage/"
                         "dascloud/das/v2/A/dist.jpg")


@responses.activate
def test_generic_request_guard_unchanged():
    c = prod()
    for u in (GOOD, "https://sg30p0.familysearch.org/x", "https://evil.example/a"):
        with pytest.raises(FSError, match="refusing to send credentials") as ei:
            c.get(u)
        assert TOKEN not in str(ei.value)
    assert not responses.calls


@pytest.mark.parametrize("v", [
    "ark:/61903/3:1:ABCD\n", "ark:/61903/3:1:AB\tCD", "ark:/61903/3:1:AB\uff11",
    "ark:/61903/3:1:ABCD\n?x=1",
])
def test_parse_ark_whitespace_unicode(v):
    with pytest.raises(ValueError):
        parse_ark(v)


@responses.activate
@pytest.mark.parametrize("ctype", [
    "image/svg+xml", "image/", "image", "text/html", "image/tiff", "",
])
def test_disallowed_types(ctype):
    responses.add(responses.GET, GOOD, body=JPEG, content_type=ctype or None)
    with pytest.raises(FSError):
        prod().get_image(GOOD)


@responses.activate
@pytest.mark.parametrize("ctype,body", [
    ("image/png", b"\x89PNG\r\n\x1a\n"), ("image/gif", b"GIF89a.."),
    ("image/webp", b"RIFF\x00\x00\x00\x00WEBPVP8 "),
])
def test_other_types_ok(ctype, body):
    responses.add(responses.GET, GOOD, body=body, content_type=ctype)
    assert prod().get_image(GOOD) == (body, ctype)


@responses.activate
@pytest.mark.parametrize("ctype,body", [
    ("image/jpeg", b"\x89PNG....."), ("image/png", JPEG), ("image/gif", b"<svg>"),
    ("image/webp", b"RIFF\x00\x00\x00\x00WAVE"),
])
def test_magic_mismatch(ctype, body):
    responses.add(responses.GET, GOOD, body=body, content_type=ctype)
    with pytest.raises(FSError, match="match"):
        prod().get_image(GOOD)


@responses.activate
def test_metadata_capped(monkeypatch):
    import familysearch.images as im
    monkeypatch.setattr(im, "MAX_META_BYTES", 50)
    responses.add(responses.GET, WWW + ARK, body=b"{" + b" " * 200 + b"}",
                  content_type="application/json")
    with pytest.raises(FSError, match="size limit"):
        ImageReader(prod()).get_image_info(ARK)


def test_default_caps():
    import familysearch.images as im
    assert im.MAX_IMAGE_BYTES == 15 * 1024 * 1024
    assert im.MAX_META_BYTES == 1024 * 1024
    assert im.IMAGE_DEADLINE_SECONDS == 60


@responses.activate
def test_deadline(monkeypatch):
    import time as t
    responses.add(responses.GET, GOOD, body=JPEG, content_type="image/jpeg")
    clock = iter([1000.0, 1000.0 + 61, 1000.0 + 62, 1000.0 + 63])
    monkeypatch.setattr(t, "monotonic", lambda: next(clock))
    with pytest.raises(FSError, match="time limit"):
        prod().get_image(GOOD)


S3HOST = "ps-services-us-east-1-914248642252-pipe-storage-das-cloud.s3.amazonaws.com"
S3PATH = "/s3/pipe-storage-das-cloud-prod-dasS3/APID-123/dist.jpg"
S3URL = f"https://{S3HOST}{S3PATH}?X-Amz-Signature=abc123&X-Amz-Expires=300"


def add_hop1(location=S3URL, status=302):
    responses.add(responses.GET, GOOD, status=status, headers={"Location": location})


@responses.activate
@pytest.mark.parametrize("status", [301, 302, 303, 307])
def test_s3_redirect_followed_without_credentials(status):
    add_hop1(status=status)
    responses.add(responses.GET, S3URL, body=JPEG, content_type="image/jpeg")
    assert prod().get_image(GOOD) == (JPEG, "image/jpeg")
    first, second = (c.request for c in responses.calls)
    assert first.headers["Authorization"] == f"Bearer {TOKEN}"
    assert second.url == S3URL
    assert "Authorization" not in second.headers and "Cookie" not in second.headers
    assert TOKEN not in str(dict(second.headers))


@responses.activate
def test_s3_thumb_same_filename():
    t = S3URL.replace("dist.jpg", "thumb_p200.jpg")
    responses.add(responses.GET, THUMB, status=302, headers={"Location": t})
    responses.add(responses.GET, t, body=JPEG, content_type="image/jpeg")
    assert prod().get_image(THUMB)[0] == JPEG


BAD_LOC = [
    S3URL.replace("https", "http"),
    S3URL.replace(S3HOST, "ps-services-x.s3.amazonaws.com.evil.com"),
    S3URL.replace(S3HOST, "evil.example"),
    S3URL.replace(S3HOST, "s3.amazonaws.com"),
    S3URL.replace("pipe-storage-das-cloud-prod-dasS3", "other-bucket"),
    S3URL.replace("APID-123", "APID-999"),
    S3URL.replace("dist.jpg", "thumb_p200.jpg"),
    S3URL.replace(S3HOST, "user:pw@" + S3HOST),
    S3URL.replace(S3HOST, S3HOST + ":8443"),
    S3URL.replace(S3HOST, S3HOST + ":443"),
    S3URL.split("?")[0],
    S3URL.split("?")[0] + "?",
    S3URL + "#frag",
    S3URL + " x",
    S3URL + "\n",
    S3URL.replace("abc123", "ab\tc"),
    " " + S3URL,
    S3URL + "&p=" + "a" * 4100,
    "",
]


@responses.activate
@pytest.mark.parametrize("loc", BAD_LOC)
def test_bad_redirects_refused(loc):
    add_hop1(loc)
    with pytest.raises(FSError) as ei:
        prod().get_image(GOOD)
    msg = str(ei.value)
    assert TOKEN not in msg and "amazonaws" not in msg and "evil" not in msg
    assert len(responses.calls) == 1


@responses.activate
def test_second_redirect_refused():
    add_hop1()
    responses.add(responses.GET, S3URL, status=302,
                  headers={"Location": "https://evil.example/x"})
    with pytest.raises(FSError, match="redirect") as ei:
        prod().get_image(GOOD)
    assert TOKEN not in str(ei.value) and len(responses.calls) == 2


@responses.activate
def test_s3_hop_still_checks_type_magic_status():
    add_hop1()
    responses.add(responses.GET, S3URL, body=b"<html>", content_type="text/html")
    with pytest.raises(FSError):
        prod().get_image(GOOD)
    responses.replace(responses.GET, S3URL, body=b"<html>", content_type="image/jpeg")
    with pytest.raises(FSError, match="match"):
        prod().get_image(GOOD)
    responses.replace(responses.GET, S3URL, status=403, body="denied")
    with pytest.raises(FSError, match="403") as ei:
        prod().get_image(GOOD)
    assert TOKEN not in str(ei.value)


def test_s3_validator_unicode_and_good():
    from familysearch.images import validate_s3_location
    assert validate_s3_location(S3URL, GOOD) == S3URL
    with pytest.raises(FSError):
        validate_s3_location(S3URL.replace("ps-services", "ps-s\uff13rvices"), GOOD)
    with pytest.raises(FSError):
        validate_s3_location(S3URL.replace("APID", "AP\u0661D"), GOOD)
