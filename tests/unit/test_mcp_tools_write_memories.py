import builtins
import os
import stat

import pytest

from familysearch import Memories
from familysearch_mcp.context import ContextError
from familysearch_mcp.tools_write import register_write_tools

REASON = "Adding from research archive folder smith-john"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
JPG = b"\xff\xd8\xff\xe0" + b"0" * 32
TIF_LE = b"II*\x00" + b"0" * 32
TIF_BE = b"MM\x00*" + b"0" * 32
PDF = b"%PDF-1.4\n" + b"0" * 32
MAX = Memories.MAX_BYTES


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class Rec:
    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class Obj:
    pass


class FakeDiscovery:
    def __init__(self, href):
        self._href = href
        self.asked = []

    def href(self, rel):
        self.asked.append(rel)
        return self._href


def make_services():
    s = Obj()
    s.memories = Obj()
    s.memories.upload = Rec("MEM123")
    s.memories.link_to_person = Rec(None)
    s.sources = Obj()
    s.sources.create_description = Rec("71FD-SL2")
    s.sources.attach_to_person = Rec(None)
    s.discovery = FakeDiscovery("https://api.example.org/platform/sources/descriptions?flag=fsh")
    return s


class FakeCtx:
    def __init__(self, services, writes=True):
        self.services = services
        self.writes = writes
        self.call_count = 0

    def require_writes(self):
        if not self.writes:
            raise ContextError("Writes are disabled on production.")

    def call(self, fn):
        self.call_count += 1
        return fn(self.services)


def build(writes=True):
    svc = make_services()
    ctx = FakeCtx(svc, writes=writes)
    mcp = FakeMCP()
    register_write_tools(mcp, ctx)
    return mcp.tools, svc, ctx


def total_calls(s):
    return (len(s.memories.upload.calls) + len(s.memories.link_to_person.calls)
            + len(s.sources.create_description.calls) + len(s.sources.attach_to_person.calls))


@pytest.fixture(autouse=True)
def upload_root(tmp_path, monkeypatch):
    """Uploads require FS_UPLOAD_ROOT; point it at tmp_path for every test."""
    monkeypatch.setenv("FS_UPLOAD_ROOT", str(tmp_path))
    return tmp_path


class OpenSpy:
    def __init__(self):
        self.paths = []
        self.fds = []


@pytest.fixture
def opens(monkeypatch):
    """Record every os.open / builtins.open path and every fd os.open returns."""
    spy = OpenSpy()
    real_os_open, real_open = os.open, builtins.open

    def os_open(path, *a, **k):
        spy.paths.append(os.fspath(path))
        fd = real_os_open(path, *a, **k)
        spy.fds.append(fd)
        return fd

    def b_open(file, *a, **k):
        if isinstance(file, (str, bytes, os.PathLike)):
            spy.paths.append(os.fspath(file))
        return real_open(file, *a, **k)

    monkeypatch.setattr(os, "open", os_open)
    monkeypatch.setattr(builtins, "open", b_open)
    return spy


def fd_closed(fd):
    try:
        os.fstat(fd)
    except OSError:
        return True
    return False


def mk(tmp_path, name, content=PDF):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return str(p)


def upload(tools, path, **kw):
    kw.setdefault("provenance", "NARA")
    return tools["upload_memory"](path, REASON, **kw)


ALL_NEW = {
    "upload_memory": dict(file_path="/nonexistent/x.pdf", reason=REASON, provenance="NARA"),
    "link_memory": dict(person_id="GKLV-DY8", memory_id="123", reason=REASON),
    "create_source": dict(about_url="https://example.org/x", title="T", citation="C",
                          reason=REASON),
    "attach_source": dict(person_id="GKLV-DY8", source_id="71FD-SL2", reason=REASON),
}


# ---- registration -----------------------------------------------------------

def test_registers_exactly_eight_tools():
    tools, _, _ = build()
    assert set(tools) == {
        "create_person", "add_fact", "create_couple", "create_parent_child",
        "upload_memory", "link_memory", "create_source", "attach_source",
    }
    for name, fn in tools.items():
        assert fn.__doc__ and fn.__doc__.strip(), name
        assert "delete" not in name and "merge" not in name, name
    doc = tools["upload_memory"].__doc__
    assert "Ben" in doc and "override_rights" in doc and "provenance" in doc
    assert "FS_UPLOAD_ROOT" in doc and "15 MB" in doc and "PUBLIC" in doc.upper()
    for agg in ("Fold3", "Ancestry", "Newspapers.com", "MyHeritage", "Findagrave"):
        assert agg in doc


@pytest.mark.parametrize("name", list(ALL_NEW))
def test_writes_disabled(name):
    tools, svc, ctx = build(writes=False)
    out = tools[name](**ALL_NEW[name])
    assert out["status"] == "writes_disabled" and out["message"]
    assert total_calls(svc) == 0 and ctx.call_count == 0


def test_writes_disabled_before_validation():
    tools, svc, ctx = build(writes=False)
    out = tools["upload_memory"]("\x00bad", "", override_rights="yes")
    assert out["status"] == "writes_disabled"
    assert tools["link_memory"]("junk", None, None)["status"] == "writes_disabled"
    assert total_calls(svc) == 0 and ctx.call_count == 0


# ---- upload happy paths -----------------------------------------------------

@pytest.mark.parametrize("name,content,ct", [
    ("a.png", PNG, "image/png"),
    ("a.jpg", JPG, "image/jpeg"),
    ("a.jpeg", JPG, "image/jpeg"),
    ("a.tif", TIF_LE, "image/tiff"),
    ("a.tiff", TIF_BE, "image/tiff"),
    ("a.pdf", PDF, "application/pdf"),
    ("A.PDF", PDF, "application/pdf"),
    ("A.JpG", JPG, "image/jpeg"),
])
def test_upload_happy(tmp_path, name, content, ct):
    tools, svc, ctx = build()
    path = mk(tmp_path, name, content)
    out = tools["upload_memory"](path, "  " + REASON + "  ", provenance="NARA")
    assert out == {"status": "uploaded", "memory_id": "MEM123", "filename": name,
                   "content_type": ct, "bytes": len(content), "rights_override": False}
    (args, kw), = svc.memories.upload.calls
    assert args == (content,)
    assert kw == dict(filename=name, content_type=ct, title=name, reason=REASON)
    assert ctx.call_count == 1


def test_upload_explicit_title_and_strip(tmp_path):
    tools, svc, _ = build()
    path = mk(tmp_path, "a.pdf")
    upload(tools, path, title="  1900 Census, Smith  ")
    assert svc.memories.upload.calls[0][1]["title"] == "1900 Census, Smith"


def test_upload_tilde_expansion(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    mk(home, "a.pdf")
    tools, svc, _ = build()
    assert upload(tools, "~/a.pdf")["status"] == "uploaded"


def test_upload_filename_with_space(tmp_path):
    tools, svc, _ = build()
    out = upload(tools, mk(tmp_path, "my scan.png", PNG))
    assert out["status"] == "uploaded" and out["filename"] == "my scan.png"


# ---- provenance gate ----------------------------------------------------------

def test_filename_aggregator_blocked_before_reading_content(tmp_path):
    tools, svc, _ = build()
    path = mk(tmp_path, "1940 Fold3 Muster Roll.jpg", b"this is not a jpeg at all")
    out = upload(tools, path)
    assert out["status"] == "provenance_blocked"
    assert out["aggregator"] == "fold3"
    assert out["file"] == "1940 Fold3 Muster Roll.jpg"
    assert "override_rights" in out["message"] and "Ben" in out["message"]
    assert svc.memories.upload.calls == []


def test_gate_runs_before_any_file_open(tmp_path, opens):
    tools, svc, _ = build()
    path = mk(tmp_path, "Fold3 x.jpg", JPG)
    assert upload(tools, path)["status"] == "provenance_blocked"
    assert upload(tools, mk(tmp_path, "clean.jpg", JPG), provenance=None)["status"] \
        == "provenance_unknown"
    assert not [f for f in opens.paths if f.startswith(os.path.realpath(tmp_path))]


def test_provenance_text_aggregator_blocked(tmp_path):
    tools, svc, _ = build()
    out = upload(tools, mk(tmp_path, "clean.pdf"), provenance="Ancestry.com")
    assert out["status"] == "provenance_blocked" and out["aggregator"] == "ancestry"
    assert svc.memories.upload.calls == []


def test_unicode_filename_blocked(tmp_path):
    tools, svc, _ = build()
    out = upload(tools, mk(tmp_path, "Fóld3.jpg", JPG))
    assert out["status"] == "provenance_blocked"
    assert svc.memories.upload.calls == []


def test_symlink_to_aggregator_file_blocked(tmp_path):
    tools, svc, _ = build()
    real = mk(tmp_path, "Fold3 x.pdf")
    link = tmp_path / "clean.pdf"
    os.symlink(real, link)
    out = upload(tools, str(link))
    assert out["status"] == "provenance_blocked" and out["aggregator"] == "fold3"
    assert svc.memories.upload.calls == []


def test_aggregator_directory_name_does_not_block(tmp_path):
    tools, svc, _ = build()
    path = mk(tmp_path / "ancestry research" / "fold3 stuff", "census.pdf")
    assert upload(tools, path)["status"] == "uploaded"


@pytest.mark.parametrize("prov", [None, "", "   "])
def test_unknown_provenance(tmp_path, prov):
    tools, svc, _ = build()
    out = upload(tools, mk(tmp_path, "clean.pdf"), provenance=prov)
    assert out["status"] == "provenance_unknown" and out["file"] == "clean.pdf"
    assert "provenance" in out["message"] and "override_rights" in out["message"]
    assert svc.memories.upload.calls == []


def test_override_with_aggregator(tmp_path):
    tools, svc, _ = build()
    path = mk(tmp_path, "Fold3 x.pdf")
    out = upload(tools, path, override_rights=True)
    assert out["status"] == "uploaded"
    assert out["rights_override"] is True and out["overridden_aggregator"] == "fold3"
    assert len(svc.memories.upload.calls) == 1


def test_override_with_unknown_provenance(tmp_path):
    tools, svc, _ = build()
    out = upload(tools, mk(tmp_path, "clean.pdf"), provenance=None, override_rights=True)
    assert out["status"] == "uploaded" and out["rights_override"] is True
    assert "overridden_aggregator" not in out


def test_override_still_checks_magic(tmp_path):
    tools, svc, _ = build()
    with pytest.raises(ValueError, match="magic"):
        upload(tools, mk(tmp_path, "Fold3 x.pdf", b"nope"), override_rights=True)
    assert svc.memories.upload.calls == []


@pytest.mark.parametrize("bad", ["yes", 1, 0, None, "True"])
def test_override_must_be_bool(tmp_path, bad):
    tools, svc, _ = build()
    with pytest.raises(ValueError):
        upload(tools, mk(tmp_path, "a.pdf"), override_rights=bad)
    assert svc.memories.upload.calls == []


# ---- file safety --------------------------------------------------------------

def refused(tools, svc, path, **kw):
    with pytest.raises(ValueError):
        upload(tools, path, **kw)
    assert svc.memories.upload.calls == []


def test_nonexistent(tmp_path):
    tools, svc, _ = build()
    refused(tools, svc, str(tmp_path / "missing.pdf"))


def test_directory(tmp_path):
    tools, svc, _ = build()
    d = tmp_path / "dir.pdf"
    d.mkdir()
    refused(tools, svc, str(d))


def test_dev_zero_rejected_without_open(tmp_path, monkeypatch, opens):
    tools, svc, _ = build()
    # Root /dev admits /dev/zero (and a symlink to it); the S_ISREG check is the
    # only thing that stops it, and it runs before any open.
    monkeypatch.setenv("FS_UPLOAD_ROOT", "/dev")
    with pytest.raises(ValueError, match="regular file"):
        upload(tools, "/dev/zero")
    link = tmp_path / "z.pdf"
    os.symlink("/dev/zero", link)
    with pytest.raises(ValueError, match="regular file"):
        upload(tools, str(link))
    assert svc.memories.upload.calls == []
    assert not [f for f in opens.paths if f.startswith("/dev/")]


def test_non_regular_with_good_extension_never_opened(tmp_path, monkeypatch, opens):
    """Extension is fine, S_ISREG is the only thing standing in the way."""
    tools, svc, _ = build()
    p = mk(tmp_path, "x.pdf")
    real_stat = os.stat

    class FakeSt:
        st_mode = stat.S_IFCHR | 0o600
        st_size = 100

    def fake_stat(path, *a, **k):
        return FakeSt() if str(path) == os.path.realpath(p) else real_stat(path, *a, **k)

    monkeypatch.setattr(os, "stat", fake_stat)
    refused(tools, svc, p)
    assert os.path.realpath(p) not in opens.paths


def test_fifo(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("no mkfifo")
    tools, svc, _ = build()
    f = tmp_path / "pipe.pdf"
    os.mkfifo(f)
    refused(tools, svc, str(f))  # would hang forever if it were opened


def test_oversize_sparse_file_not_read(tmp_path, opens):
    tools, svc, _ = build()
    p = tmp_path / "big.pdf"
    with open(p, "wb") as f:
        f.write(PDF)
        f.truncate(MAX + 1)
    opens.paths.clear()
    refused(tools, svc, str(p))
    assert os.path.realpath(p) not in opens.paths


def test_exactly_max_size_allowed(tmp_path):
    tools, svc, _ = build()
    p = tmp_path / "ok.pdf"
    with open(p, "wb") as f:
        f.write(PDF)
        f.truncate(MAX)
    assert upload(tools, str(p))["bytes"] == MAX


def test_empty_file(tmp_path):
    tools, svc, _ = build()
    refused(tools, svc, mk(tmp_path, "e.pdf", b""))


@pytest.mark.parametrize("name", ["a.txt", "a.py", "noext", "a.exe", "a.pdf.exe", "a.", ".pdf"])
def test_bad_extension(tmp_path, name):
    tools, svc, _ = build()
    with pytest.raises(ValueError, match=r"\.pdf"):
        upload(tools, mk(tmp_path, name, PDF))
    assert svc.memories.upload.calls == []


@pytest.mark.parametrize("name,content", [
    ("x.pdf", b"just some text, not a pdf"),
    ("x.jpg", PNG),
    ("x.png", JPG),
    ("x.tif", PDF),
    ("x.pdf", PNG),
])
def test_magic_mismatch(tmp_path, name, content):
    tools, svc, _ = build()
    with pytest.raises(ValueError, match="magic"):
        upload(tools, mk(tmp_path, name, content))
    assert svc.memories.upload.calls == []


@pytest.mark.parametrize("bad", ["", "   ", "a\x00b.pdf", "a\nb.pdf", "a b.pdf",
                                 "/" + "x" * 4097 + ".pdf", None, 5, b"/x.pdf"])
def test_bad_path_values(bad):
    tools, svc, _ = build()
    refused(tools, svc, bad)


def test_symlink_to_etc_hosts_has_no_allowed_extension(tmp_path, monkeypatch):
    tools, svc, _ = build()
    monkeypatch.setenv("FS_UPLOAD_ROOT", "/etc")  # containment passes; extension must stop it
    link = tmp_path / "x.pdf"
    os.symlink("/etc/hosts", link)
    with pytest.raises(ValueError, match="allowed extensions"):
        upload(tools, str(link))
    assert svc.memories.upload.calls == []


def test_bad_reason_title_provenance(tmp_path):
    tools, svc, _ = build()
    p = mk(tmp_path, "a.pdf")
    for kw in (dict(title="x" * 2001), dict(title="a\x00b"), dict(provenance="x" * 2001),
               dict(provenance="a\nb"), dict(title=5)):
        refused(tools, svc, p, **kw)
    for bad in ("", "  ", "x" * 501, "a\nb", None):
        with pytest.raises(ValueError):
            tools["upload_memory"](p, bad, provenance="NARA")
    assert svc.memories.upload.calls == []


# ---- FS_UPLOAD_ROOT -------------------------------------------------------------

def test_root_accepts_inside(tmp_path, monkeypatch):
    tools, svc, _ = build()
    monkeypatch.setenv("FS_UPLOAD_ROOT", str(tmp_path))
    assert upload(tools, mk(tmp_path, "sub/a.pdf"))["status"] == "uploaded"


def test_root_accepts_root_relative_tilde(tmp_path, monkeypatch):
    tools, svc, _ = build()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FS_UPLOAD_ROOT", "~/arch")
    assert upload(tools, mk(tmp_path, "arch/a.pdf"))["status"] == "uploaded"


def test_root_rejects_outside(tmp_path, monkeypatch):
    tools, svc, _ = build()
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("FS_UPLOAD_ROOT", str(root))
    refused(tools, svc, mk(tmp_path, "outside.pdf"))


def test_root_rejects_symlink_escape(tmp_path, monkeypatch):
    tools, svc, _ = build()
    root = tmp_path / "root"
    root.mkdir()
    outside = mk(tmp_path, "outside.pdf", PDF)  # regular, .pdf, valid %PDF- header
    os.symlink(outside, root / "x.pdf")
    monkeypatch.setenv("FS_UPLOAD_ROOT", str(root))
    with pytest.raises(ValueError, match="outside the allowed"):
        upload(tools, str(root / "x.pdf"))
    assert svc.memories.upload.calls == []


def test_root_rejects_dotdot(tmp_path, monkeypatch):
    tools, svc, _ = build()
    root = tmp_path / "root"
    root.mkdir()
    mk(tmp_path, "outside.pdf")
    monkeypatch.setenv("FS_UPLOAD_ROOT", str(root))
    refused(tools, svc, str(root / ".." / "outside.pdf"))


def test_root_prefix_trick(tmp_path, monkeypatch):
    tools, svc, _ = build()
    (tmp_path / "a").mkdir()
    monkeypatch.setenv("FS_UPLOAD_ROOT", str(tmp_path / "a"))
    refused(tools, svc, mk(tmp_path, "ab/file.pdf"))


NOT_CONFIGURED = "upload_root_not_configured"
INVALID = "upload_root_invalid"


def _root_case(tmp_path, monkeypatch, kind):
    if kind == "unset":
        monkeypatch.delenv("FS_UPLOAD_ROOT", raising=False)
    elif kind == "blank":
        monkeypatch.setenv("FS_UPLOAD_ROOT", "")
    elif kind == "spaces":
        monkeypatch.setenv("FS_UPLOAD_ROOT", "   ")
    elif kind == "relative":
        monkeypatch.setenv("FS_UPLOAD_ROOT", "archive/dir")
    elif kind == "missing":
        monkeypatch.setenv("FS_UPLOAD_ROOT", str(tmp_path / "nope"))
    elif kind == "notdir":
        monkeypatch.setenv("FS_UPLOAD_ROOT", mk(tmp_path, "file.pdf"))
    elif kind == "slash":
        monkeypatch.setenv("FS_UPLOAD_ROOT", "/")
    elif kind == "home":
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("FS_UPLOAD_ROOT", str(tmp_path))
    elif kind == "home_tilde":
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("FS_UPLOAD_ROOT", "~")


@pytest.mark.parametrize("kind,status", [
    ("unset", NOT_CONFIGURED), ("blank", NOT_CONFIGURED), ("spaces", NOT_CONFIGURED),
    ("relative", INVALID), ("missing", INVALID), ("notdir", INVALID),
    ("slash", INVALID), ("home", INVALID), ("home_tilde", INVALID),
])
def test_upload_root_refusals(tmp_path, monkeypatch, opens, kind, status):
    tools, svc, ctx = build()
    f = mk(tmp_path, "good.pdf")
    _root_case(tmp_path, monkeypatch, kind)
    opens.paths.clear()
    out = upload(tools, f)
    assert out["status"] == status
    assert "Nothing was uploaded" in out["message"] and "Ben" in out["message"]
    assert "FS_UPLOAD_ROOT" in out["message"]
    assert svc.memories.upload.calls == [] and ctx.call_count == 0
    assert not [p for p in opens.paths if p.startswith(os.path.realpath(tmp_path))]


def test_upload_root_messages_are_distinct(tmp_path, monkeypatch):
    tools, svc, _ = build()
    f = mk(tmp_path, "good.pdf")
    msgs = set()
    for kind in ("relative", "missing", "notdir", "slash", "home"):
        _root_case(tmp_path, monkeypatch, kind)
        msgs.add(upload(tools, f)["message"])
    assert len(msgs) == 5


def test_upload_root_checked_before_other_validation(tmp_path, monkeypatch):
    tools, svc, _ = build()
    monkeypatch.delenv("FS_UPLOAD_ROOT", raising=False)
    out = tools["upload_memory"]("\x00junk", "", override_rights="yes")
    assert out["status"] == NOT_CONFIGURED


def test_upload_root_after_write_gate(monkeypatch):
    tools, svc, ctx = build(writes=False)
    monkeypatch.delenv("FS_UPLOAD_ROOT", raising=False)
    assert tools["upload_memory"]("/x.pdf", REASON)["status"] == "writes_disabled"


def test_upload_root_symlinked_root_ok(tmp_path, monkeypatch):
    tools, svc, _ = build()
    real = tmp_path / "real"
    real.mkdir()
    os.symlink(real, tmp_path / "linkroot")
    monkeypatch.setenv("FS_UPLOAD_ROOT", str(tmp_path / "linkroot"))
    assert upload(tools, mk(real, "a.pdf"))["status"] == "uploaded"


# ---- stat-to-open race (R2) ---------------------------------------------------

def _swap_on_gate(monkeypatch, swap):
    from familysearch_mcp import tools_write
    orig = tools_write.provenance_blocked
    state = {"done": False}

    def wrapper(*a, **k):
        if not state["done"]:
            state["done"] = True
            swap()
        return orig(*a, **k)

    monkeypatch.setattr(tools_write, "provenance_blocked", wrapper)


def test_race_swap_to_outside_symlink(tmp_path, monkeypatch, opens):
    tools, svc, _ = build()
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("FS_UPLOAD_ROOT", str(root))
    ok = root / "ok.png"
    ok.write_bytes(PNG)
    outside = tmp_path / "secret.png"
    outside.write_bytes(PNG + b"SECRET")

    def swap():
        ok.unlink()
        os.symlink(outside, ok)

    _swap_on_gate(monkeypatch, swap)
    with pytest.raises(ValueError):
        upload(tools, str(ok))
    assert svc.memories.upload.calls == []
    assert all(fd_closed(fd) for fd in opens.fds)


def test_race_swap_to_different_regular_file(tmp_path, monkeypatch, opens):
    tools, svc, _ = build()
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("FS_UPLOAD_ROOT", str(root))
    ok = root / "ok.png"
    ok.write_bytes(PNG)
    other = root / "other.png"
    other.write_bytes(PNG + b"OTHER")

    def swap():
        os.replace(other, ok)

    _swap_on_gate(monkeypatch, swap)
    with pytest.raises(ValueError):
        upload(tools, str(ok))
    assert svc.memories.upload.calls == []
    assert opens.fds and all(fd_closed(fd) for fd in opens.fds)


def test_no_fd_leak_on_success_and_magic_failure(tmp_path, opens):
    tools, svc, _ = build()
    upload(tools, mk(tmp_path, "a.pdf"))
    with pytest.raises(ValueError, match="magic"):
        upload(tools, mk(tmp_path, "b.pdf", b"nope"))
    assert len(opens.fds) == 2 and all(fd_closed(fd) for fd in opens.fds)


def test_open_uses_nofollow_nonblock(tmp_path, monkeypatch):
    flags = []
    real = os.open

    def spy(path, fl, *a, **k):
        flags.append(fl)
        return real(path, fl, *a, **k)

    monkeypatch.setattr(os, "open", spy)
    tools, _, _ = build()
    upload(tools, mk(tmp_path, "a.pdf"))
    assert flags and flags[0] & os.O_NOFOLLOW and flags[0] & os.O_NONBLOCK


# ---- title gate (R3) ------------------------------------------------------------

def test_title_aggregator_blocked(tmp_path, opens):
    tools, svc, _ = build()
    p = mk(tmp_path, "clean.jpg", JPG)
    opens.paths.clear()
    out = upload(tools, p, title="Fold3 image of draft card")
    assert out["status"] == "provenance_blocked" and out["aggregator"] == "fold3"
    assert out["matched_in"] == "title"
    assert svc.memories.upload.calls == []
    assert os.path.realpath(p) not in opens.paths


def test_title_aggregator_override_uploads(tmp_path):
    tools, svc, _ = build()
    out = upload(tools, mk(tmp_path, "clean.jpg", JPG), title="Fold3 image",
                 override_rights=True)
    assert out["status"] == "uploaded" and out["overridden_aggregator"] == "fold3"
    assert svc.memories.upload.calls[0][1]["title"] == "Fold3 image"


def test_matched_in_file_and_provenance(tmp_path):
    tools, svc, _ = build()
    assert upload(tools, mk(tmp_path, "Fold3 a.pdf"))["matched_in"] == "file"
    assert upload(tools, mk(tmp_path, "clean.pdf"),
                  provenance="Ancestry.com")["matched_in"] == "provenance"
    link = tmp_path / "clean2.pdf"
    os.symlink(mk(tmp_path, "Fold3 b.pdf"), link)
    assert upload(tools, str(link))["matched_in"] == "file"


# ---- bidi controls (R4) -----------------------------------------------------------

@pytest.mark.parametrize("ch", ["‪", "‮", "⁦", "⁩"])
def test_bidi_controls_rejected(tmp_path, ch):
    tools, svc, _ = build()
    with pytest.raises(ValueError):
        upload(tools, mk(tmp_path, "a.pdf"), title="x" + ch + "y")
    with pytest.raises(ValueError):
        tools["create_source"]("https://example.org/x", "T", "c" + ch, REASON)
    assert total_calls(svc) == 0


def test_zwsp_not_rejected():
    tools, svc, _ = build()
    assert tools["create_source"]("https://example.org/x", "T​T", "C", REASON)["status"] \
        == "created"


def test_root_read_at_call_time(tmp_path, monkeypatch):
    tools, svc, _ = build()
    p = mk(tmp_path, "a.pdf")
    assert upload(tools, p)["status"] == "uploaded"
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("FS_UPLOAD_ROOT", str(other))
    with pytest.raises(ValueError):
        upload(tools, p)
    assert len(svc.memories.upload.calls) == 1


# ---- link_memory ------------------------------------------------------------------

def test_link_memory_ok():
    tools, svc, ctx = build()
    out = tools["link_memory"](" gklv-dy8 ", "12345", "  " + REASON + " ")
    assert out == {"status": "linked", "person_id": "GKLV-DY8", "memory_id": "12345"}
    assert svc.memories.link_to_person.calls == [(("GKLV-DY8", "12345"), {"reason": REASON})]


def test_link_memory_int_id_ok():
    tools, svc, _ = build()
    assert tools["link_memory"]("GKLV-DY8", 77, REASON)["memory_id"] == "77"


@pytest.mark.parametrize("mid", ["abc", "12 34", "../1", "１２３", True,
                                 "1" * 21, None, "", "12/3", 1.5, [1]])
def test_link_memory_bad_memory_id(mid):
    tools, svc, _ = build()
    with pytest.raises(ValueError):
        tools["link_memory"]("GKLV-DY8", mid, REASON)
    assert total_calls(svc) == 0


@pytest.mark.parametrize("pid", ["junk", None, "../x", "GKLV-DY8/x"])
def test_link_memory_bad_person_id(pid):
    tools, svc, _ = build()
    with pytest.raises(ValueError):
        tools["link_memory"](pid, "123", REASON)
    assert total_calls(svc) == 0


def test_link_memory_bad_reason():
    tools, svc, _ = build()
    with pytest.raises(ValueError):
        tools["link_memory"]("GKLV-DY8", "123", " ")
    assert total_calls(svc) == 0


# ---- create_source ----------------------------------------------------------------

def test_create_source_ok():
    tools, svc, _ = build()
    out = tools["create_source"](" https://www.archives.gov/x?y=1 ", " Title ", " Cite ", REASON)
    assert out == {"status": "created", "source_id": "71FD-SL2"}
    assert svc.sources.create_description.calls == [
        (("https://www.archives.gov/x?y=1", "Title", "Cite"), {"reason": REASON})]


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "file:///etc/passwd", "ftp://x", "//host/x", "https://",
    "https://a b", "x" * 2001, "https://" + "a" * 2000, "data:text/html,hi", "example.org/x",
    "", "  ", None, 5, "https://a\x00b.org", "http:///x", "https://a.org/\nx",
    "https://user:pw@example.org/x", "https://evil@example.org", "https://a.org\\@b.org/",
    "https://example.org/a\\b", "https://example.org:99999/x", "https://example.org:abc/x",
])
def test_create_source_bad_url(url):
    tools, svc, _ = build()
    with pytest.raises(ValueError):
        tools["create_source"](url, "T", "C", REASON)
    assert total_calls(svc) == 0


@pytest.mark.parametrize("title,citation", [("", "C"), ("  ", "C"), ("T", ""), ("T", " "),
                                            (None, "C"), ("T", None), ("T\x00", "C"),
                                            ("T", "C" * 2001)])
def test_create_source_bad_text(title, citation):
    tools, svc, _ = build()
    with pytest.raises(ValueError):
        tools["create_source"]("https://example.org/x", title, citation, REASON)
    assert total_calls(svc) == 0


def test_create_source_http_allowed():
    tools, svc, _ = build()
    assert tools["create_source"]("HTTP://example.org/x", "T", "C", REASON)["status"] == "created"


# ---- attach_source ----------------------------------------------------------------

def test_attach_source_builds_clean_url():
    tools, svc, ctx = build()
    out = tools["attach_source"](" gklv-dy8 ", " 71fd-sl2 ", REASON)
    assert out == {"status": "attached", "person_id": "GKLV-DY8", "source_id": "71FD-SL2"}
    (args, kw), = svc.sources.attach_to_person.calls
    assert args == ("GKLV-DY8", "https://api.example.org/platform/sources/descriptions/71FD-SL2")
    assert "?" not in args[1] and args[1].endswith("/71FD-SL2")
    assert kw == {"reason": REASON}
    assert svc.discovery.asked == ["source-descriptions"]
    assert ctx.call_count == 1


@pytest.mark.parametrize("sid", ["abc", "71FD", "71FD-SL2/../x", "https://x/y", None, 5,
                                 "71FD-SL2?x=1", "7ıFD-SL2", ""])
def test_attach_source_bad_source_id(sid):
    tools, svc, _ = build()
    with pytest.raises(ValueError, match="source description id"):
        tools["attach_source"]("GKLV-DY8", sid, REASON)
    assert total_calls(svc) == 0 and svc.discovery.asked == []


def test_attach_source_bad_person_id():
    tools, svc, _ = build()
    with pytest.raises(ValueError):
        tools["attach_source"]("nope", "71FD-SL2", REASON)
    assert total_calls(svc) == 0


def test_attach_source_strips_query_and_fragment():
    tools, svc, _ = build()
    svc.discovery = FakeDiscovery("https://api.example.org/platform/descriptions?flag=fsh#frag")
    tools["attach_source"]("GKLV-DY8", "71FD-SL2", REASON)
    assert svc.sources.attach_to_person.calls[0][0][1] == \
        "https://api.example.org/platform/descriptions/71FD-SL2"
