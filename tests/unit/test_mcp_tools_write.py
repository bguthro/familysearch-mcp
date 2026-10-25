import copy
import json
from pathlib import Path

import pytest

from familysearch_mcp import tools_write
from familysearch_mcp.context import ContextError
from familysearch import FSError, TokenExpiredError
from familysearch_mcp.tools_write import PartialWriteError, register_write_tools

FIX = Path(__file__).parent / "fixtures"
HIT = json.loads((FIX / "search_entry.json").read_text())

REASON = "Adding from research archive folder smith-john"


class FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self, *a, **k):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class Rec:
    def __init__(self, result=None, exc=None):
        self.calls = []
        self.result = result
        self.exc = exc

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.exc:
            raise self.exc
        return self.result


class Obj:
    pass


def hit(conf, pid="KW1"):
    h = copy.deepcopy(HIT)
    h["confidence"] = conf
    h["content"]["gedcomx"]["persons"][0]["id"] = pid
    return h


class ParentRec(Rec):
    """Returns one relationship id per parent, REL-<parent>; can fail on a parent."""

    def __init__(self):
        super().__init__()
        self.fail_on = {}

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        pid = kwargs["parent_ids"][0]
        if pid in self.fail_on:
            raise self.fail_on[pid]
        return [f"REL-{pid}"]


def make_services(hits=()):
    s = Obj()
    s.search = Obj()
    s.search.tree_search = Rec(list(hits))
    s.writer = Obj()
    s.writer.create_person = Rec("NEW1-ABC")
    s.writer.add_fact = Rec(None)
    s.writer.create_couple = Rec("REL1-XYZ")
    s.writer.create_parent_child = ParentRec()
    return s


def writer_calls(s):
    return sum(len(getattr(s.writer, n).calls) for n in
               ("create_person", "add_fact", "create_couple", "create_parent_child"))


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


@pytest.fixture(autouse=True)
def year(monkeypatch):
    monkeypatch.setattr(tools_write, "_current_year", lambda: 2026)


def build(hits=(), writes=True):
    svc = make_services(hits)
    ctx = FakeCtx(svc, writes=writes)
    mcp = FakeMCP()
    register_write_tools(mcp, ctx)
    return mcp.tools, svc, ctx


@pytest.fixture
def setup():
    return build()


PERSON = dict(given="John", surname="Smith", gender="Male", reason=REASON)
OLD = dict(PERSON, birth_date="1850")

CALLS = {
    "create_person": dict(PERSON, birth_date="1850"),
    "add_fact": dict(person_id="GKLV-DY8", fact_type="Birth", reason=REASON, date="1850"),
    "create_couple": dict(person1_id="GKLV-DY8", person2_id="GKLW-DY9", reason=REASON),
    "create_parent_child": dict(child_id="GKLV-DY8", parent_ids=["GKLW-DY9"], reason=REASON),
}


def test_registers_first_four_tools_with_docs(setup):
    # The full set of eight is asserted in test_mcp_tools_write_memories.py.
    tools, _, _ = setup
    assert set(CALLS) <= set(tools)
    for name, fn in tools.items():
        assert fn.__doc__ and fn.__doc__.strip(), name
        assert "reason" in fn.__doc__, name
        assert not any(w in name for w in ("delete", "merge", "remove")), name
    assert "Ben" in tools["create_person"].__doc__
    assert "private" in tools["create_person"].__doc__.lower()


@pytest.mark.parametrize("name", list(CALLS))
def test_write_blocked_when_writes_disabled(name):
    tools, svc, ctx = build(writes=False)
    out = tools[name](**CALLS[name])
    assert out["status"] == "writes_disabled" and out["message"]
    assert writer_calls(svc) == 0
    assert svc.search.tree_search.calls == []
    assert ctx.call_count == 0


@pytest.mark.parametrize("name", list(CALLS))
def test_write_gate_precedes_validation(name):
    tools, svc, ctx = build(writes=False)
    args = dict(CALLS[name], reason="")
    assert tools[name](**args)["status"] == "writes_disabled"
    assert ctx.call_count == 0 and writer_calls(svc) == 0


@pytest.mark.parametrize("name", list(CALLS))
@pytest.mark.parametrize("reason", ["", "   \n", "x" * 501, None, 5, ["r"]])
def test_bad_reason(name, reason):
    tools, svc, ctx = build()
    with pytest.raises(ValueError):
        tools[name](**dict(CALLS[name], reason=reason))
    assert writer_calls(svc) == 0
    assert ctx.call_count == 0


def test_reason_max_length_ok(setup):
    tools, svc, _ = setup
    out = tools["create_couple"](**dict(CALLS["create_couple"], reason="x" * 500))
    assert out["status"] == "created"


def test_non_context_error_from_gate_is_not_swallowed():
    tools, svc, ctx = build()

    def boom():
        raise RuntimeError("env mismatch")

    ctx.require_writes = boom
    with pytest.raises(RuntimeError):
        tools["create_couple"](**CALLS["create_couple"])


# ---- create_person -------------------------------------------------------

def test_create_person_returns_matches_and_does_not_create():
    hits = [hit(2, "A1"), hit(5, "B2"), hit(None, "C3"), hit(4, "D4")]
    tools, svc, _ = build(hits)
    out = tools["create_person"](**OLD)
    assert out["status"] == "matches_found"
    assert [c["id"] for c in out["candidates"]] == ["B2", "D4", "A1", "C3"]
    assert "Ben" in out["message"] and "create_despite_matches" in out["message"]
    assert writer_calls(svc) == 0
    assert svc.search.tree_search.calls[0][1]["count"] == 10


def test_matches_capped_at_ten():
    tools, svc, _ = build([hit(i, f"P{i}") for i in range(15)])
    out = tools["create_person"](**OLD)
    assert len(out["candidates"]) == 10
    assert out["candidates"][0]["confidence"] == 14


def test_create_person_creates_with_override():
    tools, svc, _ = build([hit(5)])
    out = tools["create_person"](**OLD, create_despite_matches=True)
    assert out == {"status": "created", "id": "NEW1-ABC", "living": False,
                   "life_status": "deceased_by_age"}
    assert svc.search.tree_search.calls == []
    assert len(svc.writer.create_person.calls) == 1


def test_no_hits_creates():
    tools, svc, _ = build([])
    out = tools["create_person"](**OLD)
    assert out["status"] == "created"
    kw = svc.search.tree_search.calls[0][1]
    assert kw["given"] == "John" and kw["surname"] == "Smith" and kw["birth_date"] == "1850"


def test_search_failure_propagates_and_nothing_created():
    tools, svc, _ = build()
    svc.search.tree_search = Rec(exc=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        tools["create_person"](**OLD)
    assert writer_calls(svc) == 0


def test_writer_error_propagates():
    tools, svc, _ = build()
    svc.writer.create_person = Rec(exc=RuntimeError("w"))
    with pytest.raises(RuntimeError):
        tools["create_person"](**OLD)


@pytest.mark.parametrize("living", [None, False])
def test_recent_birth_needs_confirmation(living):
    tools, svc, _ = build()
    out = tools["create_person"](**PERSON, birth_date="1990", living=living)
    assert out["status"] == "needs_life_confirmation"
    assert out["classification"] == "possibly_living"
    assert "confirm_deceased" in out["message"] and "living=True" in out["message"]
    assert writer_calls(svc) == 0


def test_living_true_creates_private_living():
    tools, svc, _ = build()
    out = tools["create_person"](**PERSON, birth_date="1990", living=True)
    assert out == {"status": "created", "id": "NEW1-ABC", "living": True,
                   "life_status": "living"}
    payload = svc.writer.create_person.calls[0][0][0]
    assert payload["living"] is True


def test_confirm_deceased_creates_not_living():
    tools, svc, _ = build()
    out = tools["create_person"](**PERSON, birth_date="1990", confirm_deceased=True)
    assert out["status"] == "created" and out["living"] is False
    assert out["life_status"] == "possibly_living"
    assert svc.writer.create_person.calls[0][0][0]["living"] is False


def test_old_birth_creates_deceased():
    tools, svc, _ = build()
    out = tools["create_person"](**OLD)
    assert out["living"] is False and out["life_status"] == "deceased_by_age"


def test_death_date_creates_deceased():
    tools, svc, _ = build()
    out = tools["create_person"](**PERSON, birth_date="1990", death_date="2010")
    assert out["status"] == "created" and out["life_status"] == "deceased"
    assert svc.writer.create_person.calls[0][0][0]["living"] is False


def test_living_with_death_date_needs_confirmation():
    tools, svc, _ = build()
    out = tools["create_person"](**PERSON, birth_date="1990", death_date="2010", living=True)
    assert out["status"] == "needs_life_confirmation"
    assert writer_calls(svc) == 0


def test_no_dates_needs_confirmation():
    tools, svc, _ = build()
    out = tools["create_person"](**PERSON)
    assert out["status"] == "needs_life_confirmation"
    assert writer_calls(svc) == 0


def test_living_and_confirm_deceased_contradict():
    tools, svc, ctx = build()
    with pytest.raises(ValueError):
        tools["create_person"](**PERSON, living=True, confirm_deceased=True)
    assert writer_calls(svc) == 0 and ctx.call_count == 0


def test_payload_facts_and_reason():
    tools, svc, _ = build()
    tools["create_person"](**PERSON, birth_date="1850", birth_place="Ohio",
                           death_date="1900")
    args, kw = svc.writer.create_person.calls[0]
    assert kw == {"reason": REASON}
    person = args[0]
    types = [f["type"] for f in person["facts"]]
    assert types == ["http://gedcomx.org/Birth", "http://gedcomx.org/Death"]
    assert person["facts"][0]["date"] == {"original": "1850"}
    assert person["facts"][0]["place"] == {"original": "Ohio"}
    assert person["facts"][1]["date"] == {"original": "1900"}
    blob = json.dumps(person["names"])
    assert "John" in blob and "Smith" in blob


def test_payload_no_facts_when_none_given():
    tools, svc, _ = build()
    tools["create_person"](**PERSON, living=True)
    person = svc.writer.create_person.calls[0][0][0]
    assert not person.get("facts")


def test_birth_place_only_makes_birth_fact():
    tools, svc, _ = build()
    tools["create_person"](**PERSON, birth_place="Ohio", living=True)
    person = svc.writer.create_person.calls[0][0][0]
    assert [f["type"] for f in person["facts"]] == ["http://gedcomx.org/Birth"]


@pytest.mark.parametrize("g,expected", [("male", "Male"), ("FEMALE", "Female"), ("unknown", "Unknown")])
def test_gender_normalized(g, expected):
    tools, svc, _ = build()
    tools["create_person"](**dict(OLD, gender=g))
    assert expected in json.dumps(svc.writer.create_person.calls[0][0][0])


@pytest.mark.parametrize("g", ["", "other", "M", None, 1])
def test_bad_gender(g):
    tools, svc, ctx = build()
    with pytest.raises(ValueError):
        tools["create_person"](**dict(OLD, gender=g))
    assert writer_calls(svc) == 0 and ctx.call_count == 0


@pytest.mark.parametrize("field", ["birth_date", "birth_place", "death_date"])
def test_overlong_or_nonstr_free_text(field):
    tools, svc, ctx = build()
    for bad in ("x" * 2001, 1850):
        with pytest.raises(ValueError):
            tools["create_person"](**dict(OLD, **{field: bad}))
    assert writer_calls(svc) == 0 and ctx.call_count == 0


# ---- add_fact ------------------------------------------------------------

@pytest.mark.parametrize("ft", ["Residence", "residence", "RESIDENCE",
                                "http://gedcomx.org/Residence"])
def test_add_fact_accepts_whitelist(ft):
    tools, svc, _ = build()
    out = tools["add_fact"]("GKLV-DY8", ft, REASON, place="Ohio")
    assert out == {"status": "added", "person_id": "GKLV-DY8", "fact_type": "Residence"}
    args, kw = svc.writer.add_fact.calls[0]
    assert args[0] == "GKLV-DY8"
    assert args[1] == {"type": "http://gedcomx.org/Residence", "place": {"original": "Ohio"}}
    assert kw == {"reason": REASON}


def test_add_fact_normalizes_id():
    tools, svc, _ = build()
    out = tools["add_fact"](" gklv-dy8 ", "Birth", REASON, date="1850")
    assert out["person_id"] == "GKLV-DY8"


@pytest.mark.parametrize("ft", ["Wizard", "", "http://example.com/Birth",
                                "http://gedcomx.org/Wizard", "https://gedcomx.org/Birth",
                                None, 3, "Birth/../x"])
def test_add_fact_rejects_unknown_type(ft):
    tools, svc, ctx = build()
    with pytest.raises(ValueError) as ei:
        tools["add_fact"]("GKLV-DY8", ft, REASON, date="1850")
    if ft == "Wizard":
        assert "Residence" in str(ei.value)
    assert writer_calls(svc) == 0 and ctx.call_count == 0


def test_add_fact_requires_date_or_place():
    tools, svc, ctx = build()
    with pytest.raises(ValueError):
        tools["add_fact"]("GKLV-DY8", "Birth", REASON)
    assert writer_calls(svc) == 0


def test_add_fact_overlong_text():
    tools, svc, _ = build()
    with pytest.raises(ValueError):
        tools["add_fact"]("GKLV-DY8", "Birth", REASON, date="x" * 2001)
    with pytest.raises(ValueError):
        tools["add_fact"]("GKLV-DY8", "Birth", REASON, place=5)
    assert writer_calls(svc) == 0


# ---- ids -----------------------------------------------------------------

BAD_IDS = ["GKLı-DY8", "../x", None, "", "GKLV-DY8/x", 5]


@pytest.mark.parametrize("bad", BAD_IDS)
def test_bad_ids_everywhere(bad):
    tools, svc, ctx = build()
    with pytest.raises(ValueError):
        tools["add_fact"](bad, "Birth", REASON, date="1850")
    with pytest.raises(ValueError):
        tools["create_couple"](bad, "GKLW-DY9", REASON)
    with pytest.raises(ValueError):
        tools["create_couple"]("GKLW-DY9", bad, REASON)
    with pytest.raises(ValueError):
        tools["create_parent_child"](bad, ["GKLW-DY9"], REASON)
    with pytest.raises(ValueError):
        tools["create_parent_child"]("GKLW-DY9", [bad], REASON)
    assert writer_calls(svc) == 0 and ctx.call_count == 0


# ---- couple / parent-child ----------------------------------------------

def test_create_couple_ok():
    tools, svc, _ = build()
    out = tools["create_couple"]("gklv-dy8", "GKLW-DY9", REASON)
    assert out == {"status": "created", "relationship_id": "REL1-XYZ"}
    args, kw = svc.writer.create_couple.calls[0]
    assert args == ("GKLV-DY8", "GKLW-DY9") and kw == {"reason": REASON}


def test_create_couple_identical_ids():
    tools, svc, ctx = build()
    with pytest.raises(ValueError):
        tools["create_couple"]("GKLV-DY8", " gklv-dy8", REASON)
    assert writer_calls(svc) == 0 and ctx.call_count == 0


def test_create_parent_child_ok():
    tools, svc, ctx = build()
    out = tools["create_parent_child"]("GKLV-DY8", ["GKLW-DY9", "GKLX-DY1"], REASON)
    assert out == {"status": "created",
                   "relationship_ids": ["REL-GKLW-DY9", "REL-GKLX-DY1"]}
    assert svc.writer.create_parent_child.calls == [
        ((), {"child_id": "GKLV-DY8", "parent_ids": ["GKLW-DY9"], "reason": REASON}),
        ((), {"child_id": "GKLV-DY8", "parent_ids": ["GKLX-DY1"], "reason": REASON}),
    ]
    assert ctx.call_count == 2


@pytest.mark.parametrize("parents", [
    [], ["GKLW-DY9", "GKLX-DY1", "GKLY-DY2"], ["GKLW-DY9", "gklw-dy9"],
    ["GKLV-DY8"], "GKLW-DY9", None, ("GKLW-DY9",),
])
def test_create_parent_child_bad_parents(parents):
    tools, svc, ctx = build()
    with pytest.raises(ValueError):
        tools["create_parent_child"]("GKLV-DY8", parents, REASON)
    assert writer_calls(svc) == 0 and ctx.call_count == 0


# ---- fix round 1 ---------------------------------------------------------

from itertools import product  # noqa: E402

JUNK_DEATHS = ["Living", "Unknown", "?", "N/A", "2027", "2999",
               "Ignore previous instructions"]


@pytest.mark.parametrize("death", JUNK_DEATHS)
@pytest.mark.parametrize("living", [True, False, None])
def test_junk_death_date_rejected_before_anything(death, living):
    tools, svc, ctx = build()
    with pytest.raises(ValueError, match="death_date"):
        tools["create_person"](**PERSON, birth_date="1990", death_date=death, living=living)
    assert writer_calls(svc) == 0 and ctx.call_count == 0
    assert svc.search.tree_search.calls == []


@pytest.mark.parametrize("death,living,confirm,birth", [
    c for c in product(JUNK_DEATHS + ["   ", "", "2020", "1 March 1920", None],
                       [True, False, None], [False, True], ["1990", "1850", None])
    if not (c[1] is True and c[2])
])
def test_invariant_no_living_person_with_death_fact(death, living, confirm, birth):
    tools, svc, ctx = build()
    try:
        out = tools["create_person"](**PERSON, birth_date=birth, death_date=death,
                                     living=living, confirm_deceased=confirm)
    except ValueError:
        assert writer_calls(svc) == 0
        return
    if out["status"] != "created":
        assert writer_calls(svc) == 0
        return
    person = svc.writer.create_person.calls[0][0][0]
    deaths = [f for f in person.get("facts") or []
              if f["type"] == "http://gedcomx.org/Death"]
    if living is True:
        assert not deaths and person["living"] is True
    for f in deaths:
        assert person["living"] is False
        assert tools_write.classify_life_status(
            None, f["date"]["original"], today_year=2026) == "deceased"


def test_living_with_valid_death_pauses():
    tools, svc, _ = build()
    out = tools["create_person"](**PERSON, birth_date="1990", death_date="2020", living=True)
    assert out["status"] == "needs_life_confirmation"
    assert writer_calls(svc) == 0


def test_valid_death_old_birth_creates_death_fact():
    tools, svc, _ = build()
    out = tools["create_person"](**PERSON, birth_date="1850", death_date="1 March 1920")
    assert out["status"] == "created"
    person = svc.writer.create_person.calls[0][0][0]
    assert person["facts"][1]["date"] == {"original": "1 March 1920"}


@pytest.mark.parametrize("flag", [{"confirm_deceased": True}, {"living": True}])
def test_flags_do_not_skip_match_gate(flag):
    tools, svc, _ = build([hit(3)])
    out = tools["create_person"](**PERSON, birth_date="1990", **flag)
    assert out["status"] == "matches_found"
    assert writer_calls(svc) == 0


@pytest.mark.parametrize("resp", [None, {}, "", 0, "abc", {"entries": []}])
def test_malformed_search_response_fails_closed(resp):
    tools, svc, _ = build()
    svc.search.tree_search = Rec(resp)
    with pytest.raises(FSError):
        tools["create_person"](**OLD)
    assert writer_calls(svc) == 0


def test_add_fact_death_date_must_be_valid():
    tools, svc, ctx = build()
    with pytest.raises(ValueError):
        tools["add_fact"]("GKLV-DY8", "Death", REASON, date="Unknown")
    with pytest.raises(ValueError):
        tools["add_fact"]("GKLV-DY8", "http://gedcomx.org/Death", REASON, date="2999")
    assert writer_calls(svc) == 0 and ctx.call_count == 0
    assert tools["add_fact"]("GKLV-DY8", "Death", REASON, date="1 March 1920")["status"] == "added"
    assert tools["add_fact"]("GKLV-DY8", "death", REASON, place="Lynn")["status"] == "added"
    assert tools["add_fact"]("GKLV-DY8", "Residence", REASON, date="about the war")["status"] == "added"


def test_add_fact_blank_handling():
    tools, svc, _ = build()
    tools["add_fact"]("GKLV-DY8", "Residence", REASON, date="  ", place=" Lynn ")
    assert svc.writer.add_fact.calls[0][0][1] == {
        "type": "http://gedcomx.org/Residence", "place": {"original": "Lynn"}}
    for d, p in [("", ""), ("  ", None), (None, "\n ")]:
        with pytest.raises(ValueError):
            tools["add_fact"]("GKLV-DY8", "Residence", REASON, date=d, place=p)
    assert len(svc.writer.add_fact.calls) == 1


def test_names_stripped():
    tools, svc, _ = build()
    tools["create_person"](**dict(OLD, given="  John ", surname=" Doe "))
    person = svc.writer.create_person.calls[0][0][0]
    form = person["names"][0]["nameForms"][0]
    assert form["fullText"] == "John Doe"
    assert [p["value"] for p in form["parts"]] == ["John", "Doe"]
    assert svc.search.tree_search.calls[0][1]["given"] == "John"


def test_blank_free_text_creates_no_junk_facts():
    tools, svc, _ = build()
    tools["create_person"](**PERSON, birth_date=" ", birth_place=" ", death_date="  ",
                           living=True)
    assert not svc.writer.create_person.calls[0][0][0].get("facts")


@pytest.mark.parametrize("bad", ["a\x00b", "a\nb", "a\tb", "\x00"])
@pytest.mark.parametrize("field", ["given", "surname", "birth_date", "birth_place",
                                   "death_date", "reason"])
def test_control_chars_rejected_create_person(field, bad):
    tools, svc, ctx = build()
    with pytest.raises(ValueError):
        tools["create_person"](**dict(OLD, **{field: bad}))
    assert writer_calls(svc) == 0 and ctx.call_count == 0


@pytest.mark.parametrize("bad", ["a\x00b", "a\nb", "a\tb"])
@pytest.mark.parametrize("field", ["date", "place", "reason"])
def test_control_chars_rejected_add_fact(field, bad):
    tools, svc, ctx = build()
    kw = dict(person_id="GKLV-DY8", fact_type="Residence", reason=REASON, place="Lynn")
    kw[field] = bad
    with pytest.raises(ValueError):
        tools["add_fact"](**kw)
    assert writer_calls(svc) == 0 and ctx.call_count == 0


def test_zero_width_chars_not_rejected():
    tools, svc, _ = build()
    out = tools["add_fact"]("GKLV-DY8", "Residence", REASON, place="Ly‍nn")
    assert out["status"] == "added"


def test_reason_length_measured_after_strip():
    tools, svc, _ = build()
    assert tools["create_couple"]("GKLV-DY8", "GKLW-DY9", " " + "x" * 500)["status"] == "created"
    with pytest.raises(ValueError):
        tools["create_couple"]("GKLV-DY8", "GKLW-DY9", " " + "x" * 501)


def test_parent_child_first_parent_failure_propagates_unchanged():
    tools, svc, _ = build()
    err = RuntimeError("nope")
    svc.writer.create_parent_child.fail_on["GKLW-DY9"] = err
    with pytest.raises(RuntimeError) as ei:
        tools["create_parent_child"]("GKLV-DY8", ["GKLW-DY9", "GKLX-DY1"], REASON)
    assert ei.value is err
    assert len(svc.writer.create_parent_child.calls) == 1


def test_parent_child_second_parent_failure_is_partial():
    tools, svc, _ = build()
    err = RuntimeError("no such person")
    svc.writer.create_parent_child.fail_on["GKLX-DY1"] = err
    with pytest.raises(PartialWriteError) as ei:
        tools["create_parent_child"]("GKLV-DY8", ["GKLW-DY9", "GKLX-DY1"], REASON)
    e = ei.value
    assert isinstance(e, FSError)
    assert e.created_ids == ["REL-GKLW-DY9"]
    assert "GKLX-DY1" in str(e) and "REL-GKLW-DY9" in str(e)
    assert e.__cause__ is err


def test_parent_child_token_expiry_reruns_only_one_post():
    tools, svc, ctx = build()

    def retrying_call(fn):
        ctx.call_count += 1
        try:
            return fn(svc)
        except TokenExpiredError:
            return fn(svc)

    ctx.call = retrying_call
    svc.writer.create_parent_child.fail_on["GKLX-DY1"] = TokenExpiredError("expired")
    with pytest.raises(PartialWriteError):
        tools["create_parent_child"]("GKLV-DY8", ["GKLW-DY9", "GKLX-DY1"], REASON)
    posted = [c[1]["parent_ids"][0] for c in svc.writer.create_parent_child.calls]
    assert posted == ["GKLW-DY9", "GKLX-DY1", "GKLX-DY1"]


# ---- Step 0 fixes -------------------------------------------------------

def test_add_fact_death_error_does_not_mention_confirm_deceased():
    tools, svc, _ = build()
    with pytest.raises(ValueError) as ei:
        tools["add_fact"]("GKLV-DY8", "Death", REASON, date="Unknown")
    assert "confirm_deceased" not in str(ei.value)
    assert "place" in str(ei.value)
    assert svc.writer.add_fact.calls == []
    assert "Death" in tools["add_fact"].__doc__ and "past date" in tools["add_fact"].__doc__


def test_create_person_death_error_keeps_confirm_hint():
    tools, _, _ = build()
    with pytest.raises(ValueError, match="confirm_deceased"):
        tools["create_person"](**dict(PERSON, death_date="Unknown"))


@pytest.mark.parametrize("sep", [" ", " "])
def test_line_paragraph_separators_rejected(sep):
    tools, svc, _ = build()
    with pytest.raises(ValueError):
        tools["create_person"](**dict(PERSON, given="Jo" + sep + "hn"))
    with pytest.raises(ValueError):
        tools["create_couple"]("GKLV-DY8", "GKLW-DY9", "why" + sep + "x")
    assert writer_calls(svc) == 0 and svc.search.tree_search.calls == []


class OddParentRec(ParentRec):
    def __init__(self, bad_for, bad):
        super().__init__()
        self.bad_for, self.bad = bad_for, bad

    def __call__(self, *args, **kwargs):
        out = super().__call__(*args, **kwargs)
        return self.bad if kwargs["parent_ids"][0] == self.bad_for else out


@pytest.mark.parametrize("bad", ["REL-1", None, [], [""], [None], ("REL-1",), {"a": 1}])
def test_parent_child_bad_return_second_parent_is_partial(bad):
    tools, svc, _ = build()
    svc.writer.create_parent_child = OddParentRec("GKLX-DY1", bad)
    with pytest.raises(PartialWriteError) as ei:
        tools["create_parent_child"]("GKLV-DY8", ["GKLW-DY9", "GKLX-DY1"], REASON)
    assert ei.value.created_ids == ["REL-GKLW-DY9"]
    assert isinstance(ei.value.__cause__, TypeError)


@pytest.mark.parametrize("bad", ["REL-1", None, [], [""], [None]])
def test_parent_child_bad_return_first_parent_is_fserror(bad):
    tools, svc, _ = build()
    svc.writer.create_parent_child = OddParentRec("GKLW-DY9", bad)
    with pytest.raises(FSError, match="unexpected response") as ei:
        tools["create_parent_child"]("GKLV-DY8", ["GKLW-DY9", "GKLX-DY1"], REASON)
    assert not isinstance(ei.value, PartialWriteError)
    assert len(svc.writer.create_parent_child.calls) == 1
