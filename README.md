# PITMuS: PIT Mutations In the Source Code

<p>
  <a href="https://youtu.be/zgHkXnsgciw">
    <img src="https://img.youtube.com/vi/zgHkXnsgciw/hqdefault.jpg" alt="Watch the demo" width="480">
  </a>
  <br>
  <a href="https://youtu.be/zgHkXnsgciw">▶ Watch the demo</a>
</p>

PIT (Pitest) mutates Java **bytecode** and never exports mutated *source*. PITMuS bridges that
gap: it parses PIT's XML report, maps each mutation back to the exact source line (using both the
report's bytecode `index` and `javap` disassembly for precision), applies the mutation, and emits
either a **dataset of mutated methods** or **fully injected mutant `.java` files**.

This repo has two layers:

1. **Reconstruction** — turn a PIT report into source-level mutants. Everything lives under the
   `PITMuS/` folder: the engine is the `PITMuS/shared/` package, and `PITMuS/gen_dataset.py` and
   `PITMuS/inject.py` are thin CLIs over it.
2. **Evaluation** (`evaluation/`) — prove the reconstructions are faithful, by compiling them
   and diffing their bytecode against the mutant `.class` files PIT itself exports.

---

## Repository Structure

```
PITMuS/                               ← repo root
├── PITMuS/                           ← reconstruction tool: CLIs + shared engine
│   ├── shared/                       ← the reconstruction engine (importable package)
│   │   ├── __init__.py               ← public API (re-exports shared.mutate)
│   │   ├── mutate.py                 ← statement location + mutator application
│   │   └── version.py                ← dataset output-folder name (single source of truth)
│   ├── run_pit.sh                    ← runs each project's pit.sh (mvn test + pitest EXPORT)
│   ├── gen_dataset.py                ← PIT report → dataset CSVs   (main entry point)
│   ├── inject.py                     ← PIT report → mutant .java files (standalone tool)
│   └── pitmus_config.py              ← deprecated shim → shared.version
├── evaluation/
│   ├── evaluate_reconstruction.ipynb ← the 4 evaluations (eval0–eval3) that grade a dataset
│   ├── PitmusCompile.java            ← in-JVM batch compiler used by the bytecode oracle
│   └── evaluation_results/
│       └── <project>_results/        ← per-project evaluation output (CSVs + Evaluation-*.txt
│                                        + skipped-reconstructions-samples-<project>.txt)
├── test-projects/
│   └── <project>/
│       ├── src/main/java/            ← project source
│       ├── pit.sh                    ← mvn test + pitest:mutationCoverage -Dfeatures=+EXPORT
│       ├── target/pit-reports/
│       │   ├── mutations.xml         ← PIT's report (INPUT to everything)
│       │   └── export/               ← PIT's exported mutant .class files (ground truth for eval3)
│       ├── target/classes/           ← compiled classes (for javap + compile classpath)
│       ├── PITMuS_dataset/           ← created by gen_dataset.py
│       │   ├── mutated_methods-<project>.csv
│       │   └── meta-<project>.csv
│       └── injected_mutants/         ← created by inject.py
│           └── <ClassName>_id<N>_line<L>.java
├── pyproject.toml                    ← makes `pitmus` pip-installable
├── requirements.txt
└── README.md
```

Both CLIs in `PITMuS/` are **independent front-ends** — either works on its own — but they share
one reconstruction engine (`PITMuS/shared/mutate.py`), so a fix to how a mutation is reconstructed
applies to the dataset and the injected `.java` files alike. They previously carried separate
copies of that logic, which drifted.

---

## Prerequisites

- **Python 3.6+** (tested on 3.12), deps via `pip install -r requirements.txt` (`javalang` for
  reconstruction; `notebook` + `ipykernel` to run the evaluations).
  The `PITMuS/` CLIs put their own folder on `sys.path` themselves, so they run straight from a
  clone with nothing installed. To `import shared` from elsewhere (a notebook, your own code),
  install it instead: `pip install -e .`
- **A JDK on `PATH`** — the scripts call `javap` for bytecode-accurate targeting; the evaluation
  notebook calls `javac`/`javap`.
- **Maven** — to build subject projects and (optionally) resolve the compile classpath in eval3.
- Each subject project must have a generated `target/pit-reports/mutations.xml` **and**, for the
  bytecode oracle, PIT's exported mutants under `target/pit-reports/export/` (see flags below).

---

## Pipeline / How to Regenerate

```bash
# 0. Produce PIT reports for every test-project (mvn test + pitest with EXPORT on).
bash PITMuS/run_pit.sh

# 1. Reconstruct source-level mutants for one project -> dataset CSVs.
python PITMuS/gen_dataset.py test-projects/joda-time

# 2. (optional) Materialize mutant .java files for one project.
python PITMuS/inject.py test-projects/joda-time

# 3. Grade the reconstruction: open the notebook, set PROJECT, run top to bottom.
#    evaluation/evaluate_reconstruction.ipynb
```

> `PITMuS/run_pit.sh` currently hardcodes `TEST_PROJECTS_DIR` to an absolute path — set it to
> your own `test-projects/` before running step 0.


### 1. `gen_dataset.py` — dataset CSVs

```bash
python PITMuS/gen_dataset.py <system_path>
```

Reads `<system_path>/target/pit-reports/mutations.xml`, resolves each mutation to its source line,
applies it, finds the enclosing method body, and writes two row-aligned CSVs into
`<system_path>/PITMuS_dataset/`.

**`mutated_methods-<project>.csv`** — one row per mutation, full method bodies:

| Column | Description |
|---|---|
| `index_no` | Sequential id (shared with `meta-<project>.csv`) |
| `original_method` | Full body of the method containing the mutated line |
| `mutated_method` | Same body with the mutated line substituted |
| `docstring` | Javadoc block preceding the method, or empty |

**`meta-<project>.csv`** — joined to the above by `index_no`:

| Column | Description |
|---|---|
| `mutation_line` | Original source line at the mutation site |
| `mutated_line` | Source line after the mutation |
| `source_file` | Path e.g. `org/joda/time/DateTime.java` |
| `stmt_start_line` | First line of the (possibly multi-line) mutated statement |
| `pit_line_number` | Line number PIT reported |
| `description` | PIT's mutation description |
| `test_file` | Covering test file(s), `\|`-separated |
| `index_no` | Same id as `mutated_methods-<project>.csv` |
| `xml_line` | Physical line of the source `<mutation>` in `mutations.xml` (traceability) |

### 2. `inject.py` — mutant `.java` files

Writes each selected mutant as a full file in `<system_path>/injected_mutants/`, named
`<ClassName>_id<N>_line<L>.java` (`<N>` = the `index_no` from the dataset). Four selection modes:

```bash
python PITMuS/inject.py <system_path>                              # every mutation
python PITMuS/inject.py <system_path> id   <index_no>              # one mutation by id
python PITMuS/inject.py <system_path> line <class.method:line>     # all on a method:line
python PITMuS/inject.py <system_path> file <class_fqn | file.java> # all in one file
```

After writing each file it runs a `javalang` tokenizer check and flags any that fail with `[INVALID]`.

### 3. `evaluate_reconstruction.ipynb` — the evaluations

Set `REPO` and `PROJECT` in the config cell, then run top to bottom. It writes into
`evaluation/evaluation_results/<project>_results/` and prints a consolidated `Evaluation-<project>_<VERSION>.txt`.

| Evaluations | Question | Output |
|---|---|---|
| **eval0** XML alignment | does each row point back to the right `<mutation>`? | `eval0_xml_misalign_*.csv` |
| **eval1** Count | was every XML mutation reconstructed? | `eval1_not_reconstructed_*.csv` |
| **eval2** Faithfulness | is the edit correct + still valid Java? (lexical) | `eval2_faithfulness_*.csv` |
| **eval3** Bytecode ground truth | does the *compiled* mutant equal PIT's exported `.class`? | `eval3_bytecode_*_broken.csv`, `_other.csv` |
| **eval4** Report | roll-up of all of the above | `Evaluation-<project>_<VERSION>.txt` |

**eval3 verdicts** (the authoritative evaluation): `MATCH`/`EQUIVALENT` = confirmed faithful;
`BROKEN` = a genuine reconstruction fault (won't compile for a real reason — this is the only
bucket in `*_broken.csv`); `UNREPRESENTABLE` = faithful mutant Java source can't legally express
(e.g. `for(;false;)` → "unreachable statement"); `DIVERGENT` = compiles but bytecode differs
(usually the same dead-code encoding difference as `EQUIVALENT`). Everything that isn't `BROKEN`
lands in `*_other.csv`. eval2 is a cheap lexical net that also catches non-parsing and no-op
reconstructions and covers rows eval3 can't compile — keep both.

---

## Flags & Knobs Worth Knowing

| Where | Flag | Effect |
|---|---|---|
| `pit.sh` / PIT config | `-Dfeatures=+EXPORT` | Exports mutant `.class` files to `target/pit-reports/export/`. **Required for eval3.** |
| PIT config | `<fullMutationMatrix>true`, `<exportLineCoverage>true` | Richer report (test matrix + line coverage). |
| `PITMuS/shared/version.py` | `dataset_dirname()` | Single source of truth for the dataset output-folder name (`PITMuS_dataset`). `gen_dataset.py` reads it; the notebook hardcodes the same name, so keep the two in step. |
| `gen_dataset.py` (env) | `PITMUS_DEBUG_SKIPS=1` | Prints, to stderr, every mutation it *skipped* and why (single-line methods, unresolved spans, …). |
| notebook eval3 | `BC_SAMPLE = None` | `None` = check all rows; set an int for a quick sample. |
| notebook eval3 | `BC_WORKERS`, `BC_CHUNK` | Parallelism (defaults to CPU count) and rows per compile batch. |
| notebook eval3 | (auto) `target/pitmus-deps.cp` | Cached Maven dependency classpath; without it, deps-referencing reconstructions can be falsely `BROKEN`. Auto-built once via `mvn dependency:build-classpath`. |

---

## Supported Mutators

The engine handles all 13 mutators in PIT's **STRONGER** group (DEFAULTS + `REMOVE_CONDITIONALS`
+ `EXPERIMENTAL_SWITCH`).

| Mutator | Example |
|---|---|
| ConditionalsBoundary | `>` → `>=` |
| Math | `+` → `-`, `*` → `/`, `%` → `*` |
| NegateConditionals | `==` → `!=`, `>=` → `<` |
| RemoveConditionals | `if (x == y)` → `if (true)`, ternary conditions |
| IncrementsMutator | `i++` → `i--`, `-4` → `4` |
| InvertNegs | removes unary negation |
| VoidMethodCall | removes the call |
| Empty / Null / Primitive / True / False Returns | `return x;` → `return null;` / `true` / `Collections.emptyMap()` |
| Bitwise / Shift | `&` → `\|`, `<<` → `>>` |

### What PITMuS does *not* reconstruct

A mutation is skipped when its bytecode target has **no matching token in the source line** —
the operation is compiler-synthesized, so there is nothing to edit. This is rare (34 of ~51k
mutations across 7 projects, ~0.06%) and covers two cases:

- **`VoidMethodCall` on synthetic `access$NNN` accessors** — bridge methods the compiler generates
  for inner-class member access; no such call exists in source.
- **`Math` on compiler-generated arithmetic** — byte-level math (string-switch hashing, boxing,
  index math) with no source operator.

Everything on real, hand-written expressions is reconstructed. `gen_dataset.py` logs each skip to
`evaluation/evaluation_results/<project>_results/skipped-reconstructions-samples-<project>.txt`.

> **Note — the JDK version affects the mutation set.** PIT mutates *bytecode*, and `javac` compiles
> the same source differently across Java versions (e.g. string concat via `StringBuilder` vs
> `invokedynamic` in Java 9+, `access$NNN` accessors dropped by Java 11+ nestmates). So the **same
> source can yield a different `mutations.xml` on a different JDK** — reconstruct with the same JDK
> used to run PIT.

---

## Generating a PIT Report (per project)

Add the plugin to the project's `pom.xml` (update `targetClasses` / `targetTests` to its packages):

```xml
<plugin>
  <groupId>org.pitest</groupId>
  <artifactId>pitest-maven</artifactId>
  <version>1.22.0</version>
  <configuration>
    <targetClasses><param>org.apache.commons.lang3.*</param></targetClasses>
    <targetTests><param>org.apache.commons.lang3.*</param></targetTests>
    <mutators><mutator>STRONGER</mutator></mutators>
    <fullMutationMatrix>true</fullMutationMatrix>
    <exportLineCoverage>true</exportLineCoverage>
    <outputFormats><param>XML</param></outputFormats>
  </configuration>
</plugin>
```

```bash
# EXPORT is what writes target/pit-reports/export/ (needed by the eval3 bytecode oracle).
mvn clean test org.pitest:pitest-maven:mutationCoverage -Dfeatures=+EXPORT
```

The report lands at `target/pit-reports/mutations.xml`.

---

## License

Apache License 2.0 — see [LICENSE](LICENSE).

## Citation

```bibtex
@misc{pitmus,
  author  = {Tasfia Tasnim, Soneya Binta Hossain},
  title   = {{PITMuS}: {PIT} {Mutations} in the {Source} {Code}},
  year    = {2026},
  version = {1.0.0},
  url     = {https://github.com/assert-lab/PITMuS}
}
```
