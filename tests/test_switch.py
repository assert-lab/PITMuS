"""EXPERIMENTAL_SWITCH support: parse_switch_tables() and reconstruct_switch_default().

`Changed switch default to be first case` is the one mutator that needs bytecode
(the switch table) to reconstruct, because the source-level default branch is not
recoverable from text alone. These tests drive the parser with recorded `javap`
output so the suite never shells out to a JDK.
"""

import pytest

from shared import parse_switch_tables, reconstruct_switch_default

# Recorded `javap -c -p -l` output for:
#     int f(int k) {
#         switch (k) {
#             case 1:  return 10;
#             case 2:  return 20;
#             default: return 0;
#         }
#     }
JAVAP_TABLESWITCH = """  public int f(int);
    descriptor: (I)I
    Code:
       0: iload_1
       1: tableswitch   { // 1 to 2
                     1: 28
                     2: 33
               default: 38
          }
      28: iconst_1
      33: iconst_2
      38: iconst_0
      39: ireturn
    LineNumberTable:
      line 10: 0
      line 12: 28
      line 14: 33
      line 16: 38
"""

JAVAP_LOOKUPSWITCH = """  public int g(int);
    descriptor: (I)I
    Code:
       0: iload_1
       1: lookupswitch  { // 2
                   100: 40
                   200: 50
               default: 60
          }
      40: iconst_1
      50: iconst_2
      60: iconst_0
    LineNumberTable:
      line 20: 0
      line 22: 40
      line 24: 50
      line 26: 60
"""


def test_parses_a_tableswitch():
    regions = parse_switch_tables(JAVAP_TABLESWITCH)
    assert len(regions) == 1
    sw = regions[0]["switches"][0]
    assert sw["kind"] == "tableswitch"
    assert sw["cases"] == [(1, 28), (2, 33)]
    assert sw["default"] == 38


def test_parses_a_lookupswitch_with_sparse_keys():
    regions = parse_switch_tables(JAVAP_LOOKUPSWITCH)
    sw = regions[0]["switches"][0]
    assert sw["kind"] == "lookupswitch"
    assert sw["cases"] == [(100, 40), (200, 50)]
    assert sw["default"] == 60


def test_line_number_table_is_parsed_as_offset_to_line():
    regions = parse_switch_tables(JAVAP_TABLESWITCH)
    assert regions[0]["lnt"] == [(0, 10), (28, 12), (33, 14), (38, 16)]


def test_default_target_differs_from_every_case_target():
    """If default collided with a case target the mutation would be a no-op."""
    sw = parse_switch_tables(JAVAP_TABLESWITCH)[0]["switches"][0]
    assert sw["default"] not in [t for _k, t in sw["cases"]]


def test_multiple_methods_yield_multiple_regions():
    regions = parse_switch_tables(JAVAP_TABLESWITCH + JAVAP_LOOKUPSWITCH)
    assert len(regions) == 2


@pytest.mark.parametrize("text", ["", "no switch here at all", "Code:\n   0: iload_1\n"])
def test_input_without_switches_yields_no_switch_records(text):
    regions = parse_switch_tables(text)
    assert all(not r["switches"] for r in regions)


def test_reconstruct_returns_none_without_bytecode():
    """No switch table -> explicitly unresolvable, never a guessed rewrite."""
    lines = ["int f(int k) {", "    switch (k) {", "        default: return 0;", "    }", "}"]
    assert reconstruct_switch_default(lines, 2, []) is None


def test_reconstruct_returns_none_when_line_has_no_switch():
    lines = ["int f(int k) {", "    return k;", "}"]
    regions = parse_switch_tables(JAVAP_TABLESWITCH)
    assert reconstruct_switch_default(lines, 2, regions) is None


# Labels on their own lines, so a jump target maps to a *different* line depending on
# whether the compiler attributes it to the label or to the first body statement. The
# inline `case 1: return 10;` used above cannot distinguish the two.
#            1: package com.example;
#            2:
#            3: public class Sw {
#            4:     int f(int k) {
#            5:         switch (k) {
#            6:             case 1:
#            7:                 return 10;
#            8:             case 2:
#            9:                 return 20;
#           10:             default:
#           11:                 return 0;
#           12:         }
#           13:     }
#           14: }
SPLIT_LABEL_SRC = """package com.example;

public class Sw {
    int f(int k) {
        switch (k) {
            case 1:
                return 10;
            case 2:
                return 20;
            default:
                return 0;
        }
    }
}""".split("\n")


def _javap_split_label(case1, case2, default):
    """Recorded tableswitch whose LineNumberTable maps targets to the given lines."""
    return f"""  int f(int);
    descriptor: (I)I
    Code:
       0: iload_1
       1: tableswitch   {{ // 1 to 2
                     1: 28
                     2: 33
               default: 38
          }}
      28: iconst_1
      33: iconst_2
      38: iconst_0
      39: ireturn
    LineNumberTable:
      line 5: 0
      line {case1}: 28
      line {case2}: 33
      line {default}: 38
"""


# javac versions disagree about which line a switch jump target belongs to. Keying the
# reconstruction on the body line alone made every switch-default row in a project
# silently degrade to a comment annotation the moment it was rebuilt with a different
# JDK -- the rows stayed in the dataset but were no longer mutants.
@pytest.mark.parametrize("attribution,case1,case2,default", [
    ("body", 7, 9, 11),
    ("label", 6, 8, 10),
])
def test_reconstruct_is_indifferent_to_label_vs_body_line_attribution(
    attribution, case1, case2, default
):
    regions = parse_switch_tables(_javap_split_label(case1, case2, default))
    result = reconstruct_switch_default(SPLIT_LABEL_SRC, 5, regions)

    assert result is not None, (
        f"{attribution}-line attribution failed to reconstruct; the switch is the same "
        "either way, so only the LineNumberTable differs"
    )
    _start, _end, text = result
    body = [ln.strip() for ln in text.split("\n") if ln.strip()]
    # default body (return 0) is promoted onto every real case; the old first-case
    # body (return 10) becomes the new default.
    assert body == [
        "switch (k) {",
        "case 1:",
        "case 2:",
        "return 0;",
        "default:",
        "return 10;",
        "}",
    ]


def test_reconstruct_returns_span_and_text_or_none():
    """Contract check: either None, or a (start, end, text) triple with sane bounds."""
    lines = [
        "int f(int k) {",
        "    switch (k) {",
        "        case 1: return 10;",
        "        case 2: return 20;",
        "        default: return 0;",
        "    }",
        "}",
    ]
    result = reconstruct_switch_default(lines, 2, parse_switch_tables(JAVAP_TABLESWITCH))
    if result is not None:
        start, end, text = result
        assert 1 <= start <= end <= len(lines)
        assert isinstance(text, str)
