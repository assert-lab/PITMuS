"""extract_test_files(): map PIT killing/covering test identifiers to source files.

PIT emits two identifier encodings and both must resolve:
  * JUnit Platform unique IDs (PIT 1.22 + JUnit 5), e.g.
    [engine:junit-jupiter]/[class:org.example.FooTest]/[method:bar()]
  * legacy JUnit 4 style, e.g.  testBar(org.example.FooTest)

Regression guard: the [class:...] form was previously unrecognized and silently
returned "", which is indistinguishable from "no test information".
"""

import pytest

from shared import extract_test_files


# ------------------------------------------------- JUnit Platform ([class:...])


def test_junit_platform_full_unique_id():
    ident = "[engine:junit-jupiter]/[class:org.example.ExampleTest]/[method:foo()]"
    assert extract_test_files(ident) == "ExampleTest.java"


def test_junit_platform_bare_class_segment():
    assert extract_test_files("[class:org.apache.commons.lang3.ArrayUtilsTest]") == "ArrayUtilsTest.java"


def test_nested_class_maps_to_enclosing_source_file():
    """A nested class lives in its top-level class's .java file."""
    assert extract_test_files("[class:org.example.Foo$InnerTest]/[method:t()]") == "Foo.java"


def test_parameterized_display_name_does_not_leak_into_result():
    ident = (
        "[engine:junit-jupiter]/[class:org.example.FooTest]"
        "/[test-template:bar(int)]/[test-template-invocation:#1 arg=5]"
    )
    assert extract_test_files(ident) == "FooTest.java"


def test_nested_class_segment_is_not_mistaken_for_class_segment():
    """`[nested-class:...]` must not be parsed as the `[class:...]` field."""
    ident = "[engine:junit-jupiter]/[class:org.example.OuterTest]/[nested-class:Inner]/[method:t()]"
    assert extract_test_files(ident) == "OuterTest.java"


def test_structured_segment_wins_over_legacy_parentheses():
    """When both encodings appear, the structured class field is authoritative."""
    ident = "[class:org.example.RealTest]/[method:check(some.other.Thing)]"
    assert extract_test_files(ident) == "RealTest.java"


# --------------------------------------------------------------- legacy (JUnit 4)


def test_legacy_parenthesised_class():
    ident = "testAbbreviate(org.apache.commons.lang3.StringUtilsTest)"
    assert extract_test_files(ident) == "StringUtilsTest.java"


def test_legacy_unqualified_class():
    assert extract_test_files("testFoo(FooTest)") == "FooTest.java"


# ------------------------------------------------------------ multiple / dedupe


def test_multiple_entries_are_sorted_and_pipe_joined():
    ident = "[class:org.a.OneTest]/[method:x()]|beta(org.b.TwoTest)"
    assert extract_test_files(ident) == "OneTest.java|TwoTest.java"


def test_duplicate_test_classes_are_deduplicated():
    ident = "[class:org.a.OneTest]/[method:x()]|[class:org.a.OneTest]/[method:y()]"
    assert extract_test_files(ident) == "OneTest.java"


def test_many_entries_all_resolve():
    ident = "|".join(f"[class:org.p.T{i}Test]/[method:m()]" for i in range(5))
    assert extract_test_files(ident) == "|".join(f"T{i}Test.java" for i in range(5))


# ------------------------------------------------------------------ unresolved


@pytest.mark.parametrize(
    "ident",
    [
        "",
        None,
        "[class:not a class!]",
        "[class:]",
        "no identifiable structure at all",
    ],
)
def test_unresolvable_identifiers_return_empty(ident):
    """Malformed input must yield an explicit empty result, never a bogus path."""
    assert extract_test_files(ident) == ""


def test_platform_id_without_a_class_segment_never_falls_back_to_parentheses():
    """The exact regression that motivated the fix.

    On a JUnit Platform identifier the parentheses hold the method's *parameter
    types*. Falling back to the legacy "(...)" parser here recorded production
    classes as test files -- Document$OutputSettings, Locale, String -- which is
    worse than reporting nothing, because it looks like genuine provenance.
    """
    ident = "[engine:junit-jupiter]/[method:normalize(org.jsoup.nodes.Document$OutputSettings)]"
    assert extract_test_files(ident) == ""


def test_platform_id_without_a_class_segment_does_not_poison_valid_siblings():
    ident = (
        "[engine:junit-jupiter]/[method:normalize(org.jsoup.nodes.Document$OutputSettings)]"
        "|[engine:junit-jupiter]/[class:org.jsoup.NodeTest]/[method:m()]"
    )
    assert extract_test_files(ident) == "NodeTest.java"


def test_legacy_form_still_uses_the_parenthesised_fallback():
    """The guard must key on Platform *structure*, not on the presence of parens."""
    assert extract_test_files("testFoo(org.example.LegacyTest)") == "LegacyTest.java"


def test_malformed_entry_does_not_discard_valid_siblings():
    ident = "[class:bad name here]|[class:org.good.GoodTest]"
    assert extract_test_files(ident) == "GoodTest.java"


def test_result_never_contains_path_separators_or_package():
    ident = "[engine:junit-jupiter]/[class:deep.nested.pkg.DeepTest]/[method:m()]"
    out = extract_test_files(ident)
    assert out == "DeepTest.java"
    assert "/" not in out and "." not in out[:-5]
