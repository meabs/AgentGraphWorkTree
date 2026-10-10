# Agent Graph Work Tree

A code graph and short notes for a Java repository, generated into the work tree so a coding agent can ask "who calls this?" or "what does this change touch?" instead of opening dozens of files.

A script reads the source with tree-sitter (no language model) and writes `.agent-knowledge/`. Every line carries a source file and line number and a tag: `[high]` means it is written in the source (a declaration, annotation, literal path or config entry), `[medium]` means a fixed rule derived it (for example a call target resolved from the declared receiver type). Agents are told to open the cited file before relying on a `[medium]` line, and that the source wins when a generated line is wrong.

Version 0.1.0 · MIT · Windows, macOS and Linux · Java only (Spring, Quarkus, JPA, OpenAPI files, `pom.xml`, SQL, properties, templates).

## Quick start

```sh
git clone https://github.com/meabs/AgentGraphWorkTree
cd AgentGraphWorkTree
./install.sh --repo ~/code/my-java-project          # macOS / Linux / Git Bash
.\install.ps1 --repo C:\code\my-java-project        # Windows PowerShell
```

Then review and commit `.agent-knowledge/` and the instruction files the installer wrote. Needs git and Python 3.10 or newer. The installer exits without changing anything if the repository has no tracked `.java` files.

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
- **Call links are accurate.** Checked against compiled bytecode, `[high]` claims matched 324 of 324 (petclinic) and 13,380 of 13,380 (shopizer); `[medium]` matched 233 of 233 and 8,947 of 8,957.
- **Notes without the graph cost more.** Generated markdown with no graph used 2.1M tokens on shopizer, the most of any setup.
- **Alternatives tried.** A model-built graph (Graphiti) cost $1.50 and 29 minutes for petclinic and found 3 of 16 routes. Another tree-sitter tool (CodeGraphContext) had 48.5% correct call links on shopizer; a third (CodeGraph) scored 83% and saved no tokens.

## Limits

- Java only. Other languages need a new extractor that emits the same claim records.
- Two main projects, small samples, two agents. Treat the numbers as signal, not proof.
- `[medium]` edges are heuristics. Dynamic dispatch, reflection and generated code are not followed.
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
