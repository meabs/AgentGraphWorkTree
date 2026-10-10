# Agent Graph Work Tree

A code graph and short notes for a Java or Python repository, generated into the work tree so a coding agent can ask "who calls this?" or "what does this change touch?" instead of opening dozens of files.

A script reads the source with tree-sitter (no language model) and writes `.agent-knowledge/`. Every line carries a source file and line number and a tag: `[high]` means it is written in the source (a declaration, annotation, literal path or config entry), `[medium]` means a fixed rule derived it (for example a call target resolved from the declared receiver type). Agents are told to open the cited file before relying on a `[medium]` line, and that the source wins when a generated line is wrong.

Version 0.2.0 · MIT · Windows, macOS and Linux · Java (Spring, Quarkus, JPA, OpenAPI files, `pom.xml`, SQL, properties, templates) and Python.

## Quick start

```sh
git clone https://github.com/meabs/AgentGraphWorkTree
cd AgentGraphWorkTree
./install.sh --repo ~/code/my-java-project          # macOS / Linux / Git Bash
.\install.ps1 --repo C:\code\my-java-project        # Windows PowerShell
```

Then review and commit `.agent-knowledge/` and the instruction files the installer wrote. Needs git and Python 3.10 or newer. The installer exits without changing anything if the repository has no tracked `.java` or `.py` files.

As an APM package: `apm install meabs/AgentGraphWorkTree --target claude` places the skill; run its `install.py` to generate the graph (APM copies files and cannot run it for you).

## What the installer does

| Step | What happens |
| --- | --- |
| 1. Copy | Copies the generator to `~/.local/share/agent-graph` (`%LOCALAPPDATA%\agent-graph` on Windows; override with `--home` or `AGENT_GRAPH_HOME`). |
| 2. Environment | Creates a private virtual environment there and installs `tree-sitter` and `tree-sitter-java`. Nothing goes into your global Python. |
| 3. Generate | Runs `kgen sync` on the repository and writes `.agent-knowledge/`. |
| 4. Instructions | Adds a marked block (`<!-- agent-graph:begin -->` ... `end`) to `AGENTS.md` (Codex and others), `CLAUDE.md` as an `@AGENTS.md` import (Claude Code), `.github/copilot-instructions.md` (Copilot) and `.cursor/rules/generated-knowledge.mdc` (Cursor). Re-running refreshes the block and never touches text outside it. `--no-agents-md` skips this step; `--harness` picks which extra files. |

`--labels confidence` (default) tags every fact and tells the agent that the source wins. `tags` and `instruction` use only one of the two. The default is the variant the experiments tested.

## What gets generated

```
.agent-knowledge/
  claims.jsonl       one claim per line: subject, predicate, object, tier, file, line, evidence
  graph.json         nodes and edges built from the claims
  manifest.json      generator version, source commit, hash of every source file used
  bin/kg, kg.cmd     query command (Python standard library only)
  okf/               short markdown notes: index, routes, data model, configuration, build, testing, one page per package
  skills/            a skill that tells agents how to use the graph
  AGENTS.snippet.md  the paragraph the installer put into AGENTS.md
```

Query it:

```sh
.agent-knowledge/bin/kg show <Type>         # what it is, with sources
.agent-knowledge/bin/kg impact <Type>       # what depends on it, for scoping a change
.agent-knowledge/bin/kg callers <method>
.agent-knowledge/bin/kg routes
.agent-knowledge/bin/kg tests <Type>
.agent-knowledge/bin/kg path <from> <to>
.agent-knowledge/bin/kg --min high impact <Type>   # hide [medium] edges
```

Also `find`, `deps`, `dependents`, `entity`, `stats`. On Windows use `kg.cmd`.

Sample output, from a two-class demo:

```
$ .agent-knowledge/bin/kg show demo.B
type:demo.B  [high]  src/main/java/demo/B.java:3
  kind_of: class
incoming:
  [high] instantiates method:demo.A#one  src/main/java/demo/A.java:4
methods:
  method two() -> int  src/main/java/demo/B.java:4
```

## Languages

| | Java | Python |
| --- | --- | --- |
| Parser | tree-sitter | the standard-library `ast` module (nothing extra to install) |
| Types and methods | classes, interfaces, enums, records, methods, fields | modules (their functions are methods), classes, methods, decorators |
| Inheritance and dependencies | `extends`, `implements`, constructor and field types | base classes, `import` statements (module to module), attribute types from annotations or constructor calls |
| Call links (`[medium]`) | resolved from the declared receiver type | resolved from imports, annotations, `self`, constructor results and return annotations; anything else produces no claim |
| Frameworks | Spring and Quarkus routes, JPA entities, Spring Data, OpenAPI, `pom.xml`, SQL, properties | Flask and FastAPI routes (`@app.get`, `@app.route(..., methods=[...])`; router and blueprint paths are `[medium]` because the mount prefix is not applied) |
| Tests | `@Test` classes linked by annotation or name | `test_*.py` modules and `Test*` classes linked by name |

In a mixed repository both extractors run. Pages that would be empty (routes, data model, configuration, build) are not written. TypeScript, Go and others need another extractor that emits the same claim records.

## Keeping it fresh

The graph is a snapshot. Commands (run with the installed launcher, `~/.local/share/agent-graph/bin/kgen`, or `kgen.cmd` on Windows):

```sh
kgen sync   --repo .     # regenerate
kgen check  --repo .     # exit 1 and list stale or new files if sources changed since the last sync
kgen verify --repo .     # confirm each claim still matches its cited line
```

`check` is cheap enough for CI: install the dependencies, fetch this repository, and run `python AgentGraphWorkTree/.apm/skills/agent-graph/tool/kgen.py check --repo .` (this CI step has not been exercised; the commands themselves are tested). Regenerating on every merge is what the experiments assumed.

## What the experiments found

Measured on Codex and Cursor over two Java projects (spring-petclinic and shopizer). Full write-up and raw results: https://github.com/meabs/repo-knowledge-gen.

- **Tags matter.** On a repository whose code had drifted from the notes, unlabelled generated notes misled agents in 8 of 24 runs. With any tag or the trust-the-source instruction it was 1 of 108 (pooled afterwards, not pre-planned). Tags and instruction could not be told apart, so the installer ships both.
- **The graph saves effort on a large repository, not a small one.** On shopizer, Codex used 398k tokens and 115 s with the graph against 953k and 346 s with none, and Cursor opened 4 files instead of 14.5. Accuracy was already at ceiling without it, and there was no saving on spring-petclinic.
- **Call links are accurate (Java).** Checked against compiled bytecode, `[high]` claims matched 324 of 324 (petclinic) and 13,380 of 13,380 (shopizer); `[medium]` matched 233 of 233 and 8,947 of 8,957.
- **Python call links, checked against a test run.** Click's own tests were run under a call tracer, and 378 of the 433 call edges whose caller ran (87%) were seen at runtime; sampled edges that were not seen were real branches the tests do not reach, or dynamic dispatch. Going the other way, the graph contains 60% of the caller-to-callee pairs the run observed; the misses are mostly callbacks passed as arguments, dunder methods and import-time code. This is a lower bound on precision, not a bytecode-exact check, on one project. No agent token or accuracy experiment has been run on Python.
- **Notes without the graph cost more.** Generated markdown with no graph used 2.1M tokens on shopizer, the most of any setup.
- **Alternatives tried.** A model-built graph (Graphiti) cost $1.50 and 29 minutes for petclinic and found 3 of 16 routes. Another tree-sitter tool (CodeGraphContext) had 48.5% correct call links on shopizer; a third (CodeGraph) scored 83% and saved no tokens.

## Checked with live agents

One question ("which HTTP routes does OwnerController handle, and which repository methods does it call?") on a fresh clone of spring-petclinic with the installer's output, asked once to each agent in non-interactive mode (claude 2.1.296 (Claude Code); cursor-agent 2026.10.01-14929f9; codex codex-cli 0.157.0; copilot GitHub Copilot CLI 1.0.95.). All four found and used `.agent-knowledge/bin/kg` without being told where it was, and all four gave the same correct answer: 7 routes and 3 `OwnerRepository` methods (`findById`, `save`, `findByLastNameStartingWith`). Each also opened the source file or checked the cited lines. This shows the instruction files are picked up by Claude Code (through `CLAUDE.md`), Codex (`AGENTS.md`), Cursor (the rule file) and the Copilot CLI (`.github/copilot-instructions.md`). It is one run each and says nothing about token savings; those figures come from the experiments above. VS Code Copilot agent mode was not tried.

## Limits

- Java and Python only. Other languages need a new extractor that emits the same claim records.
- Two main projects, small samples, two agents. Treat the numbers as signal, not proof.
- `[medium]` edges are heuristics. Dynamic dispatch, reflection, callbacks and generated code are not followed. Python has no static types to lean on, so unannotated code yields fewer call links than Java.
- Not tested on Windows with a live agent; the generator, installer and `kg` run on Windows, macOS and Linux in CI.
- The `low` tier (claims written by a model) exists in the format but the installer never produces it.

## Layout

```
apm.yml                                APM package manifest
install.sh / install.ps1               entry points
.apm/skills/agent-graph/SKILL.md       agent instructions
.apm/skills/agent-graph/install.py     cross-platform installer
.apm/skills/agent-graph/tool/          kgen (generator) and its tests
```

Tests: `pip install pytest tree-sitter tree-sitter-java && cd .apm/skills/agent-graph/tool && python -m pytest -q`.

## License

MIT. See `LICENSE`.
