# Evaluation

What was measured, how, and what it does and does not show. Numbers are quoted from the runs described here; the Java experiments predate this repository and are written up in [meabs/repo-knowledge-gen](https://github.com/meabs/repo-knowledge-gen), where the raw results live. Those Java figures were carried over from notes and not re-derived for this document.

## Summary

| Question | Answer |
| --- | --- |
| Are the graph's facts correct? | Yes for the sampled projects: Java call links match compiled bytecode almost exactly; Python and TypeScript sampled edges are mostly right. |
| Do labels stop agents trusting stale notes? | Yes. Misleading answers fell from 8 of 24 runs to 1 of 108. |
| Do agents use the graph without being told where it is? | Yes, in all four agents tested, once the instruction block lists the commands. |
| Does it reduce tokens? | On a large Java repository, yes (about 2.4×). On Python and TypeScript repositories, not overall; see [Token cost](#token-cost). |

## Accuracy of the graph

**Java.** Call links were compared with compiled bytecode.

| Project | `[high]` claims | `[medium]` claims |
| --- | --- | --- |
| spring-petclinic | 324 of 324 | 233 of 233 |
| shopizer | 13,380 of 13,380 | 8,947 of 8,957 |

**Python.** Click's own tests were run under a call tracer. 378 of the 433 call edges whose caller ran (87%) were seen at runtime; the remainder were real branches the tests do not reach, or dynamic dispatch. In the other direction, the graph contains 60% of the caller-to-callee pairs the run observed, missing mostly callbacks passed as arguments, dunder methods and import-time code. This is a lower bound on precision for one project, not a bytecode-exact check.

**TypeScript and JavaScript.** 32 randomly chosen call, instantiation and JSX edges from Express, zustand, Next-js-Boilerplate and the NestJS starter were checked against the source; all 32 were correct. Recall was not measured at the time (see `kgen audit` below). It is lower for untyped JavaScript, where a method called on a value of unknown type produces no claim (Express yields few call links for this reason).

**`kgen audit`** (0.3.6) repeats this check on any repository, with no compiler or test run. Precision: the share of sampled `calls` edges whose cited line really calls the callee. Recall (a lower bound): the share of textual call sites of uniquely named methods that a `calls` edge covers. Run on the current code:

| Repository | Precision | Recall, all call sites | Recall, bare and `self.`/`this.` calls |
| --- | --- | --- | --- |
| Click | 100% (300 of 300) | 74% (131 of 177) | 98% (100 of 102) |
| Express | 100% (55 of 55) | 18% (36 of 202) | 55% (36 of 66) |

Precision is high in both. Recall is the weak side, and it depends on the language: Click's calls are mostly bare or through `self`, which the extractor resolves; Express passes `req` and `res` around untyped, so a call such as `res.send(...)` in an example file has no receiver type and no edge, and top-level calls such as `defineGetter(req, 'path', ...)` are not recorded. The audit found and led to the fixes for `var f = require('./x').f` bindings and for a second call to the same function inside one caller (edges are kept once per caller and callee, so the audit counts a site as covered when its calling function has an edge). Names shared with the standard library, such as `read` or `update`, can inflate the miss count; the printed examples show which kind each miss is.

## Labels and stale notes

On a repository whose code had drifted from its notes, unlabelled generated notes misled agents in 8 of 24 runs. With any tag, or with the trust-the-source instruction, it was 1 of 108 (pooled after the fact, not pre-planned). Tags and the instruction could not be told apart, so the installer ships both.

## Token cost

### Java

On shopizer, Codex used 398k tokens and 115 s with the graph against 953k and 346 s without, and Cursor opened 4 files instead of 14.5. Accuracy was already at ceiling without the graph. There was no saving on spring-petclinic, so the benefit appears on large repositories. Generated markdown notes with no graph cost the most (2.1M tokens on shopizer).

### Python and TypeScript

Controlled comparisons, Claude Code and Codex, answer keys built independently of the graph (with `ast`, tree-sitter name matching or `grep`). "Plain" is a clean clone; "graph" is a clone with the installer's output.

| Run | Repositories | Questions | Runs | Correct | Tokens per question |
| --- | --- | --- | --- | --- | --- |
| Small repos | Click, Next-js-Boilerplate | 6 lookups | 48 | 48 / 48 | Claude Code 70k graph vs 76k plain; Codex 74k vs 63k |
| Large repos | Celery (443 files), NestJS (2,012 files) | 6 lookups | 48 | 48 / 48 | Claude Code 91k vs 83k; Codex 98k vs 84k |
| Multi-hop | Celery, NestJS | 6 hierarchy and call-chain | 72 | 72 / 72 | Claude Code 84k (0.3.3 instructions) vs 81k plain; Codex 136k vs 107k |
| `kg subclasses` | Celery, NestJS | same 6 | 24 | 24 / 24 | Hierarchy questions: Codex 90k vs 106k plain; Claude Code 82k vs 79k |
| `kg callers --depth` (0.3.5) | Click, Express | 3 | 36 | Codex 5/6 graph vs 4/6 plain, Cursor 6/6 vs 4/6, Copilot 6/6 vs 6/6 | Callers of callers: Codex 84k vs 196k, Cursor 121k vs 414k, Copilot 161k vs 181k. Simple lookups: level or up to 80% dearer with the graph |

Findings:

- With the instruction block, agents ran `kg` in every graph run (24 of 24 and 12 of 12 per arm, falling to 9 of 12 and 11 of 12 after the block gained a longer command list). Without it, none did.
- Accuracy was at ceiling in every arm, so these questions measure cost, not correctness.
- The graph did not reduce tokens overall. Agents run `kg` and then, as the instructions suggest for `[medium]` results, check it with `grep`.
- A "trust `kg`, do not repeat it with `grep`" wording was tried (72-run comparison above) and was worse than the original on Claude Code (102k against 84k), so it was not kept.
- `kg subclasses` cut Codex's tokens by 16% against plain on hierarchy questions (and by 33% against the earlier instructions). It did not help Claude Code. The NestJS hierarchy questions were cheaper with the graph (84k against 114k; 80k against 94k); the Celery ones were level.
- Callers of callers cost more with the graph (164k against 101k) until `kg callers --depth N` existed. With it, the 0.3.5 run (2 runs per cell, Codex, Cursor and Copilot) was cheaper with the graph on that question for all three agents (relative cost 0.43, 0.29 and 0.89, all provisional), while one-hop lookups were level or dearer (for example Express `res.send`: Cursor 95k against 52k). Per-agent verdicts over all three questions were "no clear difference" with 6 runs per arm.
- The multi-level question was then run 8 times per arm for each agent (48 runs, Click, `iter_params_for_processing` callers up to three levels). Codex 95k tokens with the graph against 255k plain (relative cost 0.37, interval 0.33 to 0.42, cheaper; 8/8 correct against 7/8). Cursor 122k against 358k (0.34, 0.26 to 0.43, cheaper; 8/8 both). Copilot 178k against 158k (1.13, 1.00 to 1.26, dearer; 8/8 both): it reads the repository cheaply with its own tools and `kg` added output. One question on one repository, so this shows the graph helps agents that otherwise search file by file, not that it helps every agent or question.
- That run also exposed a miss: Express builds `res` with `Object.create(...)`, which the JavaScript extractor skipped, so `kg callers` on `res.send` returned nothing. Fixed in 0.3.5 and covered by a test. The graph still misses calls through values of unknown type, such as `Group.invoke` reaching `cmd.make_context`.
- With two to four runs per cell and large run-to-run spread, differences under roughly 20% are noise.

### Defects the runs exposed

Each was fixed and has a test.

| Found by | Defect | Fix |
| --- | --- | --- |
| Next-js-Boilerplate | A module and its exported type share a name (`AppConfig`), so `kg dependents` listed every importer of the module. | Import edges list the imported symbols. |
| NestJS | A call inside `static Proxy = class implements … {}` was missed. | Class expressions in static fields are collected. |
| Celery | `kg dependents` returns one level, so listing all subclasses took several calls. | `kg subclasses`, with `--overrides`. |

## Live agent checks

One question per agent, run non-interactively on a fresh clone with the installer's output.

| Agent | Version | Petclinic routes | Click hierarchy | Next-js-Boilerplate |
| --- | --- | --- | --- | --- |
| Claude Code | 2.1.296 | used `kg`, correct | correct; one `kg` call, otherwise `grep` | used `kg`, correct |
| Cursor | cursor-agent 2026.10.01 | used `kg`, correct | used `kg`, correct | used `kg`, correct |
| Codex | codex-cli 0.157.0 | used `kg`, correct | used `kg`, correct | used `kg`, correct |
| GitHub Copilot CLI | 1.0.95 | used `kg`, correct | `kg --help`, then `grep`, correct | `kg --help`, then `grep`, correct |

The Petclinic question was "which HTTP routes does `OwnerController` handle, and which repository methods does it call?" (7 routes and 3 repository methods). This shows the instruction files are picked up by Claude Code (through `CLAUDE.md`), Codex (`AGENTS.md`), Cursor (the rule file) and the Copilot CLI (`.github/copilot-instructions.md`). These are one-run checks and say nothing about cost. Codex needs `< /dev/null` in non-interactive runs, otherwise it waits for stdin. Cursor and Copilot were re-run with the current instruction block in the 0.3.5 trial above: both used `kg` in 12 of 12 graph runs.

## Alternatives tried

A model-built graph (Graphiti) cost $1.50 and 29 minutes for petclinic and found 3 of 16 routes. Another tree-sitter tool (CodeGraphContext) had 48.5% correct call links on shopizer; a third (CodeGraph) scored 83% and saved no tokens.

## Threats to validity

- Small samples, two agents, and a handful of repositories; the Java results come from two projects.
- Answer keys were written by the same people who built the tool. One key was wrong at first (a file that does not import the symbol) and was corrected after the agents contradicted it; every arm was rescored under the corrected key.
- Questions were chosen to have clean, checkable answers, which also makes them ones `grep` can answer. Questions that need reasoning across many files may favour the graph more, and were not tested.
- Token counts include cached input, which differs between agents and runs.
- Models and agent CLIs change; versions are given above.
