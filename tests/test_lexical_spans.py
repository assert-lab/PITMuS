"""Method spans recovered without an AST.

javalang is a Java-8-era parser. One switch expression, text block or record in a
file makes the *whole file* fail to parse, and `extract_method_spans` used to return
`[]` for it -- so every mutation in that file fell through to the `<field>` fallback
and `original_method` became a single statement instead of the enclosing method.

These tests pin the recovery path: a brace-balancing lexical scan that finds method
and constructor declarations, refuses to guess at control structures, lambdas and
initializers, and reports where each span came from so callers can tell "no methods"
apart from "cannot see the methods".
"""

import sys

import pytest

from conftest import TOOL_DIR

sys.path.insert(0, str(TOOL_DIR))

from shared import extract_method_spans, find_span_for_line, load_source  # noqa: E402

# Parses under javalang only from Java 14 on: a switch *expression* with `->` arms
# and a text block. Everything else here is ordinary Java 8.
MODERN_JAVA = '''package com.example;

public class Modern {

    /** Adds two numbers. */
    public int add(int a, int b) {
        if (a > b) {
            return a + b;
        }
        return b + a;
    }

    public int add(int a, int b, int c) {
        return a + b + c;
    }

    public String describe(int day) {
        return switch (day) {
            case 1, 7 -> "weekend";
            default -> """
                a { brace } and a "quote" inside a text block
                """;
        };
    }

    public Modern(int seed) {
        this.seed = seed;
    }

    private final int seed;
    private static final int[] TABLE = { 1, 2, 3 };

    static {
        System.out.println("static initializer");
    }

    public Runnable make() {
        return new Runnable() {
            @Override
            public void run() {
                System.out.println("anon");
            }
        };
    }

    public void each(java.util.List<String> xs) {
        xs.forEach(x -> {
            System.out.println(x);
        });
    }
}
'''


@pytest.fixture(scope="module")
def modern():
    lines = MODERN_JAVA.splitlines()
    spans, origin = extract_method_spans(MODERN_JAVA, lines, with_origin=True)
    return lines, spans, origin


def line_of(lines, needle):
    """1-based line number of the single line containing `needle`."""
    hits = [i + 1 for i, ln in enumerate(lines) if needle in ln]
    assert len(hits) == 1, f"{needle!r} matched {len(hits)} lines"
    return hits[0]


# ------------------------------------------------- the parser fails, spans survive

def test_the_sample_really_does_defeat_the_parser(modern):
    """If javalang ever learns Java 14 this fixture stops testing the fallback."""
    _lines, _spans, origin = modern
    assert origin == "lexical"


def test_ordinary_methods_are_still_recovered_when_parsing_fails(modern):
    _lines, spans, _origin = modern
    names = {s[2] for s in spans}
    assert {"add", "describe", "make", "each", "Modern"} <= names


def test_constructors_are_recovered_as_spans(modern):
    lines, spans, _origin = modern
    ctor = next(s for s in spans if s[2] == "Modern")
    assert ctor[0] == line_of(lines, "public Modern(int seed)")


def test_control_structures_are_not_mistaken_for_methods(modern):
    _lines, spans, _origin = modern
    assert not {"if", "for", "while", "switch", "catch", "try"} & {s[2] for s in spans}


def test_initializers_and_lambdas_do_not_produce_spans(modern):
    """A `static {` block, an array initializer and an `x -> {` lambda all end in a
    brace but none of them is a declaration."""
    lines, spans, _origin = modern
    static_block = line_of(lines, "    static {")
    lambda_line = line_of(lines, "xs.forEach(x -> {")
    table_line = line_of(lines, "TABLE = { 1, 2, 3 }")
    starts = {s[0] for s in spans}
    assert static_block not in starts
    assert lambda_line not in starts
    assert table_line not in starts


def test_braces_inside_strings_and_comments_do_not_unbalance_the_scan(modern):
    """The text block holds `{ brace }`; if masking failed, `describe` would end early."""
    lines, spans, _origin = modern
    describe = next(s for s in spans if s[2] == "describe")
    assert lines[describe[1] - 1].strip() == "}"
    assert describe[1] > line_of(lines, "a { brace } and a")


def test_span_starts_at_the_declaration_not_the_annotation(modern):
    """`run()` carries an @Override; the span must match what the AST would give."""
    lines, spans, _origin = modern
    run = next(s for s in spans if s[2] == "run")
    assert lines[run[0] - 1].strip().startswith("public void run(")


# -------------------------------------------------------------- awkward headers

def spans_of(source):
    from shared.mutate import _lexical_method_spans

    return _lexical_method_spans(source.splitlines())


def test_a_generic_header_wrapped_over_lines_with_throws_is_one_span():
    """The declaration, its parameters and its `throws` clause span four lines; the
    span must start at the declaration and cover the whole body."""
    source = """class A {
    public <T extends Comparable<T>> java.util.List<T> sortAll(
            java.util.Collection<? extends T> in,
            java.util.Comparator<T> cmp) throws IllegalStateException {
        return null;
    }
}
"""
    assert spans_of(source) == [(2, 6, "sortAll")]


def test_enum_constant_bodies_behave_like_anonymous_classes():
    """An enum constant with a body is a subclass, so it gets a span the way an
    anonymous class does -- and the method inside it is the innermost span."""
    source = """enum E {
    FOO(1) {
        int v() {
            return 1;
        }
    },
    BAR(2);

    E(int x) {
    }

    int get() {
        return 0;
    }
}
"""
    spans = spans_of(source)
    assert spans == [(2, 6, "FOO"), (3, 5, "v"), (9, 10, "E"), (12, 14, "get")]
    assert find_span_for_line(spans, 4) == (3, 5, "v")


def test_interface_default_methods_are_found_but_the_interface_is_not():
    source = """interface I {
    int abstractOne(int x);

    default int d() {
        return 0;
    }
}
"""
    assert spans_of(source) == [(4, 6, "d")]


# ----------------------------------------------------------- overloaded methods

def test_a_mutation_in_an_overload_keeps_exactly_that_overload(modern):
    """Two `add` overloads sit next to each other. Resolving a line inside the first
    must yield the *whole* two-argument method and nothing of the three-argument one --
    a span that merged them would put the wrong body in `original_method`."""
    lines, spans, _origin = modern
    inside = line_of(lines, "return a + b;")
    span = find_span_for_line(spans, inside)
    assert span is not None
    start, end, name = span
    assert name == "add"
    assert start == line_of(lines, "public int add(int a, int b) {")
    body = "\n".join(lines[start - 1:end])
    assert "return a + b + c;" not in body, "span leaked into the other overload"
    assert body.count("public int add(") == 1


def test_the_other_overload_resolves_to_its_own_span(modern):
    lines, spans, _origin = modern
    inside = line_of(lines, "return a + b + c;")
    start, end, name = find_span_for_line(spans, inside)
    assert name == "add"
    assert start == line_of(lines, "public int add(int a, int b, int c) {")
    assert "return a + b;\n" not in "\n".join(lines[start - 1:end])


# --------------------------------------------------------------------- origins

def test_a_parseable_file_still_reports_an_ast_origin():
    source = "class A {\n    int f() {\n        return 1;\n    }\n}\n"
    spans, origin = extract_method_spans(source, source.splitlines(), with_origin=True)
    assert origin == "ast"
    assert [s[2] for s in spans] == ["f"]


def test_unrecoverable_source_reports_the_none_origin():
    """Neither parseable nor lexically recoverable -- callers must not read the empty
    span list as 'this file has no methods'."""
    bad = "class { ((( not java"
    spans, origin = extract_method_spans(bad, bad.splitlines(), with_origin=True)
    assert (spans, origin) == ([], "none")


def test_extract_method_spans_default_signature_is_unchanged():
    """Existing callers pass two arguments and expect a plain list back."""
    source = "class A {\n    int f() { return 1; }\n}\n"
    assert extract_method_spans(source, source.splitlines()) == [(2, 2, "f")]


# ------------------------------------------------------------------ load_source

def test_load_source_threads_the_origin_through(java_file):
    lines, _tokens, spans, origin = load_source(java_file(MODERN_JAVA), with_origin=True)
    assert origin == "lexical"
    assert lines == MODERN_JAVA.splitlines()
    assert any(s[2] == "describe" for s in spans)


def test_load_source_reports_none_for_a_missing_file():
    assert load_source("/no/such/file/Nope.java", with_origin=True) == ([], [], [], "none")


def test_load_source_reports_none_for_unrecoverable_source(java_file):
    _lines, _tokens, spans, origin = load_source(
        java_file("class { ((( not java"), with_origin=True
    )
    assert (spans, origin) == ([], "none")
