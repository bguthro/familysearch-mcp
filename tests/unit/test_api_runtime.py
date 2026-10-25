import pytest
import responses

from familysearch.api._runtime import ApiBase, ApiResult
from familysearch.api.registry import OperationLockedError
from familysearch.api.spec import OpSpec
from familysearch.auth import Environment
from familysearch.client import FSClient
from familysearch.errors import FSError

BETA = Environment.BETA.base_url
FP = "fp"


def _spec(kind="read", method="GET", path="/tree/persons/{pid}", path_params=("pid",),
          required_query=(), accepts=(), body="none", destructive=False):
    return OpSpec(
        operation_id="Op", method_name="op", area="a", http_method=method, path=path,
        summary="s", kind=kind, destructive=destructive, path_params=path_params,
        required_query=required_query, accepts=accepts, body=body, fingerprint=FP,
    )


def _api(registry=None, env=Environment.BETA):
    client = FSClient("tkn", env, min_write_interval=0)
    return ApiBase(client, registry=registry if registry is not None
                   else {"schema": 1, "operations": {}})


def _verified():
    return {"schema": 1, "operations": {"Op": {"generated": {
        "status": "verified", "fingerprint": FP, "reason": "ok"}}}}


@responses.activate
@pytest.mark.parametrize("pid, quoted", [
    ("../x", "..%2Fx"), ("a/b", "a%2Fb"), ("a?b#c", "a%3Fb%23c"), ("é", "%C3%A9"),
    ("a{pid}", "a%7Bpid%7D"),
])
def test_path_injection_is_quoted(pid, quoted):
    url = BETA + "/platform/tree/persons/" + quoted
    responses.add(responses.GET, url, json={})
    _api()._call(_spec(), {"pid": pid})
    assert len(responses.calls) == 1
    assert responses.calls[0].request.url == url


@responses.activate
@pytest.mark.parametrize("pid", ["", "   ", "\t", ".", "..", None, " . "])
def test_empty_or_blank_id_rejected(pid):
    with pytest.raises(ValueError):
        _api()._call(_spec(), {"pid": pid})
    assert len(responses.calls) == 0


def test_missing_path_arg_rejected():
    with pytest.raises(ValueError):
        _api()._call(_spec(), {})


@responses.activate
def test_accept_default_and_validation():
    responses.add(responses.GET, BETA + "/platform/tree/persons/1", json={})
    spec = _spec(accepts=("application/x-fs-v1+json", "application/json"))
    api = _api()
    api._call(spec, {"pid": 1})
    assert responses.calls[0].request.headers["Accept"] == "application/x-fs-v1+json"
    api._call(spec, {"pid": 1}, accept="application/json")
    assert responses.calls[1].request.headers["Accept"] == "application/json"
    with pytest.raises(ValueError):
        api._call(spec, {"pid": 1}, accept="text/xml")
    assert len(responses.calls) == 2
    api._call(_spec(), {"pid": 1}, accept="text/xml")  # no declared accepts: free


@responses.activate
def test_required_query_enforced():
    responses.add(responses.GET, BETA + "/platform/tree/persons/1", json={})
    spec = _spec(required_query=("q",))
    with pytest.raises(ValueError):
        _api()._call(spec, {"pid": 1})
    with pytest.raises(ValueError):
        _api()._call(spec, {"pid": 1}, query={"x": 1})
    _api()._call(spec, {"pid": 1}, query={"q": "a"})
    assert len(responses.calls) == 1


@responses.activate
def test_query_none_dropped_and_bool_rendered():
    responses.add(responses.GET, BETA + "/platform/tree/persons/1", json={})
    _api()._call(_spec(), {"pid": 1}, query={"a": None, "b": True, "c": False, "d": 3})
    url = responses.calls[0].request.url
    assert "a=" not in url
    assert "b=true" in url and "c=false" in url and "d=3" in url


@responses.activate
def test_204_gives_none_body_and_json_list_body_parsed():
    responses.add(responses.GET, BETA + "/platform/tree/persons/1", status=204)
    r = _api()._call(_spec(), {"pid": 1})
    assert r.status == 204 and r.body is None and r.text == ""
    responses.replace(responses.GET, BETA + "/platform/tree/persons/1",
                      body="[1, 2]", content_type="application/x-fs-v1+json")
    r = _api()._call(_spec(), {"pid": 1})
    assert r.body == [1, 2]
    assert r.content_type == "application/x-fs-v1+json"


@responses.activate
def test_non_json_body_is_none_text_kept():
    responses.add(responses.GET, BETA + "/platform/tree/persons/1",
                  body="<x/>", content_type="text/xml")
    r = _api()._call(_spec(), {"pid": 1})
    assert r.body is None and r.text == "<x/>"
    responses.replace(responses.GET, BETA + "/platform/tree/persons/1",
                      body="not json", content_type="application/json")
    r = _api()._call(_spec(), {"pid": 1})
    assert r.body is None and r.text == "not json"


def test_entity_id_from_header_then_location():
    both = {"X-ENTITY-ID": "P1", "Location": "/a/b/P2"}
    assert ApiResult(201, both, "", None, "").entity_id == "P1"
    assert ApiResult(201, {"x-entity-id": "P1"}, "", None, "").entity_id == "P1"
    assert ApiResult(201, {"Location": "https://h/a/b/P2"}, "", None, "").entity_id == "P2"
    assert ApiResult(201, {"Location": "/a/b/P2/"}, "", None, "").entity_id == "P2"
    assert ApiResult(201, {}, "", None, "").entity_id is None


@responses.activate
def test_write_requires_reason_via_client():
    responses.add(responses.POST, BETA + "/platform/tree/persons/1", status=201)
    spec = _spec(kind="write", method="POST", body="json")
    api = _api(_verified())
    with pytest.raises(FSError):
        api._call(spec, {"pid": 1}, body={"a": 1})
    assert len(responses.calls) == 0
    api._call(spec, {"pid": 1}, body={"a": 1}, reason="why")
    assert responses.calls[0].request.headers["X-Reason"] == "why"


@responses.activate(assert_all_requests_are_fired=False)
def test_locked_write_makes_no_network_call():
    spec = _spec(kind="write", method="POST", body="json")
    with pytest.raises(OperationLockedError):
        _api()._call(spec, {"pid": 1}, body={}, reason="why")
    with pytest.raises(OperationLockedError):
        _api(_verified(), env=Environment.PRODUCTION)._call(
            spec, {"pid": 1}, body={}, reason="why")
    assert len(responses.calls) == 0


@responses.activate
def test_read_kind_post_has_no_reason_header():
    responses.add(responses.POST, BETA + "/platform/tree/search", json={})
    spec = _spec(kind="read", method="POST", path="/tree/search", path_params=(), body="json")
    _api()._call(spec, {}, body={"q": 1})
    assert "X-Reason" not in responses.calls[0].request.headers


@responses.activate
def test_upload_only_for_upload_ops():
    responses.add(responses.POST, BETA + "/platform/tree/persons/1", status=201)
    json_spec = _spec(kind="write", method="POST", body="json")
    api = _api(_verified())
    with pytest.raises(ValueError):
        api._call(json_spec, {"pid": 1}, files={"f": b"x"}, reason="r")
    with pytest.raises(ValueError):
        api._call(json_spec, {"pid": 1}, data=b"x", data_content_type="a/b", reason="r")
    assert len(responses.calls) == 0
    up = _spec(kind="write", method="POST", body="upload")
    api._call(up, {"pid": 1}, data=b"x", data_content_type="image/png", reason="r")
    assert responses.calls[0].request.headers["Content-Type"] == "image/png"
