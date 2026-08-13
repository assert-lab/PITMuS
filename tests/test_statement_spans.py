"""find_statement_span() and multi-line mutation application.

PIT reports a single lineNumber, but the statement it belongs to may wrap across
several lines. Getting the span wrong corrupts the reconstructed method body, so
these bounds are load-bearing.
"""

import pytest

from conftest import tok_all
from shared import apply_mutation_multiline, apply_mutation_with_fallback, find_statement_span


def test_single_line_statement_spans_one_line():
    lines = ["void m() {", "    int x = 1;", "}"]
    assert find_statement_span(lines, 2) == (2, 2)


def test_wrapped_call_span_covers_all_continuation_lines():
    lines = ["void m() {", "    int x = foo(a,", "        b,", "        c);", "    return;", "}"]
    assert find_statement_span(lines, 2) == (2, 4)


def test_span_is_identical_from_any_line_inside_it():
    lines = ["void m() {", "    int x = foo(a,", "        b,", "        c);", "    return;", "}"]
    assert find_statement_span(lines, 3) == find_statement_span(lines, 2)
    assert find_statement_span(lines, 4) == find_statement_span(lines, 2)


def test_statement_after_a_wrapped_one_is_not_absorbed():
    lines = ["void m() {", "    int x = foo(a,", "        c);", "    return;", "}"]
    assert find_statement_span(lines, 4) == (4, 4)


def test_out_of_range_line_is_clamped_not_crashed():
    lines = ["void m() {", "    int x = 1;", "}"]
    assert find_statement_span(lines, 99) == (99, 99)
    assert find_statement_span(lines, 0) == (0, 0)


def test_semicolon_inside_string_literal_does_not_end_statement():
    lines = ["void m() {", '    String s = foo("a;b",', "        c);", "    return;", "}"]
    assert find_statement_span(lines, 2) == (2, 3)


# ------------------------------------------------------- multi-line application


def test_multiline_condition_mutates_first_operator():
    lines = ["void m() {", "    if (a == b", "            && c == d) {", "        return;", "    }", "}"]
    start, end, text = apply_mutation_with_fallback(lines, tok_all("\n".join(lines)), 2, "negated conditional", 0)
    assert (start, end) == (2, 3)
    assert text == "    if (a != b\n            && c == d) {"


def test_multiline_condition_mutates_second_operator_on_later_line():
    lines = ["void m() {", "    if (a == b", "            && c == d) {", "        return;", "    }", "}"]
    start, end, text = apply_mutation_with_fallback(lines, tok_all("\n".join(lines)), 2, "negated conditional", 1)
    assert (start, end) == (2, 3)
    assert text == "    if (a == b\n            && c != d) {"


def test_multiline_result_line_count_matches_span():
    """The replacement must have exactly as many lines as the span it replaces,
    or the surrounding method body gets misaligned."""
    lines = ["void m() {", "    if (a == b", "            && c == d) {", "        return;", "    }", "}"]
    start, end, text = apply_mutation_with_fallback(lines, tok_all("\n".join(lines)), 2, "negated conditional", 0)
    assert len(text.split("\n")) == end - start + 1


def test_multiline_returns_none_when_nothing_changes():
    lines = ["void m() {", "    int x = 1;", "}"]
    src = "\n".join(lines)
    assert apply_mutation_multiline(lines, tok_all(src), "negated conditional", 0, 2, 2) is None


def test_fallback_returns_three_tuple_for_out_of_range_line():
    lines = ["void m() {", "    int x = 1;", "}"]
    result = apply_mutation_with_fallback(lines, tok_all("\n".join(lines)), 99, "negated conditional", 0)
    assert isinstance(result, tuple) and len(result) == 3


def test_fallback_finds_operator_on_adjacent_line():
    """PIT's lineNumber can be off by a line for wrapped expressions; the engine
    scans neighbours rather than giving up."""
    lines = ["void m() {", "    int x = 1;", "    if (a == b) {", "        return;", "    }", "}"]
    start, end, text = apply_mutation_with_fallback(lines, tok_all("\n".join(lines)), 2, "negated conditional", 0)
    assert "a != b" in text
    assert start == end == 3


# ------------------------------------------------------- block comments in spans
#
# A `*/` at the end of the previous line used to end the backward walk
# unconditionally, on the assumption it closed a Javadoc block above a
# declaration. But an *inline trailing* comment on a continuation line
# (`..., null /* note */`) looks identical, so the walk stopped in the middle of
# an argument list. The span then started mid-expression, its parentheses never
# balanced, and the forward scan ran on through unrelated methods -- one BCEL row
# captured 7,499 characters and stopped inside a `for` loop three methods later.


def test_trailing_inline_comment_is_not_a_statement_boundary():
    """The real MethodGen shape: an argument list whose lines carry inline comments."""
    lines = [
        "class A {",
        "    A(Method method) {",
        "        this(method.getAccessFlags(), getReturnType(method),",
        "            getArgumentTypes(method), null /* may be overridden anyway */",
        "            , method.getName(), className,",
        "            ((method.getAccessFlags() & MASK) == 0)",
        "                ? new InstructionList(getByteCodes(method))",
        "                : null,",
        "            cp);",
        "    }",
        "}",
    ]
    assert find_statement_span(lines, 6) == (3, 9)


def test_span_with_inline_comments_is_balanced_and_self_contained():
    """Regression guard for the runaway: the span must not leak past its own `;`."""
    lines = [
        "class A {",
        "    A() {",
        "        this(a, null /* note */",
        "            , b);",
        "    }",
        "    void other() {",
        "        int x = 1;",
        "    }",
        "}",
    ]
    start, end = find_statement_span(lines, 4)
    text = "\n".join(lines[start - 1:end])
    assert (start, end) == (3, 4)
    assert text.count("(") == text.count(")")
    assert "other" not in text


def test_every_continuation_line_may_end_in_a_comment():
    """The real Code.java shape -- a wrapped `return` where each line is annotated."""
    lines = [
        "class A {",
        "    private int getInternalLength() {",
        "        return 2 /*maxStack*/+ 2 /*maxLocals*/+ 4 /*code length*/",
        "                + code.length /*byte-code*/",
        "                + 2 /*exception-table length*/",
        "                + 8 * (table == null ? 0 : table.length) /* exception table */",
        "                + 2 /* attributes count */;",
        "    }",
        "}",
    ]
    assert find_statement_span(lines, 6) == (3, 7)


def test_span_extends_past_a_comment_opened_on_the_statement_line():
    """The real InstructionList shape: the statement ends at `;` but the line then
    opens a block comment that runs on below. Cutting at the `;` emits Java with an
    unterminated `/*`, which does not compile."""
    lines = [
        "class A {",
        "    void m() {",
        "        int target = bi.getPosition() + bi.getIndex(); /*",
        "                     * Byte code position: relative -> absolute.",
        "                     */",
        "        int next = 0;",
        "    }",
        "}",
    ]
    start, end = find_statement_span(lines, 3)
    assert (start, end) == (3, 5)
    text = "\n".join(lines[start - 1:end])
    assert text.count("/*") == text.count("*/")


def test_javadoc_above_a_declaration_is_still_a_boundary():
    """The `*/` rule exists for this case; it must keep working."""
    lines = [
        "class A {",
        "    /**",
        "     * Adds two numbers.",
        "     */",
        "    int add(int a, int b) { return a + b; }",
        "}",
    ]
    assert find_statement_span(lines, 5) == (5, 5)


def test_a_comment_only_line_is_still_a_boundary():
    lines = [
        "class A {",
        "    void m() {",
        "        /* set it up */",
        "        int x = 1;",
        "    }",
        "}",
    ]
    assert find_statement_span(lines, 4) == (4, 4)


def test_a_comment_closing_from_an_earlier_line_is_still_a_boundary():
    """`*/` with no `/*` on the same line closes a multi-line block -- a real boundary."""
    lines = [
        "class A {",
        "    void m() {",
        "        /* a note",
        "           spanning lines */",
        "        int x = 1;",
        "    }",
        "}",
    ]
    assert find_statement_span(lines, 5) == (5, 5)


def test_comment_inside_a_string_literal_does_not_extend_the_span():
    """`\"/*\"` is text, not a comment; extending on it would swallow the next line."""
    lines = [
        "class A {",
        "    void m() {",
        '        String s = "/*";',
        "        int x = 1;",
        "    }",
        "}",
    ]
    assert find_statement_span(lines, 3) == (3, 3)
