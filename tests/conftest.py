"""Shared fixtures and helpers for the PITMuS test suite.

Runs against a bare clone: puts the `PITMuS/` folder on sys.path so `import shared`
resolves without installing the package.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_DIR = REPO_ROOT / "PITMuS"
sys.path.insert(0, str(TOOL_DIR))

import javalang  # noqa: E402


def tok(line):
    """Tokenize exactly the string that will be passed as `line`.

    Token columns are absolute within the line, so the tokenized text MUST be the
    identical string given to apply_mutation() — tokenizing a stripped copy shifts
    every position and silently produces garbage edits.
    """
    return list(javalang.tokenizer.tokenize(line))


def tok_all(source):
    """Tokenize a whole multi-line source string."""
    return list(javalang.tokenizer.tokenize(source))


def mutate_line(line, desc, occ=0, int_locals=frozenset()):
    """apply_mutation() on a single line, with correctly aligned tokens."""
    from shared import apply_mutation

    return apply_mutation(line, tok(line), desc, occ, int_locals)


FALLBACK = " // MUTATED: "


def is_fallback(result, desc=None):
    """True when the engine gave up and appended its fallback marker."""
    if FALLBACK not in result:
        return False
    return desc is None or result.endswith(FALLBACK + desc)


@pytest.fixture
def java_file(tmp_path):
    """Write a Java source string to disk and return its path (for load_source)."""

    def _write(source, name="Example.java"):
        p = tmp_path / name
        p.write_text(source, encoding="utf-8")
        return str(p)

    return _write


@pytest.fixture(scope="session")
def repo_root():
    return REPO_ROOT


# --------------------------------------------------------------- fake projects
# The CLIs take a project directory, not arguments, so exercising main() needs a
# real one on disk. These build the smallest tree the CLIs will accept:
#
#   <root>/src/main/java/<pkg>/<Class>.java
#   <root>/target/pit-reports/mutations.xml
#
# mutations.xml is written one <mutation> per line on purpose -- gen_dataset maps
# each row back to its physical XML line and that mapping assumes this layout.

MUTATION_FIELDS = (
    "sourceFile", "mutatedClass", "mutatedMethod", "methodDescription",
    "lineNumber", "mutator", "description",
    # both PIT test-provenance dialects: plural (fullMutationMatrix) and singular
    "killingTests", "coveringTests", "killingTest", "coveringTest", "succeedingTests",
)


def mutation_xml(mutations):
    """Serialize dicts to a PIT mutations.xml string, one <mutation> per line."""
    from xml.sax.saxutils import escape

    rows = []
    for m in mutations:
        parts = []
        for field in MUTATION_FIELDS:
            if field not in m:
                continue
            # a list emits the element repeatedly -- PIT may do this for test elements
            values = m[field] if isinstance(m[field], list) else [m[field]]
            for value in values:
                # PIT escapes too -- <clinit> and generics would break the XML
                parts.append(f"<{field}>{escape(str(value))}</{field}>")
        for index in m.get("indexes", []):
            parts.append(f"<indexes><index>{index}</index></indexes>")
        for block in m.get("blocks", []):
            parts.append(f"<blocks><block>{block}</block></blocks>")
        rows.append("<mutation detected='true' status='KILLED'>" + "".join(parts) + "</mutation>")
    return '<?xml version="1.0" encoding="UTF-8"?>\n<mutations>\n' + "\n".join(rows) + "\n</mutations>\n"


@pytest.fixture
def fake_project(tmp_path):
    """Build a minimal PIT-style project tree; returns its path."""

    def _build(java_source, mutations, package="com.example", class_name="Foo", name="proj"):
        root = tmp_path / name
        pkg_dir = root / "src" / "main" / "java" / Path(*package.split("."))
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / f"{class_name}.java").write_text(java_source, encoding="utf-8")

        reports = root / "target" / "pit-reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / "mutations.xml").write_text(mutation_xml(mutations), encoding="utf-8")
        return root

    return _build


@pytest.fixture
def run_cli(monkeypatch, repo_root):
    """Run gen_dataset.main() on a project and clean up the skip report it writes.

    main() writes its skip report into the *real* repo tree (keyed by project name),
    not into tmp_path, so the fixture removes that directory afterwards.
    """
    import shutil

    import gen_dataset

    created = []

    def _run(project_dir):
        project = project_dir.name
        created.append(
            REPO_ROOT / "evaluation" / "evaluation_results" / f"{project}_results"
        )
        monkeypatch.setattr(sys, "argv", ["gen_dataset.py", str(project_dir)])
        gen_dataset.main()
        dataset = project_dir / gen_dataset.DATASET
        return {
            "meta": dataset / f"meta-{project}.csv",
            "methods": dataset / f"mutated_methods-{project}.csv",
            "skips": created[-1] / f"skipped-reconstructions-samples-{project}.txt",
        }

    yield _run

    for path in created:
        shutil.rmtree(path, ignore_errors=True)


SIMPLE_JAVA = """package com.example;

public class Foo {
    /** Adds two numbers. */
    public int add(int a, int b) {
        return a + b;
    }

    public boolean big(int n) {
        if (n > 10) {
            return true;
        }
        return false;
    }
}
"""


def simple_mutations():
    """Two reconstructible mutations against SIMPLE_JAVA."""
    return [
        {
            "sourceFile": "Foo.java", "mutatedClass": "com.example.Foo",
            "mutatedMethod": "add", "methodDescription": "(II)I", "lineNumber": 6,
            "mutator": "org.pitest.mutationtest.engine.gregor.mutators.MathMutator",
            "description": "Replaced integer addition with subtraction",
            "killingTests": "[engine:junit-jupiter]/[class:com.example.FooTest]/[method:add()]",
            "indexes": [4], "blocks": [0],
        },
        {
            "sourceFile": "Foo.java", "mutatedClass": "com.example.Foo",
            "mutatedMethod": "big", "methodDescription": "(I)Z", "lineNumber": 10,
            "mutator": "org.pitest.mutationtest.engine.gregor.mutators.ConditionalsBoundaryMutator",
            "description": "changed conditional boundary",
            "coveringTests": "[engine:junit-jupiter]/[class:com.example.FooTest]/[method:big()]",
            "indexes": [2], "blocks": [0],
        },
    ]


def pytest_addoption(parser):
    parser.addoption(
        "--runslow",
        action="store_true",
        default=False,
        help="also replay the large test-projects in the golden dataset regression (~2 min)",
    )
