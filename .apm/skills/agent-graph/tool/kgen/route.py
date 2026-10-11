"""Turn AgentTrialRig results into per-agent advice on when to use `kg` and when to search the text.

The trial compared a plain arm with a graph arm. Questions are grouped by the kind of lookup they ask for; for each
agent and kind the advice is "use kg" when the graph arm's tokens were clearly lower, "prefer search" when they were
clearly higher, and "either" otherwise, so the advice is only as strong as the runs behind it.
"""
from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path
from statistics import mean

KINDS = (
    ("transitive callers", ("levels", "transitive", "through up to", "callers of callers", "indirectly", "chain"), "kg callers <method> --depth 3"),
    ("class hierarchy", ("inherit", "subclass", "extends", "implements", "hierarchy"), "kg subclasses <Class>"),
    ("direct callers", ("call ", "calls ", "callers", "invoke", "uses "), "kg callers <method>"),
    ("http routes", ("route", "endpoint", "handler"), "kg routes"),
    ("change impact", ("break", "impact", "affected"), "kg impact <name>"),
    ("imports and dependencies", ("import", "depend"), "kg deps / kg dependents <name>"),
)


def kind_of(text: str) -> tuple[str, str]:
    t = text.lower()
    for name, words, command in KINDS:
        if any(w in t for w in words):
            return name, command
    return "other", "kg find <name>"


def load(results: Path, trial: Path | None, plain: str, graph: str) -> list[dict]:
    prompts = {}
    if trial:
        import tomllib

        prompts = {q["id"]: q.get("prompt", "") for q in tomllib.loads(trial.read_text(encoding="utf-8")).get("questions", [])}
    runs = []
    for f in sorted(results.glob("runs/*/*/*/*/run.json")):
        r = json.loads(f.read_text(encoding="utf-8"))
        tok = (r.get("tokens") or {}).get("total")
        if r.get("status") == "ok" and tok and r["arm"] in (plain, graph):
            kind, command = kind_of(prompts.get(r["question"], "") + " " + r["question"].replace("_", " "))
            runs.append({"agent": r["agent"], "question": r["question"], "arm": r["arm"], "tokens": tok, "kind": kind, "command": command, "correct": r.get("correct")})
    return runs


def ratio_interval(graph: list[float], plain: list[float], seed: int = 7, n: int = 2000) -> tuple[float, float, float]:
    rng = random.Random(seed)
    vals = []
    for _ in range(n):
        p = mean(rng.choices(plain, k=len(plain)))
        vals.append(mean(rng.choices(graph, k=len(graph))) / p)
    vals.sort()
    return mean(graph) / mean(plain), vals[int(0.025 * n)], vals[int(0.975 * n) - 1]


def advise(runs: list[dict], plain: str = "plain", graph: str = "graph", min_runs: int = 4) -> list[dict]:
    cells = defaultdict(lambda: {"plain": [], "graph": []})
    for r in runs:
        cells[(r["agent"], r["kind"], r["command"])]["graph" if r["arm"] == graph else "plain"].append(r["tokens"])
    out = []
    for (agent, kind, command), v in sorted(cells.items()):
        g, p = v["graph"], v["plain"]
        row = {"agent": agent, "kind": kind, "command": command, "plain_runs": len(p), "graph_runs": len(g)}
        if min(len(g), len(p)) < min_runs:
            row.update(verdict="not enough runs", ratio=None)
        else:
            ratio, lo, hi = ratio_interval(g, p)
            verdict = "use kg" if hi < 1 else "prefer search" if lo > 1 else "either"
            row.update(verdict=verdict, ratio=ratio, lo=lo, hi=hi)
        out.append(row)
    return out


def markdown(rows: list[dict]) -> str:
    lines = ["# When to use kg (measured)", "", "Token cost of the graph arm against plain search, from repeated trials. `use kg` means the interval was below 1.00; `prefer search` above; `either` spans it.", ""]
    for agent in sorted({r["agent"] for r in rows}):
        lines += [f"## {agent}", "", "| question kind | command | graph vs plain (95% CI) | runs per arm | advice |", "| --- | --- | --- | --- | --- |"]
        for r in (x for x in rows if x["agent"] == agent):
            rel = f"{r['ratio']:.2f} ({r['lo']:.2f} to {r['hi']:.2f})" if r["ratio"] is not None else "-"
            lines.append(f"| {r['kind']} | `{r['command']}` | {rel} | {min(r['plain_runs'], r['graph_runs'])} | {r['verdict']} |")
        lines.append("")
    return "\n".join(lines)


def instruction(rows: list[dict], agent: str) -> str:
    use = [r for r in rows if r["agent"] == agent and r["verdict"] == "use kg"]
    avoid = [r for r in rows if r["agent"] == agent and r["verdict"] == "prefer search"]
    if not use and not avoid:
        return ""
    parts = []
    if use:
        parts.append("Use `kg` for: " + "; ".join(f"{r['kind']} (`{r['command']}`)" for r in use) + ".")
    if avoid:
        parts.append("Search the text directly for: " + ", ".join(r["kind"] for r in avoid) + ".")
    return " ".join(parts)
