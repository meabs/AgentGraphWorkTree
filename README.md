# Agent Graph Work Tree

[![CI](https://github.com/meabs/AgentGraphWorkTree/actions/workflows/test.yml/badge.svg)](https://github.com/meabs/AgentGraphWorkTree/actions/workflows/test.yml)
![version](https://img.shields.io/badge/version-0.3.6-blue)
![license](https://img.shields.io/badge/license-MIT-green)
![platforms](https://img.shields.io/badge/platforms-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

A code graph for your repository, generated into the work tree, so a coding agent can ask *"who calls this?"*, *"what extends this?"* or *"what does this change touch?"* and get a precise answer in one command instead of opening dozens of files.

A script parses the source (no language model involved) and writes `.agent-knowledge/`. Every fact cites a file and line and carries a confidence tag, so an agent knows what to trust and what to check. The installer wires the graph into the instruction files that Claude Code, Codex, Cursor and GitHub Copilot already read.

| Language | Frameworks and files understood |
| --- | --- |
| **Java** | Spring, Quarkus, JPA, Spring Data, OpenAPI, `pom.xml`, SQL, properties, templates |
| **Python** | Flask and FastAPI routes, dataclasses, test modules |
| **TypeScript / JavaScript** | React, Express, NestJS, Next.js, React Router, `package.json` |

![How the graph is built](docs/img/pipeline.svg)

**[Project page with live examples](https://meabs.github.io/AgentGraphWorkTree/)** · [Evaluation](docs/EVALUATION.md)

## Contents

- [Quick start](#quick-start)
- [Using the graph](#using-the-graph)
- [Examples](#examples)
- [How it works](#how-it-works)
- [Confidence tags](#confidence-tags)
- [Language support](#language-support)
- [Keeping it fresh](#keeping-it-fresh)
- [Evidence](#evidence)
- [Limitations](#limitations)
- [Development](#development)

## Quick start

Requires git and Python 3.10 or newer.

```sh
git clone https://github.com/meabs/AgentGraphWorkTree
cd AgentGraphWorkTree

./install.sh --repo ~/code/my-project          # macOS, Linux, Git Bash
.\install.ps1 --repo C:\code\my-project        # Windows PowerShell
```

Review and commit `.agent-knowledge/` and the instruction files the installer wrote. The installer exits without changing anything if the repository has no tracked Java, Python, TypeScript or JavaScript files. To remove everything it added, run it again with `--uninstall`.

Installing as an APM package is also supported: `apm install meabs/AgentGraphWorkTree --target claude` places the skill, and its `install.py` generates the graph.

### What the installer does

1. Copies the generator to `~/.local/share/agent-graph` (`%LOCALAPPDATA%\agent-graph` on Windows; override with `--home` or `AGENT_GRAPH_HOME`).
2. Creates a private virtual environment there and installs `tree-sitter` with the Java and TypeScript grammars. Nothing is added to your global Python.
3. Runs `kgen sync` on the repository to write `.agent-knowledge/`.
4. Adds a marked block (`<!-- agent-graph:begin -->` … `<!-- agent-graph:end -->`) to `AGENTS.md`, to `CLAUDE.md` as an `@AGENTS.md` import, to `.github/copilot-instructions.md` and to `.cursor/rules/generated-knowledge.mdc`. Re-running refreshes the block and never touches text outside it.
5. Installs git hooks that refresh the graph after a pull, checkout or rebase and on commit.

| Option | Effect |
| --- | --- |
| `--repo PATH` | Repository to process (default: current directory). |
| `--home DIR` | Where to install the generator. |
| `--harness LIST` | Which extra instruction files to write: any of `claude`, `copilot`, `cursor`, or `none`. |
| `--no-agents-md` | Do not write any instruction files. |
| `--labels MODE` | `confidence` (default), `tags` or `instruction`; see [Confidence tags](#confidence-tags). |
| `--no-hooks` | Do not install the git hooks that keep the graph fresh. |
| `--ci check\|sync` | Also write a GitHub Actions workflow; see [Keeping it fresh](#keeping-it-fresh). |
| `--uninstall` | Remove the block, the hooks, a generated workflow and `.agent-knowledge/`. |

## Using the graph

Agents call `kg`, a small command that needs only the Python standard library. On Windows use `kg.cmd`.

```sh
.agent-knowledge/bin/kg show <Type>                    # what a type is, with sources
.agent-knowledge/bin/kg callers <method> [--depth N]    # who calls a method, then their callers
.agent-knowledge/bin/kg dependents <Type>              # who uses, extends or implements it
.agent-knowledge/bin/kg subclasses <Type>              # every subclass at any depth
.agent-knowledge/bin/kg subclasses <Type> --overrides <method>
.agent-knowledge/bin/kg impact <Type>                  # transitive users, routes and tests
.agent-knowledge/bin/kg routes                         # HTTP routes and their handlers
.agent-knowledge/bin/kg tests <Type>                   # tests linked to a type
.agent-knowledge/bin/kg path <from> <to>               # shortest dependency path
.agent-knowledge/bin/kg --min high impact <Type>       # hide [medium] edges
```

Also `find`, `deps`, `entity`, `stats` and `stale`. Import edges list the imported symbols (`[imports AppConfig]`), so a module that shares its name with a type is not ambiguous.

```
$ .agent-knowledge/bin/kg show demo.B
type:demo.B  [high]  src/main/java/demo/B.java:3
  kind_of: class
incoming:
  [high] instantiates method:demo.A#one  src/main/java/demo/A.java:4
methods:
  method two() -> int  src/main/java/demo/B.java:4
```

## Examples

Diagrams drawn from the real `graph.json` of each project (open the [project page](https://meabs.github.io/AgentGraphWorkTree/) for the terminal output next to them).

![Every subclass of BaseKeyValueStoreBackend in Celery](docs/img/hierarchy-python.svg)

![Routes, controllers and the repository in Spring Petclinic](docs/img/routes-java.svg)

![Who calls getI18nPath in Next-js-Boilerplate](docs/img/callers-typescript.svg)

## How it works

```
source files ──► extractors ──► claims ──► graph.json ──► notes and kg
                (tree-sitter,    one fact     nodes and      okf/*.md
                 Python ast)     per line     edges
```

Each extractor emits **claims**: a subject, predicate and object, with a confidence tier, the file and line, and the text of that line. Claims are the single source of truth; the graph and notes are pure functions of them.

```
.agent-knowledge/
  claims.jsonl       one claim per line, with tier, file, line and evidence
  graph.json         nodes and edges built from the claims
  manifest.json      generator version, source commit, hash of every source file used
  bin/kg, kg.cmd     the query command
  okf/               short markdown notes: index, routes, data model, configuration, build, testing, one page per package
  skills/            a skill that tells agents how to use the graph
  AGENTS.snippet.md  the paragraph the installer put into AGENTS.md
```

Pages that would be empty (routes, data model, configuration, build) are not written. In a mixed repository every extractor runs.

## Confidence tags

| Tag | Meaning | Examples |
| --- | --- | --- |
| `[high]` | Written in the source. | A declaration, an annotation, an import, a literal path, a config entry. |
| `[medium]` | Derived by a fixed rule. | A call target resolved from the declared receiver type, a test linked by name. |
| `[low]` | Written by a model. | Part of the format, but the installer never produces it. |

Agents are told to open the cited file before relying on a `[medium]` line, and that the source wins whenever a generated line is wrong. The `confidence` label mode ships both the tags and that instruction; `tags` and `instruction` use only one of the two.

## Language support

| | Java | Python | TypeScript / JavaScript |
| --- | --- | --- | --- |
| **Parser** | tree-sitter | standard-library `ast` | tree-sitter (`.ts .tsx .js .jsx .mjs .cjs .mts .cts`; `.d.ts`, minified files and files over 500 KB are skipped) |
| **Declarations** | classes, interfaces, enums, records, methods, fields | modules, classes, functions, methods, decorators | modules, classes, interfaces, enums, functions and hooks, React components, prototype and object-literal methods, class expressions in static fields |
| **Dependencies** | `extends`, `implements`, constructor and field types | base classes, `import` statements, attribute types | `extends`, `implements`, `import`, `require`, `export … from`, `export *` barrels, `tsconfig` `paths` and `baseUrl` |
| **Call links** (`[medium]`) | resolved from the declared receiver type | resolved from imports, annotations, `self`, constructor results and return annotations | resolved from imports, annotations, `this`, `new` and return types; JSX elements become `renders` edges |
| **Routes** | Spring, Quarkus | Flask, FastAPI | Express and Fastify style, NestJS controllers, Next.js app and pages routers, React Router |
| **Tests** | `@Test` classes | `test_*.py` modules, `Test*` classes | `*.test.*`, `*.spec.*`, `__tests__/` |

Other languages need an extractor that emits the same claim records.

## Keeping it fresh

The graph is a snapshot of one commit, so it has to follow the code. Two layers, both reversible.

**Git hooks (installed by default).** The installer adds `post-merge`, `post-checkout`, `post-rewrite` and `pre-commit` hooks. The graph refreshes after a pull, a branch switch or a rebase, and a commit that touches source files carries its updated graph. Existing hooks are kept, the block is removed by `--uninstall`, and `--no-hooks` skips it. Hooks live in `.git/hooks`, so each teammate gets them by running the installer.

**GitHub Actions (`--ci`).** `--ci check` writes a workflow that fails a pull request when the committed graph is stale. `--ci sync` writes one that regenerates the graph on pushes to `main` and commits it only when the claims changed. Both modes were run end to end on a test repository. The `sync` workflow pushes to `main`, so it needs a branch that allows the bot to push.

You can also run the launcher by hand (`~/.local/share/agent-graph/bin/kgen`, or `kgen.cmd` on Windows):

```sh
kgen sync   --repo .    # regenerate
kgen check  --repo .    # exit 1 and list stale or new files if sources changed since the last sync
kgen verify --repo .    # confirm each claim still matches its cited line
kgen audit  --repo .    # spot-check call edges against the source text: precision, and a lower-bound recall
```

`kg stale` shows agents which files changed since the graph was built.

## Evidence

The full method, data and caveats are in [docs/EVALUATION.md](docs/EVALUATION.md). In brief:

- **Accuracy.** Java call links checked against compiled bytecode: `[high]` 13,380 of 13,380 and `[medium]` 8,947 of 8,957 on shopizer. Python: 87% of call edges whose caller ran were seen at runtime in Click's test suite. TypeScript and JavaScript: 32 of 32 sampled edges correct by hand.
- **Labels prevent misleading notes.** Unlabelled generated notes misled agents in 8 of 24 runs on a drifted repository; with a tag or the trust-the-source instruction it was 1 of 108.
- **Token savings are real for Java and not shown elsewhere.** On a large Java repository (shopizer) Codex used 398k tokens with the graph against 953k without. On Python and TypeScript repositories, with 192 runs across Claude Code and Codex, the graph kept answers correct and was used reliably, but did not reduce tokens overall. A hierarchy command (`kg subclasses`) reduced Codex's tokens by 16% on hierarchy questions and did not help Claude Code.
- **All four agents pick it up.** Claude Code, Codex, Cursor and the Copilot CLI each found and used `kg` from the generated instructions.

## Limitations

- Java, Python, TypeScript and JavaScript only. Vue, Svelte and Angular templates are not read, and `pyproject.toml` is not parsed.
- `[medium]` edges are heuristics. Dynamic dispatch, reflection, callbacks and generated code are not followed, and unannotated Python and plain JavaScript yield fewer call links than Java or TypeScript.
- The experiments are small: two to four runs per cell, two agents with the controlled comparisons. Treat the numbers as signal, not proof.
- VS Code Copilot agent mode was not tested. Windows is covered in CI but was not tried with a live agent.

## Development

```
docs/                                  project page (GitHub Pages), evaluation, diagrams
apm.yml                                APM package manifest
install.sh, install.ps1                entry points
.apm/skills/agent-graph/SKILL.md       agent instructions
.apm/skills/agent-graph/install.py     cross-platform installer
.apm/skills/agent-graph/tool/kgen/     generator: extractors, graph builder, renderer, kg
.apm/skills/agent-graph/tool/tests/    tests
```

```sh
pip install pytest tree-sitter tree-sitter-java tree-sitter-typescript
cd .apm/skills/agent-graph/tool && python -m pytest -q
```

CI runs the tests and installer smoke tests for all three languages on Linux, macOS and Windows.

The experiments that motivated the design live in [meabs/repo-knowledge-gen](https://github.com/meabs/repo-knowledge-gen).

## License

[MIT](LICENSE)
