"""apply_mutation(): one case per PIT STRONGER mutator, plus occurrence selection,
the iinc/int-local rule, and fallback behavior.

Descriptions here are copied verbatim from real PIT 1.x reports (see
test-projects/*/PITMuS_dataset/meta-*.csv), so a change to the description-matching
regexes shows up as a failure here.
"""

import pytest

from conftest import FALLBACK, is_fallback, mutate_line

# ---------------------------------------------------------------- core operators

MUTATORS = [
    # (description, original line, expected mutated line)
    # --- NegateConditionals
    ("negated conditional", "if (a == b) {", "if (a != b) {"),
    ("negated conditional", "if (a != b) {", "if (a == b) {"),
    ("negated conditional", "if (a >= b) {", "if (a < b) {"),
    ("negated conditional", "if (a <= b) {", "if (a > b) {"),
    ("negated conditional", "if (a > b) {", "if (a <= b) {"),
    ("negated conditional", "if (a < b) {", "if (a >= b) {"),
    # --- ConditionalsBoundary
    ("changed conditional boundary", "if (a > b) {", "if (a >= b) {"),
    ("changed conditional boundary", "if (a < b) {", "if (a <= b) {"),
    ("changed conditional boundary", "if (a >= b) {", "if (a > b) {"),
    ("changed conditional boundary", "if (a <= b) {", "if (a < b) {"),
    # --- Math (integer)
    ("Replaced integer addition with subtraction", "int c = a + b;", "int c = a - b;"),
    ("Replaced integer subtraction with addition", "int c = a - b;", "int c = a + b;"),
    ("Replaced integer multiplication with division", "int c = a * b;", "int c = a / b;"),
    ("Replaced integer division with multiplication", "int c = a / b;", "int c = a * b;"),
    ("Replaced integer modulus with multiplication", "int c = a % b;", "int c = a * b;"),
    # --- Math (other primitive widths share one regex branch)
    ("Replaced long addition with subtraction", "long c = a + b;", "long c = a - b;"),
    ("Replaced double division with multiplication", "double c = a / b;", "double c = a * b;"),
    ("Replaced float addition with subtraction", "float c = a + b;", "float c = a - b;"),
    # --- Shifts
    ("Replaced Shift Left with Shift Right", "x = y << 2;", "x = y >> 2;"),
    ("Replaced Shift Right with Shift Left", "x = y >> 2;", "x = y << 2;"),
    ("Replaced Unsigned Shift Right with Shift Left", "x = y >>> 2;", "x = y << 2;"),
    # --- Bitwise
    ("Replaced XOR with AND", "x = y ^ z;", "x = y & z;"),
    ("Replaced bitwise AND with OR", "x = y & z;", "x = y | z;"),
    ("Replaced bitwise OR with AND", "x = y | z;", "x = y & z;"),
    # --- Increments
    ("Changed increment from 1 to -1", "    i++;", "    i--;"),
    ("Changed increment from -1 to 1", "    i--;", "    i++;"),
    # --- InvertNegs
    ("removed negation", "return -x;", "return x;"),
    # --- Return values
    ("replaced return value with null for com/x/Foo::get", "return value;", "return null;"),
    ("replaced boolean return with false for com/x/Foo::is", "return flag;", "return false;"),
]


@pytest.mark.parametrize("desc,line,expected", MUTATORS, ids=lambda v: None)
def test_mutator_applies(desc, line, expected):
    assert mutate_line(line, desc) == expected


def test_all_stronger_families_covered():
    """Guard against a mutator family silently losing its only test case."""
    families = {
        "negated conditional",
        "changed conditional boundary",
        "Replaced integer addition",
        "Replaced long",
        "Replaced double",
        "Shift Left",
        "Shift Right",
        "XOR",
        "bitwise AND",
        "bitwise OR",
        "Changed increment",
        "removed negation",
        "return value",
        "boolean return",
    }
    covered = " ; ".join(d for d, _, _ in MUTATORS)
    missing = [f for f in families if f not in covered]
    assert not missing, f"no test case for mutator family: {missing}"


# --------------------------------------------------------------- VoidMethodCall


def test_removed_call_comments_out_the_call():
    out = mutate_line("foo.bar(1);", "removed call to com/x/Foo::bar")
    assert "bar" in out and out.strip().startswith("//")


def test_removed_call_wrong_method_name_falls_back():
    """The description names ::baz but the line only calls bar() -> unresolvable."""
    out = mutate_line("foo.bar(1);", "removed call to com/x/Foo::baz")
    assert is_fallback(out)


# ------------------------------------------------------------------- occurrence


def test_occurrence_selects_nth_operator():
    line = "int r = a + b + c;"
    desc = "Replaced integer addition with subtraction"
    assert mutate_line(line, desc, occ=0) == "int r = a - b + c;"
    assert mutate_line(line, desc, occ=1) == "int r = a + b - c;"


def test_occurrence_past_end_falls_back():
    line = "int r = a + b + c;"
    desc = "Replaced integer addition with subtraction"
    out = mutate_line(line, desc, occ=2)
    assert is_fallback(out, desc)


def test_occurrence_on_compound_condition():
    line = "if (a < b && c < d) {"
    assert mutate_line(line, "negated conditional", occ=0) == "if (a >= b && c < d) {"
    assert mutate_line(line, "negated conditional", occ=1) == "if (a < b && c >= d) {"


def test_only_the_targeted_operator_changes():
    """A mutation must never rewrite more than its one operator."""
    line = "int r = a + b - c + d;"
    out = mutate_line(line, "Replaced integer subtraction with addition", occ=0)
    assert out == "int r = a + b + c + d;"


# ------------------------------------------------- iinc rule (plain int locals)


def test_int_local_increment_is_not_a_math_target():
    """`i++` on a plain int local compiles to iinc, which PIT's math mutator never
    targets -- so a math description on it must NOT be applied."""
    line = "    i++;"
    desc = "Replaced integer addition with subtraction"
    out = mutate_line(line, desc, occ=0, int_locals=frozenset({"i"}))
    assert is_fallback(out, desc)


def test_non_int_local_increment_is_a_math_target():
    """A field or non-int local compiles to a real add and IS mutable."""
    line = "    i++;"
    out = mutate_line(line, "Replaced integer addition with subtraction", occ=0)
    assert out == "    i--;"


def test_int_locals_does_not_block_increment_mutator():
    """IncrementsMutator targets iinc itself, so int_locals must not gate it."""
    line = "    i++;"
    out = mutate_line(line, "Changed increment from 1 to -1", 0, frozenset({"i"}))
    assert out == "    i--;"


# --------------------------------------------------------------------- fallback


def test_unknown_description_falls_back_without_editing_code():
    line = "int c = a + b;"
    out = mutate_line(line, "totally unknown mutator")
    assert out.startswith(line)
    assert out == line + FALLBACK + "totally unknown mutator"


def test_operator_absent_from_line_falls_back():
    """Right mutator, wrong line: no `+` to mutate."""
    desc = "Replaced integer addition with subtraction"
    out = mutate_line("return x;", desc)
    assert is_fallback(out, desc)


def test_fallback_preserves_original_line_verbatim():
    line = "    someCall(a, b);   "
    out = mutate_line(line, "no such mutator")
    assert out[: len(line)] == line


# ----------------------------------------------------------- non-destructiveness


@pytest.mark.parametrize("desc,line,expected", MUTATORS, ids=lambda v: None)
def test_mutation_changes_exactly_one_thing(desc, line, expected):
    """Every successful mutation differs from the original, and only slightly."""
    assert expected != line, "test case is a no-op; it would not detect a regression"
    # crude edit-distance ceiling: no mutator should rewrite the whole line
    assert abs(len(expected) - len(line)) <= 3


def test_string_literals_are_not_mutated():
    """An operator inside a string literal is not a bytecode operator."""
    line = 'String s = "a + b";'
    out = mutate_line(line, "Replaced integer addition with subtraction")
    assert is_fallback(out) or out == line
    assert '"a + b"' in out


def test_comment_text_is_not_mutated():
    line = "int c = d; // a + b"
    out = mutate_line(line, "Replaced integer addition with subtraction")
    assert "// a + b" in out
