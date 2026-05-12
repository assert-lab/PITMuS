# PITMuS: PIT Mutations In the Source Code

[![Watch the demo](https://img.youtube.com/vi/zgHkXnsgciw/maxresdefault.jpg)](https://youtu.be/zgHkXnsgciw)

This repository contains two end-to-end scripts that extract and inject source-level mutations from PIT (Pitest) XML reports.

PIT operates at the bytecode level and does not export mutated source code. PITMuS bridges that gap by parsing PIT's XML output, mapping each mutation back to its source line (using both the report's bytecode index and `javap` output for precision), and applying the mutation description to produce a mutated source line — and, when needed, a mutated full method body or a fully injected mutant `.java` file.

The two scripts are **fully independent** of each other; either can be used on its own.

## Repository Structure

```
PITMuS/
├── scripts/
│   ├── gen_dataset.py                ← end-to-end: PIT report → dataset CSVs
│   └── inject.py                     ← end-to-end: PIT report → mutant .java files
└── test-projects/
    └── <system>/
        ├── src/main/java/            ← project source code
        ├── target/pit-reports/mutations.xml
        ├── target/classes/           ← compiled .class files (used for javap)
        ├── PITMuS_dataset/           ← created by gen_dataset.py
        │   ├── mutated_methods.csv
        │   └── meta.csv
        └── injected_mutants/         ← created by inject.py
            ├── ClassName_id1_line95.java
            ├── ClassName_id2_line95.java
            └── ...
```

## Prerequisites

- Python 3.6+
- Python dependencies — install with:
  ```bash
  pip install -r requirements.txt
  ```
  (`javalang` for source parsing.)
- A JDK on `PATH` (both scripts invoke `javap` to read compiled `.class` files for bytecode-accurate mutation targeting).
- A Maven project with PIT configured, a generated `mutations.xml` report, and compiled classes under `target/classes/`.

## Usage

### Generate the dataset

```bash
python scripts/gen_dataset.py <system_path>
```

This reads `<system_path>/target/pit-reports/mutations.xml`, resolves each mutation to its source line, applies the mutation, locates the enclosing method body, and writes two CSVs into `<system_path>/PITMuS_dataset/`.

Example:

```bash
python scripts/gen_dataset.py test-projects/joda-time
```

#### `mutated_methods.csv` — one row per mutation, full method bodies

| Column | Description |
|---|---|
| `index_no` | Sequential row identifier (shared with `meta.csv`) |
| `original_method` | Full body of the method containing the mutated line |
| `mutated_method` | Same method body with the mutated line substituted |
| `docstring` | Javadoc block (`/** ... */`) immediately preceding the method, or empty |

#### `meta.csv` — row-aligned with `mutated_methods.csv` via `index_no`

| Column | Description |
|---|---|
| `mutation_line` | Original source line at the mutation site |
| `mutated_line` | Source line after applying the mutation |
| `source_file` | Path to the source file (e.g. `org/joda/time/DateTime.java`) |
| `line_number` | Line number in the source file |
| `description` | PIT's mutation description |
| `test_file` | Test file(s) covering the mutation, separated by `\|` |
| `index_no` | Same id as the corresponding row in `mutated_methods.csv` |

### Inject mutations into source

`inject.py` supports four selection modes. Each matching mutation is written as its own full `.java` file in `<system_path>/injected_mutants/`, named `<ClassName>_id<N>_line<L>.java`, where `<N>` is the `index_no` from the dataset.

```bash
# Whole system — inject every mutation
python scripts/inject.py <system_path>

# One specific mutation by its dataset index_no
python scripts/inject.py <system_path> id <index_no>

# Every mutation on a specific method:line
python scripts/inject.py <system_path> line <class.method:line>

# Every mutation in a specific source file (FQN or filename)
python scripts/inject.py <system_path> file <class_fqn | file.java>
```

Examples:

```bash
python scripts/inject.py test-projects/joda-time
python scripts/inject.py test-projects/joda-time id 614
python scripts/inject.py test-projects/joda-time line org.joda.time.DateTime.plus:614
python scripts/inject.py test-projects/joda-time file org.joda.time.DateTime
```

The `id` for a given mutation is the same `index_no` that `gen_dataset.py` writes into `meta.csv`, so a typical workflow is to inspect `PITMuS_dataset/meta.csv` and then re-inject any specific mutation by its id. After writing each mutant file, the script also runs a lightweight `javalang` tokenizer check and flags any that fail.

## Supported Mutators

Both scripts handle all 13 mutators in PIT's STRONGER group (DEFAULTS + `REMOVE_CONDITIONALS` + `EXPERIMENTAL_SWITCH`).

| Mutator | Example |
|---|---|
| ConditionalsBoundary | `>` → `>=`, etc. |
| Math | `+` → `-`, `*` → `/`, `%` → `*`, etc. |
| NegateConditionals | `==` → `!=`, `>=` → `<` |
| RemoveConditionals | `if (x == y)` → `if (true)`, ternary conditions |
| IncrementsMutator | `i++` → `i--`, `-4` → `4`, etc. |
| InvertNegs | removes unary negation |
| VoidMethodCall | removes the method call entirely |
| Empty / Null / Primitive / True / False Returns | `return x;` → `return null;` / `return true;` / `return Collections.emptyMap();` / etc. |
| Bitwise / Shift | `&` → `\|`, `<<` → `>>`, etc. |

## Generating a PIT Report

If you need to generate a PIT mutation report for a Maven project, add the following plugin to the project's `pom.xml`. The example below is configured for Apache Commons Lang 3 — update `targetClasses` and `targetTests` to match the subject project's package structure.

```xml
<plugin>
  <groupId>org.pitest</groupId>
  <artifactId>pitest-maven</artifactId>
  <version>1.22.0</version>
  <configuration>
    <targetClasses>
      <param>org.apache.commons.lang3.*</param>
    </targetClasses>
    <targetTests>
      <param>org.apache.commons.lang3.*</param>
    </targetTests>
    <mutators>
      <mutator>STRONGER</mutator>
    </mutators>
    <fullMutationMatrix>true</fullMutationMatrix>
    <exportLineCoverage>true</exportLineCoverage>
    <outputFormats>XML</outputFormats>
  </configuration>
</plugin>
```

Then run:

```bash
mvn clean test org.pitest:pitest-maven:mutationCoverage
```

The XML report is written to `target/pit-reports/mutations.xml`.

## License

This project is licensed under the Apache License 2.0 — see the [LICENSE](LICENSE) file for details.

## Citation

If you use PITMuS in your work, please cite it:

```bibtex
@misc{pitmus,
  author  = {Tasfia Tasnim, Soneya Binta Hossain},
  title   = {{PITMuS}: {PIT} {Mutations} in the {Source} {Code}},
  year    = {2026},
  version = {1.0.0},
  url     = {https://github.com/assert-lab/PITMuS}
}
```
