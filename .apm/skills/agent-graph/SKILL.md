---
name: agent-graph
description: Use the generated code graph and notes in .agent-knowledge/ to answer structure questions in a Java, Python, TypeScript or JavaScript repository (who calls what, what a change touches, which routes exist, which components render which) without reading many files, or install the generator when the user asks.
---

# Agent graph

`.agent-knowledge/` is generated from the source by a script, not written by people. Every line has a source file and line and a tag: `[high]` is written in the source, `[medium]` is a deterministic inference.

## Using it (repos that have `.agent-knowledge/`)

```sh
.agent-knowledge/bin/kg show <Type>        # what a type is, with sources
.agent-knowledge/bin/kg impact <Type>      # what depends on it, for scoping a change
.agent-knowledge/bin/kg callers <method>   # who calls a method
.agent-knowledge/bin/kg routes             # HTTP routes and their handlers
.agent-knowledge/bin/kg tests <Type>       # tests linked to a type
```

On Windows use `.agent-knowledge\bin\kg.cmd`. Run `kg --help` for the rest.

Rules: open the cited file before relying on a `[medium]` line. The source wins over any generated line; if one is wrong, say so in your final message. If sources changed since generation, say so rather than trusting stale lines.

## Installing (only when the user asks)

```sh
python <this-skill-dir>/install.py --repo <project>
# macOS/Linux also: sh install.sh ...   Windows PowerShell also: .\install.ps1 ...
```

This copies the generator to `~/.local/share/agent-graph` (`%LOCALAPPDATA%\agent-graph` on Windows) with its own virtual environment, writes `.agent-knowledge/`, and adds a marked block to `AGENTS.md`, `CLAUDE.md` (an `@AGENTS.md` import), `.github/copilot-instructions.md` and a Cursor rule, for the user to review and commit. Needs git and Python 3.10+. Java, Python, TypeScript and JavaScript only: it exits without changing anything when the repository has no tracked files in those languages. Undo with `--uninstall`.
