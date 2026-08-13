"""Token/string primitives: replace_at, nth_token, nth_token_in, tokens_on_line, nth_gt_run.

These are small but every mutator is built on them, so a regression here is
silently wide-reaching.
"""

import pytest

from conftest import tok, tok_all
from shared import nth_gt_run, nth_token, nth_token_in, replace_at, tokens_on_line


# ---------------------------------------------------------------- replace_at


def test_replace_at_substitutes_in_place():
    assert replace_at("a + b", 2, 1, "-") == "a - b"


def test_replace_at_supports_different_lengths():
    assert replace_at("a > b", 2, 1, ">=") == "a >= b"
    assert replace_at("a >= b", 2, 2, ">") == "a > b"


def test_replace_at_at_string_start_and_end():
    assert replace_at("abc", 0, 1, "X") == "Xbc"
    assert replace_at("abc", 2, 1, "X") == "abX"


def test_replace_at_with_empty_replacement_deletes():
    assert replace_at("a - b", 2, 2, "") == "a b"


def test_replace_at_leaves_rest_of_line_untouched():
    line = "    int x = a + b;  // trailing"
    out = replace_at(line, line.index("+"), 1, "-")
    assert out.endswith("// trailing")
    assert out.count("-") == 1


# ------------------------------------------------------------------ nth_token


def test_nth_token_finds_each_occurrence():
    toks = tok("a + b + c")
    assert nth_token(toks, "+", 0).position[1] < nth_token(toks, "+", 1).position[1]


def test_nth_token_out_of_range_returns_none():
    assert nth_token(tok("a + b"), "+", 5) is None


def test_nth_token_absent_value_returns_none():
    assert nth_token(tok("a + b"), "*", 0) is None


def test_nth_token_in_matches_any_of_a_set():
    toks = tok("a < b")
    assert nth_token_in(toks, {"<", ">", "<=", ">="}, 0).value == "<"


def test_nth_token_in_respects_occurrence_order():
    toks = tok("a < b && c > d")
    assert nth_token_in(toks, {"<", ">"}, 0).value == "<"
    assert nth_token_in(toks, {"<", ">"}, 1).value == ">"


def test_nth_token_in_out_of_range_returns_none():
    assert nth_token_in(tok("a < b"), {"<"}, 3) is None


# -------------------------------------------------------------- tokens_on_line


def test_tokens_on_line_filters_by_line_number():
    src = "int a = 1;\nint b = 2;\nint c = 3;"
    all_toks = tok_all(src)
    line2 = tokens_on_line(all_toks, 2)
    assert [t.value for t in line2] == ["int", "b", "=", "2", ";"]


def test_tokens_on_line_empty_for_blank_line():
    all_toks = tok_all("int a = 1;\n\nint c = 3;")
    assert tokens_on_line(all_toks, 2) == []


def test_tokens_on_line_out_of_range_is_empty():
    assert tokens_on_line(tok_all("int a = 1;"), 99) == []


# ----------------------------------------------------------------- nth_gt_run

# javalang splits `>>` into two `>` tokens (generics ambiguity), so shift mutators
# must re-assemble runs of adjacent `>` -- and must not confuse `>>` with `>>>`
# or with the `>` `>` of `List<Map<K,V>>`.


def test_nth_gt_run_finds_shift_right():
    line = "x = y >> 2;"
    t = nth_gt_run(tok(line), 2, 0)
    assert t is not None and line[t.position[1] - 1 : t.position[1] + 1] == ">>"


def test_nth_gt_run_finds_unsigned_shift_right():
    line = "x = y >>> 2;"
    t = nth_gt_run(tok(line), 3, 0)
    assert t is not None and line[t.position[1] - 1 : t.position[1] + 2] == ">>>"


def test_nth_gt_run_needs_enough_tokens():
    """A two-token run cannot satisfy a request for three."""
    assert nth_gt_run(tok("x = y >> 2;"), 3, 0) is None


def test_nth_gt_run_matches_a_prefix_of_a_longer_run():
    """Characterization, not endorsement: the scan matches `count` *adjacent* `>`,
    so asking for 2 inside `>>>` succeeds on the first two.

    Harmless in the real pipeline because PIT distinguishes the operators itself
    (`>>` -> ishr, `>>>` -> iushr) and emits the matching description, so a `>>`
    description is never applied to a `>>>`-only line. It would matter on a line
    containing both operators; no such case exists in the current corpora.
    """
    assert nth_gt_run(tok("x = y >>> 2;"), 2, 0) is not None


def test_nth_gt_run_ignores_single_greater_than():
    assert nth_gt_run(tok("if (a > b) {"), 2, 0) is None


def test_nth_gt_run_out_of_range_returns_none():
    assert nth_gt_run(tok("x = y >> 2;"), 2, 5) is None


def test_nth_gt_run_requires_adjacency():
    """Separated `>` tokens (`a > b > c`) are not a shift operator."""
    assert nth_gt_run(tok("a > b > c"), 2, 0) is None
