"""extract_method_spans(), find_span_for_line(), extract_javadoc(), int_locals_in_span().

The method span decides what ends up in the dataset's `original_method` /
`mutated_method` columns, so a wrong span silently corrupts every downstream row.
"""

import pytest

from shared import (
    extract_javadoc,
    extract_method_spans,
    find_span_for_line,
    int_locals_in_span,
    load_source,
)

SOURCE = """package p;

class C {
    /**
     * Adds two numbers.
     */
    int add(int a, int b) {
        return a + b;
    }

    C() {
        this.x = 0;
    }

    Runnable r = () -> System.out.println(1);

    void outer() {
        Runnable anon = new Runnable() {
            public void run() {
                int q = 1;
            }
        };
    }
}
"""
LINES = SOURCE.splitlines()


@pytest.fixture(scope="module")
def spans():
    return extract_method_spans(SOURCE, LINES)


def test_spans_are_found(spans):
    assert spans, "no method spans extracted"


def test_spans_are_sorted_by_start_line(spans):
    starts = [s[0] for s in spans]
    assert starts == sorted(starts)


def test_spans_are_wellformed_1based_inclusive(spans):
    for start, end, name in spans:
        assert 1 <= start <= end <= len(LINES)
        assert isinstance(name, str) and name


def test_named_method_span_covers_its_body(spans):
    add = next(s for s in spans if s[2] == "add")
    start, end, _ = add
    body = "\n".join(LINES[start - 1 : end])
    assert "int add(int a, int b)" in body
    assert "return a + b;" in body


def test_constructor_is_captured(spans):
    assert any(s[2] in ("C", "<init>") for s in spans)


def test_lambda_is_captured(spans):
    assert any(s[2] == "<lambda>" for s in spans)


def test_find_span_for_line_returns_enclosing_method(spans):
    body_line = LINES.index("        return a + b;") + 1
    span = find_span_for_line(spans, body_line)
    assert span is not None and span[2] == "add"


def test_find_span_for_line_returns_none_outside_any_method(spans):
    assert find_span_for_line(spans, 1) is None  # the `package p;` line


def test_find_span_for_line_picks_innermost_span(spans):
    """A line inside an anonymous class must resolve to the inner span, not `outer`."""
    inner_line = LINES.index("                int q = 1;") + 1
    span = find_span_for_line(spans, inner_line)
    assert span is not None
    start, end, _ = span
    assert end - start < 8, "picked an outer span instead of the innermost one"


def test_malformed_source_yields_no_spans():
    bad = "class { this is not java ((("
    assert extract_method_spans(bad, bad.splitlines()) == []


# ----------------------------------------------------------------------- javadoc


def test_javadoc_is_extracted_for_documented_method(spans):
    add_start = next(s[0] for s in spans if s[2] == "add")
    doc = extract_javadoc(LINES, add_start)
    assert "Adds two numbers." in doc
    assert doc.strip().startswith("/**") and doc.strip().endswith("*/")


def test_javadoc_is_empty_for_undocumented_method(spans):
    outer_start = next(s[0] for s in spans if s[2] == "outer")
    assert extract_javadoc(LINES, outer_start).strip() == ""


def test_javadoc_does_not_run_off_the_top_of_the_file():
    assert extract_javadoc(["int f() {", "}"], 1) == ""


# ------------------------------------------------------- int locals (iinc rule)


def test_plain_int_locals_are_detected():
    lines = ["void m() {", "    int i = 0;", "    long j = 0;", "    i++;", "}"]
    assert int_locals_in_span(lines, (1, 5, "m")) == frozenset({"i"})


def test_non_int_locals_are_excluded():
    lines = ["void m() {", "    long j = 0;", "    short s = 0;", "    byte b = 0;", "}"]
    assert int_locals_in_span(lines, (1, 5, "m")) == frozenset()


def test_int_locals_handles_none_span():
    assert int_locals_in_span(["int i = 0;"], None) == frozenset()


def test_int_locals_returns_frozenset():
    lines = ["void m() {", "    int i = 0;", "}"]
    assert isinstance(int_locals_in_span(lines, (1, 3, "m")), frozenset)


# ------------------------------------------------------------------ load_source


def test_load_source_roundtrip(java_file):
    path = java_file(SOURCE)
    lines, tokens, spans_ = load_source(path)
    assert lines == LINES
    assert tokens, "expected tokens from a valid Java file"
    assert any(s[2] == "add" for s in spans_)


def test_load_source_missing_file_returns_empty_triple():
    lines, tokens, spans_ = load_source("/no/such/file/Nope.java")
    assert (lines, tokens, spans_) == ([], [], [])


def test_load_source_unparseable_file_still_returns_lines(java_file):
    """A file javalang cannot parse must not lose its source lines."""
    path = java_file("class { ((( not java")
    lines, _tokens, spans_ = load_source(path)
    assert lines == ["class { ((( not java"]
    assert spans_ == []


def test_load_source_token_positions_align_with_lines(java_file):
    """Token (line, col) must index back into `lines` correctly -- everything in the
    engine relies on this."""
    path = java_file(SOURCE)
    lines, tokens, _ = load_source(path)
    for t in tokens[:80]:
        ln, col = t.position
        assert lines[ln - 1][col - 1 : col - 1 + len(t.value)] == t.value
