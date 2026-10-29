"""Offline tests for the private-tree cleanup helper, and the live-run fixes it shares."""
import re

import pytest
import responses

from familysearch.auth import Environment
from familysearch.client import FSClient
from tools.verify import cleanup_zzspike_genealogies as cz
from tools.verify import teardown
from tools.verify.fixtures import FixtureSet, local_part, same_id

from _fake_genealogies import FakeGenealogies


def client():
    return FSClient("tkn", Environment.BETA, min_write_interval=0)


def fixtures():
    return FixtureSet(client(), reason="ZZSPIKE test")


def leftovers(fake):
    """A ZZSPIKE tree and source left behind, and a foreign source that must stay."""
    fx = fixtures()
    tid = fx.genealogies_tree("ZZSPIKEleft")
    sdid = fx.genealogies_source(tid, "ZZSPIKEsrc")
    fake.sources["2:2:2:FOREIGN-1"] = {"tid": tid, "about": "https://x", "title": "Keep me",
                                       "citation": "c", "deleted": False}
    return tid, sdid


@responses.activate
def test_dry_run_only_reads():
    fake = FakeGenealogies().serve()
    tid, sdid = leftovers(fake)
    before = len(fake.calls)
    lines = []
    code = cz.main(["--tree", tid, "--source", sdid, "--source", "2:2:2:FOREIGN-1"],
                   client=client(), out=lines.append)
    assert code == 0
    assert all(m == "GET" for m, _ in fake.calls[before:])
    assert any(line.startswith("would delete tree") for line in lines)
    assert any("SKIP source 2:2:2:FOREIGN-1" in line for line in lines)
    assert not fake.sources[sdid]["deleted"] and not fake.trees[tid]["deleted"]


@responses.activate
def test_apply_deletes_only_zzspike_objects_sources_before_trees():
    fake = FakeGenealogies().serve()
    tid, sdid = leftovers(fake)
    lines = []
    code = cz.main(["--tree", tid, "--source", sdid, "--source", "2:2:2:FOREIGN-1", "--apply"],
                   client=client(), out=lines.append)
    assert code == 0, lines
    assert fake.sources[sdid]["deleted"] and fake.trees[tid]["deleted"]
    assert not fake.sources["2:2:2:FOREIGN-1"]["deleted"]
    deletes = [p.split("/genealogies/")[1].split("/")[0] for m, p in fake.calls
               if m == "DELETE"]
    assert deletes == ["sources", "trees"]


@responses.activate
def test_an_unreadable_id_is_skipped_not_written():
    fake = FakeGenealogies().serve()
    lines = []
    assert cz.main(["--source", "2:2:2:GZSD-999", "--apply"], client=client(),
                   out=lines.append) == 0
    assert any("not readable" in line for line in lines)
    assert not [m for m, _ in fake.calls if m == "DELETE"]


def test_usage_errors_exit_2(capsys):
    assert cz.main([]) == 2
    assert cz.main(["--source-range", "9-1"]) == 2
    assert cz.expand_range("286671-286673") == ["2:2:286671", "2:2:286672", "2:2:286673"]


@responses.activate
def test_a_delete_that_never_takes_is_reported():
    fake = FakeGenealogies("source_delete_noop").serve()
    tid, sdid = leftovers(fake)
    lines = []
    assert cz.main(["--source", sdid, "--apply"], client=client(), out=lines.append) == 1
    assert any("still readable" in line for line in lines)


# ---------------------------------------------------------------- shared fixes


def test_ids_compare_across_prefix_and_local_forms():
    assert local_part("2:2:286671") == local_part("s_286671") == "286671"
    assert same_id("2:2:2:MMMF-S7Q", "MMMF-S7Q") and not same_id("2:2:286671", "s_286672")


@responses.activate
def test_a_private_source_deleted_only_by_its_local_id_is_still_torn_down():
    fake = FakeGenealogies("source_delete_needs_local").serve()
    fx = fixtures()
    tid = fx.genealogies_tree("ZZSPIKEtree")
    sdid = fx.genealogies_source(tid, "ZZSPIKEsrc")
    assert fx.teardown() == []
    assert fake.sources[sdid]["deleted"]
    assert any(p.endswith("/sources/s_" + sdid.rsplit(":", 1)[-1]) for m, p in fake.calls
               if m == "DELETE")


def test_gone_after_polls_a_late_delete(monkeypatch):
    from familysearch import FSHTTPError
    reads = iter([None, None, FSHTTPError(404, "", "x")])
    pauses = []
    monkeypatch.setattr(teardown, "sleep", pauses.append)

    def get():
        v = next(reads)
        if v:
            raise v
    assert teardown.gone_after(get, (5, 2.0)) is True and pauses == [2.0, 2.0]
    assert teardown.gone_after(lambda: None, (3, 1.0)) is False


@responses.activate
def test_a_person_create_without_a_local_id_is_refused_like_beta_and_fixtures_send_one():
    fake = FakeGenealogies().serve()
    fx = fixtures()
    tid = fx.genealogies_tree("ZZSPIKEtree")
    pid = fx.genealogies_person(tid, "Local")
    posted = [b for m, p, b in fake.bodies if m == "POST" and p.endswith("/persons")]
    assert posted[0]["persons"][0]["id"] == "p1" and pid in fake.persons
    with pytest.raises(Exception, match="local id"):
        fx._client.post(fx.genealogies_url("trees", tid, "persons"),
                        json={"persons": [{"living": True}]}, reason="ZZSPIKE")


# ---------------------------------------------------------------- hardening


def test_the_source_title_guard_uses_the_entry_of_the_requested_id():
    body = {"sourceDescriptions": [{"id": "s_1", "titles": [{"value": "ZZSPIKE other"}]},
                                   {"id": "s_286671", "titles": [{"value": "Keep me"}]}]}
    assert cz._title("source", "2:2:286671", body) == "Keep me"
    assert cz._title("source", "2:2:999", body) is None  # no entry of that id: never [0]


@responses.activate
def test_list_tree_persons_deletes_zzspike_persons_before_the_tree():
    fake = FakeGenealogies().serve()
    fx = fixtures()
    tid = fx.genealogies_tree("ZZSPIKEleft")
    pid = fx.genealogies_person(tid, "Leftover")
    lines = []
    before = len(fake.calls)
    assert cz.main(["--tree", tid, "--list-tree-persons"], client=client(),
                   out=lines.append) == 0
    assert all(m == "GET" for m, _ in fake.calls[before:])
    assert any(line.startswith("would delete person") for line in lines)
    assert not fake.persons[pid]["deleted"]
    assert cz.main(["--tree", tid, "--list-tree-persons", "--apply"], client=client(),
                   out=lines.append) == 0, lines
    assert fake.persons[pid]["deleted"] and fake.trees[tid]["deleted"]
    deletes = [p.split("/genealogies/")[1].split("/")[0] for m, p in fake.calls
               if m == "DELETE"]
    assert deletes.index("persons") < deletes.index("trees")


@responses.activate
@pytest.mark.parametrize("status,code", [(404, 0), (410, 0), (400, 1), (403, 1), (500, 1)])
def test_only_404_and_410_count_as_already_clean(status, code):
    responses.add(responses.GET, re.compile(r".*/genealogies/sources/[^/?]+(\?.*)?$"),
                  status=status, body="x")
    lines = []
    assert cz.main(["--source", "2:2:1", "--apply"], client=client(), out=lines.append) == code
    assert any(("already clean" in line) == (code == 0) for line in lines), lines
