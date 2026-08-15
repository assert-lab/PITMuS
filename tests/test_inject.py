"""Unit tests for the inject.py CLI.

inject.py is the second front-end over `shared/mutate.py`. It adds three things the
dataset front-end does not have, and those are what is tested here:

  * bytecode-based occurrence resolution (`_parse_javap` / `resolve_occ_from_bytecode`)
  * CLI target selection (`matches`, `parse_line_target`, `file_target_to_rel`)
  * .java emission (`inject_at`, `write_mutant`, `main`)

No subprocess is spawned: the javap tests run against recorded output.
"""

import sys

import pytest

from conftest import SIMPLE_JAVA, TOOL_DIR, simple_mutations

sys.path.insert(0, str(TOOL_DIR))

import inject  # noqa: E402


# --------------------------------------------------------------- family_for_desc

@pytest.mark.parametrize(
    "desc, expected_member",
    [
        ("Replaced integer addition with subtraction", "iadd"),
        ("Replaced long multiplication with division", "lmul"),
        ("Replaced double division with multiplication", "ddiv"),
        ("Replaced Shift Left with Shift Right", "ishl"),
        ("Replaced Shift Right with Shift Left", "ishr"),
        ("Replaced Unsigned Shift Right with Shift Left", "iushr"),
        ("Replaced XOR with AND", "ixor"),
        ("Replaced bitwise AND with OR", "iand"),
        ("Replaced bitwise OR with AND", "ior"),
        ("removed negation", "ineg"),
        ("Changed increment from 1 to -1", "iinc"),
        ("Changed switch default to be the first case", "tableswitch"),
    ],
)
def test_family_for_desc_maps_description_to_opcodes(desc, expected_member):
    family = inject.family_for_desc(desc)
    assert family is not None, f"no opcode family for {desc!r}"
    assert expected_member in family


@pytest.mark.parametrize(
    "desc",
    [
        "changed conditional boundary",
        "negated conditional",
        "removed conditional - replaced equality check with false",
        "removed call to java/lang/String::trim",
        "replaced int return with 0 for com/example/Foo::add",
    ],
)
def test_family_for_desc_covers_non_math_mutators(desc):
    assert inject.family_for_desc(desc)


def test_family_for_desc_returns_none_for_unknown():
    assert inject.family_for_desc("some mutator we have never seen") is None


# ------------------------------------------------------------------- javap parse
# Recorded `javap -c -p -l` output. Keeping it inline means the test needs no JDK
# and cannot drift with the local javac version.

JAVAP_ADD = """\
Compiled from "Foo.java"
public class com.example.Foo {
  public com.example.Foo();
    descriptor: ()V
    Code:
       0: aload_0
       1: invokespecial #1
       4: return
    LineNumberTable:
      line 3: 0

  public int add(int, int);
    descriptor: (II)I
    Code:
       0: iload_1
       1: iload_2
       2: iadd
       3: iload_1
       4: iload_2
       5: iadd
       6: ireturn
    LineNumberTable:
      line 6: 0
      line 7: 3
}
"""


def test_parse_javap_groups_instructions_by_method_and_descriptor():
    methods = inject._parse_javap(JAVAP_ADD, "Foo")
    assert ("add", "(II)I") in methods
    assert ("<init>", "()V") in methods, "constructor should be keyed as <init>"

    add = methods[("add", "(II)I")]
    assert ("2", "iadd") not in add["insns"], "offsets must be ints, not strings"
    assert (2, "iadd") in add["insns"]
    assert (5, "iadd") in add["insns"]


def test_parse_javap_reads_line_number_table_as_offset_to_line():
    methods = inject._parse_javap(JAVAP_ADD, "Foo")
    assert methods[("add", "(II)I")]["lnt"] == [(0, 6), (3, 7)]


def test_parse_javap_ignores_text_before_any_method():
    assert inject._parse_javap("random text\n  0: iadd\n", "Foo") == {}


JAVAP_CLINIT = """\
public class com.example.Foo {
  static {};
    descriptor: ()V
    Code:
       0: iconst_1
       1: putstatic #2
    LineNumberTable:
      line 4: 0
}
"""


def test_parse_javap_recognizes_a_static_initializer():
    methods = inject._parse_javap(JAVAP_CLINIT, "Foo")
    assert ("<clinit>", "()V") in methods
    assert (0, "iconst_1") in methods[("<clinit>", "()V")]["insns"]


def test_parse_javap_drops_instructions_under_an_unrecognized_descriptor():
    """A `descriptor:` whose preceding line is not a method signature must reset
    the current method, not silently attach its opcodes to the previous one."""
    text = JAVAP_ADD + "  Constant pool:\n    descriptor: ()V\n       0: nop\n"
    methods = inject._parse_javap(text, "Foo")
    assert all("nop" not in [m for _, m in info["insns"]] for info in methods.values())


def test_load_class_bytecode_returns_empty_for_missing_file(tmp_path):
    assert inject.load_class_bytecode(str(tmp_path / "Nope.class")) == {}


def test_load_class_bytecode_parses_javap_output(tmp_path, monkeypatch):
    class_file = tmp_path / "Foo.class"
    class_file.write_bytes(b"\xca\xfe\xba\xbe")
    monkeypatch.setattr(
        inject.subprocess, "run",
        lambda *a, **k: type("R", (), {"stdout": JAVAP_ADD})(),
    )
    assert ("add", "(II)I") in inject.load_class_bytecode(str(class_file))


def test_load_class_bytecode_survives_a_missing_javap(tmp_path, monkeypatch):
    """javap is optional -- occurrence resolution falls back to positional counting."""
    class_file = tmp_path / "Foo.class"
    class_file.write_bytes(b"\xca\xfe\xba\xbe")

    def boom(*a, **k):
        raise FileNotFoundError("javap")

    monkeypatch.setattr(inject.subprocess, "run", boom)
    assert inject.load_class_bytecode(str(class_file)) == {}


# ------------------------------------------------------- occurrence from bytecode

def test_resolve_occ_ranks_matching_opcodes_on_the_same_line():
    methods = inject._parse_javap(JAVAP_ADD, "Foo")
    desc = "Replaced integer addition with subtraction"
    # offsets 2 and 5 are both iadd, but only offset 2 maps to line 6
    assert inject.resolve_occ_from_bytecode(methods, "add", "(II)I", 2, 6, desc) == 0
    assert inject.resolve_occ_from_bytecode(methods, "add", "(II)I", 5, 7, desc) == 0


def test_resolve_occ_returns_none_when_index_is_not_on_that_line():
    methods = inject._parse_javap(JAVAP_ADD, "Foo")
    desc = "Replaced integer addition with subtraction"
    assert inject.resolve_occ_from_bytecode(methods, "add", "(II)I", 5, 6, desc) is None


def test_resolve_occ_returns_none_for_unknown_method_or_description():
    methods = inject._parse_javap(JAVAP_ADD, "Foo")
    assert inject.resolve_occ_from_bytecode(methods, "nope", "()V", 0, 1, "x") is None
    assert inject.resolve_occ_from_bytecode(methods, "add", "(II)I", 2, 6, "unknown") is None


# -------------------------------------------------------------------- inject_at

def test_inject_at_preserves_the_original_indentation():
    lines = ["class A {", "        return a + b;", "}"]
    out = inject.inject_at(lines, 2, 2, "return a - b;")
    assert out == "class A {\n        return a - b;\n}\n"


def test_inject_at_collapses_a_multi_line_statement():
    lines = ["class A {", "    return a", "        + b;", "}"]
    out = inject.inject_at(lines, 2, 3, "return a - b;")
    assert out == "class A {\n    return a - b;\n}\n"


def test_inject_at_keeps_later_lines_of_a_multi_line_replacement():
    lines = ["class A {", "    int x = 1;", "}"]
    out = inject.inject_at(lines, 2, 2, "int x = 1;\n    int y = 2;")
    assert out.splitlines() == ["class A {", "    int x = 1;", "    int y = 2;", "}"]


@pytest.mark.parametrize("start, end", [(0, 1), (1, 99), (99, 99), (3, 2)])
def test_inject_at_returns_source_unchanged_for_out_of_range_spans(start, end):
    lines = ["a", "b"]
    assert inject.inject_at(lines, start, end, "zzz") == "a\nb\n"


# ---------------------------------------------------------------- validate_syntax

def test_validate_syntax_accepts_real_java():
    assert inject.validate_syntax(SIMPLE_JAVA)


def test_validate_syntax_rejects_an_unterminated_literal():
    assert not inject.validate_syntax('class A { String s = "unterminated; }')


# ----------------------------------------------------------------- CLI targeting

def test_parse_line_target_splits_class_method_and_line():
    assert inject.parse_line_target("org.joda.time.DateTime.plus:614") == (
        "org.joda.time.DateTime", "plus", 614,
    )


@pytest.mark.parametrize("bad", ["no_colon", "NoDots:12"])
def test_parse_line_target_rejects_malformed_targets(bad):
    with pytest.raises(ValueError):
        inject.parse_line_target(bad)


def test_file_target_to_rel_accepts_both_fqn_and_filename():
    assert inject.file_target_to_rel("com.example.Foo") == "com/example/Foo.java"
    assert inject.file_target_to_rel("Foo.java") == "Foo.java"


INFO = {
    "index_no": 7,
    "mutated_class": "com.example.Foo",
    "mutated_method": "add",
    "line_number": 6,
    "source_file": "com/example/Foo.java",
}


def test_matches_without_a_mode_accepts_everything():
    assert inject.matches(INFO, None, None)


@pytest.mark.parametrize(
    "mode, target, expected",
    [
        ("id", "7", True),
        ("id", "8", False),
        ("line", "com.example.Foo.add:6", True),
        ("line", "com.example.Foo.add:9", False),
        ("line", "com.example.Bar.add:6", False),
        ("file", "com.example.Foo", True),
        ("file", "Foo.java", True),
        ("file", "com.example.Other", False),
    ],
)
def test_matches_selects_by_mode(mode, target, expected):
    assert inject.matches(INFO, mode, target) is expected


def test_matches_rejects_an_unknown_mode():
    with pytest.raises(ValueError):
        inject.matches(INFO, "bogus", "x")


# ------------------------------------------------------------------ write_mutant

def test_write_mutant_names_the_file_by_index_and_line(tmp_path):
    info = dict(
        INFO,
        source_lines=SIMPLE_JAVA.split("\n"),
        stmt_start=6,
        stmt_end=6,
        raw_mutated_line="return a - b;",
    )
    assert inject.write_mutant(str(tmp_path), info) is True

    out = tmp_path / "Foo_id7_line6.java"
    assert out.exists(), [p.name for p in tmp_path.iterdir()]
    assert "return a - b;" in out.read_text(encoding="utf-8")


def test_write_mutant_reports_a_mutant_that_will_not_tokenize(tmp_path):
    info = dict(
        INFO,
        source_lines=SIMPLE_JAVA.split("\n"),
        stmt_start=6,
        stmt_end=6,
        raw_mutated_line='return "unterminated;',
    )
    assert inject.write_mutant(str(tmp_path), info) is False


# ------------------------------------------------------------------------- main

def run_main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["inject.py"] + argv)
    with pytest.raises(SystemExit) as exc:
        inject.main()
    return exc.value.code


def test_main_writes_one_file_per_mutation(fake_project, monkeypatch, capsys):
    proj = fake_project(SIMPLE_JAVA, simple_mutations())
    monkeypatch.setattr(sys, "argv", ["inject.py", str(proj)])
    inject.main()

    written = sorted(p.name for p in (proj / "injected_mutants").iterdir())
    assert len(written) == 2, written
    assert all(n.endswith(".java") for n in written)
    assert "Wrote 2 mutants" in capsys.readouterr().out


def test_main_id_mode_writes_exactly_one_mutant(fake_project, monkeypatch):
    proj = fake_project(SIMPLE_JAVA, simple_mutations())
    monkeypatch.setattr(sys, "argv", ["inject.py", str(proj), "id", "1"])
    inject.main()
    assert len(list((proj / "injected_mutants").iterdir())) == 1


def test_main_file_mode_selects_by_source_file(fake_project, monkeypatch):
    proj = fake_project(SIMPLE_JAVA, simple_mutations())
    monkeypatch.setattr(sys, "argv", ["inject.py", str(proj), "file", "com.example.Foo"])
    inject.main()
    assert len(list((proj / "injected_mutants").iterdir())) == 2


def test_main_skips_mutations_it_cannot_reconstruct(fake_project, monkeypatch, capsys):
    """Unreconstructible mutations must be dropped, never written out with the
    ' // MUTATED: ' fallback marker still in the emitted .java file."""
    muts = simple_mutations()
    muts[0]["lineNumber"] = 9999                                    # out of range
    muts[1]["description"] = "Replaced integer addition with subtraction"  # no '+' on line
    proj = fake_project(SIMPLE_JAVA, muts)
    assert run_main(monkeypatch, [str(proj)]) == 1
    assert "No mutations matched" in capsys.readouterr().out


def test_main_finds_sources_under_generated_sources(fake_project, monkeypatch):
    import shutil

    proj = fake_project(SIMPLE_JAVA, simple_mutations())
    src = proj / "src" / "main" / "java" / "com" / "example" / "Foo.java"
    generated = proj / "target" / "generated-sources" / "jjtree" / "com" / "example"
    generated.mkdir(parents=True)
    shutil.move(str(src), str(generated / "Foo.java"))

    monkeypatch.setattr(sys, "argv", ["inject.py", str(proj)])
    inject.main()
    assert len(list((proj / "injected_mutants").iterdir())) == 2


def test_main_exits_when_nothing_matches(fake_project, monkeypatch, capsys):
    proj = fake_project(SIMPLE_JAVA, simple_mutations())
    assert run_main(monkeypatch, [str(proj), "id", "999"]) == 1
    assert "No mutations matched" in capsys.readouterr().out


def test_main_exits_on_a_malformed_line_target(fake_project, monkeypatch, capsys):
    proj = fake_project(SIMPLE_JAVA, simple_mutations())
    assert run_main(monkeypatch, [str(proj), "line", "garbage"]) == 1
    assert "Error" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv_tail, expected_output",
    [
        ([], "Usage"),
        (["/definitely/not/a/dir"], "is not a directory"),
    ],
)
def test_main_rejects_bad_invocations(monkeypatch, capsys, argv_tail, expected_output):
    assert run_main(monkeypatch, argv_tail) == 1
    assert expected_output in capsys.readouterr().out


def test_main_rejects_an_unknown_mode(fake_project, monkeypatch, capsys):
    proj = fake_project(SIMPLE_JAVA, simple_mutations())
    assert run_main(monkeypatch, [str(proj), "bogus", "x"]) == 1
    assert "unknown mode" in capsys.readouterr().out


def test_main_requires_a_target_for_every_mode(fake_project, monkeypatch, capsys):
    proj = fake_project(SIMPLE_JAVA, simple_mutations())
    assert run_main(monkeypatch, [str(proj), "id"]) == 1
    assert "requires a target" in capsys.readouterr().out


def test_main_reports_a_missing_pit_report(tmp_path, monkeypatch, capsys):
    assert run_main(monkeypatch, [str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "mutations.xml" in out and "not found" in out
