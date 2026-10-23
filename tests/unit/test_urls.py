from familysearch._urls import join_path


def test_preserves_query_and_appends_path():
    assert (join_path("https://h/platform/tree/persons?flag=fsh", "P1")
            == "https://h/platform/tree/persons/P1?flag=fsh")


def test_multiple_segments():
    assert (join_path("https://h/platform/tree/persons?flag=fsh", "MID", "personas")
            == "https://h/platform/tree/persons/MID/personas?flag=fsh")


def test_strips_uri_template_suffix():
    assert join_path("https://h/p/persons{?q}", "P1") == "https://h/p/persons/P1"
    assert join_path("https://h/p/persons?flag=fsh{&x}", "P1") == "https://h/p/persons/P1?flag=fsh"


def test_trailing_slash_and_quoting():
    assert join_path("https://h/p/", "a/b c") == "https://h/p/a%2Fb%20c"


def test_no_query():
    assert join_path("https://h/p", "P1") == "https://h/p/P1"
