"""Emit an Open Knowledge Format (v0.2) bundle from kgen claims, with switches for what goes in it.

The bundle is a pure function of claims.jsonl and manifest.json, so it is as deterministic as the claims.
Every switch exists so an experiment can turn one thing off at a time.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import VERSION

OKF_VERSION = "0.2"


@dataclass(frozen=True)
class Config:
    unit: str = "module"        # module: one concept per source module; package: one per package
    meta: str = "native"        # minimal: type/title/description only; native: adds resource, tags, generated, sources, status
    line_tags: bool = False     # append `[high]`/`[medium]` to each generated line (how the claim was derived)
    banner: bool = False        # a sentence at the top of each concept telling the reader to check the source
    stale_after_days: int = 0   # >0: add stale_after = source commit time + N days
    links: bool = True          # markdown links between concepts (calls, dependencies, tests)
    calls: str = "cross"        # none | cross (calls into other modules) | all (also calls within the module)
    index: str = "routing"      # none | flat (names only) | routing (name plus what is in it)
    cite: bool = True           # `path:line` after each line
    attest: bool = False        # add `verified` with a process actor for concepts whose claims are all high confidence
    max_items: int = 400        # cap on types, functions and edges listed per concept
    source_url: str = ""        # template with {path}, e.g. https://github.com/o/r/blob/<commit>/{path}; empty gives a path relative to a bundle at <repo>/okf/
    file_times: tuple = ()      # ((path, ISO 8601 time the file last changed), ...) for sources[].last_modified; falls back to the commit time
    stale_files: tuple = ()     # source paths known to have changed since generation; their concepts get a past stale_after
    references: tuple = field(default_factory=tuple)  # extra bundles to mirror under references/: ((name, path), ...)


POINTER = """# Repository knowledge

`okf/` is an Open Knowledge Format bundle generated from this repository's source: one markdown file per module with its types, functions, call edges and the file and line each came from. Start at `okf/index.md`. Use it to find your way around, then read the source to confirm before you rely on it.
"""
POINTER_STALE = "\nA concept whose `stale_after` time has passed describes source that has changed since it was generated; re-check it against the source.\n"
POINTER_CONFIDENCE = "\nEntries marked `[medium]` are heuristic guesses, not facts read from the code. When your answer relies on one, say so.\n"
POINTER_REFERENCES = "\n`okf/references/` holds bundles of other repositories this one depends on, copied in as they were when generated.\n"


def pointer(cfg: "Config") -> str:
    """The AGENTS.md text that goes with a bundle. The first paragraph's last sentence is the part that measurably stops stale claims being followed."""
    return POINTER + (POINTER_STALE if cfg.stale_files or cfg.stale_after_days else "") + (POINTER_CONFIDENCE if cfg.line_tags else "") + (POINTER_REFERENCES if cfg.references else "")


PRESETS = {
    "minimal": Config(meta="minimal", links=False, calls="none", index="none", cite=False),
    "lean": Config(calls="none", cite=True),
    "default": Config(),
    "recommended": Config(),  # what the experiments support: module concepts, native fields, links, citations, cross-module calls, routing index
    "tagged": Config(line_tags=True, attest=True),
    "guarded": Config(line_tags=True, banner=True, attest=True, stale_after_days=30),
}


def load(repo: Path):
    root = repo / ".agent-knowledge"
    claims = [json.loads(line) for line in (root / "claims.jsonl").read_text(encoding="utf-8").splitlines() if line]
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    return claims, manifest


def q(v) -> str:
    return json.dumps(v, ensure_ascii=False)


ACCESSOR = re.compile(r"^(get|set|is)[A-Z]")  # Java accessors
RESERVED = {"index", "log"}  # index.md and log.md mean something at any level (SPEC section 3.1), so a module of that name cannot be a concept file


def mod_path(name: str) -> str:
    stem = name.replace("/", ".")
    return "/modules/" + (stem + "-module" if stem.lower() in RESERVED else stem) + ".md"


def src_ref(path: str, cfg: "Config") -> str:
    """A path-valued field (SPEC section 6.2): an absolute URL, or else a relative path from modules/ to the source.
    A relative path means nothing once the bundle is mirrored into another repository, so give a bundle that will be mirrored a --source-url."""
    return cfg.source_url.format(path=path) if cfg.source_url else "../../" + path


def short(fqn: str) -> str:
    return fqn.split(":", 1)[-1]


def tag(c: dict, cfg: Config) -> str:
    return f" `[{c['confidence']}]`" if cfg.line_tags else ""


def cite(c: dict, cfg: Config) -> str:
    return f" `{c['source']['file']}:{c['source']['line']}`" if cfg.cite else ""


def render(claims: list[dict], manifest: dict, cfg: Config = Config()) -> dict[str, str]:
    by = defaultdict(list)
    for c in claims:
        by[c["predicate"]].append(c)
    kinds = {c["subject"]: c for c in by["is_a"]}
    own = {t: c for t, c in kinds.items() if c["object"] == "module"}  # one concept per module
    java = not own  # no modules (Java): every type is its own file-level unit and concepts are packages
    if java:
        own = dict(kinds)
        cfg = replace(cfg, unit="package")
    file_of = {t: c["source"]["file"] for t, c in own.items()}
    is_test = lambda f: "/tests/" in "/" + f or "/test/" in "/" + f or f.rsplit("/", 1)[-1].startswith("test_") or ".test." in f or ".spec." in f or f.endswith(("Test.java", "Tests.java", "IT.java"))
    module_of: dict[str, str] = {}   # any symbol id -> module type id
    for t, c in kinds.items():
        f = c["source"]["file"]
        if t in own and (java or c["object"] == "module"):
            module_of[t] = t
            continue
        for m, mc in own.items():
            if file_of[m] == f:
                module_of[t] = m
                break
    for c in by["has_method"]:
        module_of.setdefault(c["object"], module_of.get(c["subject"], c["subject"]))
    commit_time = manifest.get("source_commit_time") or ""
    commit = (manifest.get("source_commit") or "unknown")[:12]
    gen_by = f"kgen/{VERSION}"
    stale = ""
    if cfg.stale_after_days and commit_time:
        try:
            stale = (datetime.fromisoformat(commit_time) + timedelta(days=cfg.stale_after_days)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            stale = ""
    units: dict[str, list[str]] = defaultdict(list)   # unit name -> module ids
    for t in sorted(own):
        f = file_of[t]
        if is_test(f):
            continue
        name = (kinds[t]["attrs"].get("package") or t) if cfg.unit == "package" else short(t)
        units[name].append(t)
    unit_of = {t: n for n, ts in units.items() for t in ts}
    link = lambda t: f"[{short(t)}]({mod_path(unit_of[t])})" if cfg.links and t in unit_of else f"`{short(t)}`"
    tests_of = defaultdict(list)
    for c in by["tests"]:
        tests_of[c["object"]].append(c["subject"])
    out: dict[str, str] = {}
    summaries: dict[str, str] = {}
    for name, mods in sorted(units.items()):
        used: list[dict] = []
        body: list[str] = []
        mset = set(mods)
        files = sorted({file_of[t] for t in mods})
        types = [kinds[t] for t in kinds if module_of.get(t) in mset and kinds[t]["object"] not in ("module",)]
        types.sort(key=lambda c: (c["source"]["file"], c["source"]["line"]))
        funcs = [c for c in by["has_method"] if module_of.get(c["subject"], c["subject"]) in mset]
        funcs.sort(key=lambda c: (c["source"]["file"], c["source"]["line"]))
        routes = [c for c in by["handled_by"] if module_of.get(c["object"].split("#")[0].replace("method:", "type:")) in mset or module_of.get(c["object"].split("#")[0]) in mset]
        body.append(f"# {name}")
        body.append("")
        body.append(f"Source: {', '.join(f'`{f}`' for f in files)}")
        if types:
            body += ["", "## Types"]
            for c in types[: cfg.max_items]:
                ext = [x for x in by["extends"] if x["subject"] == c["subject"]]
                base = ", ".join(link(x["object"]) if x["object"] in unit_of else f"`{short(x['object'])}`" for x in ext)
                body.append(f"- `{short(c['subject']).rsplit('.', 1)[-1]}` ({c['object']}){' extends ' + base if base else ''}{cite(c, cfg)}{tag(c, cfg)}")
                used += [c] + ext
        fields = [c for c in by["has_field"] if module_of.get(c["subject"], c["subject"]) in mset and c["attrs"].get("name") != "serialVersionUID"]
        if fields:
            body += ["", "## Fields"]
            for c in sorted(fields, key=lambda c: (c["source"]["file"], c["source"]["line"]))[: cfg.max_items]:
                body.append(f"- `{short(c['subject']).rsplit('.', 1)[-1]}.{c['attrs']['name']}: {c['attrs']['type']}`{cite(c, cfg)}{tag(c, cfg)}")
                used.append(c)
        if funcs:
            body += ["", "## Functions and methods"]
            for c in funcs[: cfg.max_items]:
                a = c["attrs"]
                params = ", ".join(a.get("params") or [])
                ret = f" -> {a['returns']}" if a.get("returns") else ""
                owner = "" if (c["subject"] in mset and not java) else short(c["subject"]).rsplit(".", 1)[-1] + "."
                body.append(f"- `{owner}{a.get('name') or short(c['object']).split('#')[-1]}({params}){ret}`{cite(c, cfg)}{tag(c, cfg)}")
                used.append(c)
        if routes:
            body += ["", "## HTTP routes"]
            for c in sorted(routes, key=lambda c: (c["attrs"]["path"], c["attrs"]["http"])):
                body.append(f"- `{c['attrs']['http']} {c['attrs']['path']}` handled by `{short(c['object'])}`{cite(c, cfg)}{tag(c, cfg)}")
                used.append(c)
        deps = sorted({(c["object"], c["source"]["line"]): c for c in by["depends_on"] if c["subject"] in mset and c["object"] in unit_of and unit_of[c["object"]] != name}.values(), key=lambda c: c["object"])
        if deps:
            body += ["", "## Depends on"]
            for c in deps[: cfg.max_items]:
                body.append(f"- {link(c['object'])}{cite(c, cfg)}{tag(c, cfg)}")
                used.append(c)
        if cfg.calls != "none":
            seen, calls = set(), []
            for c in by["calls"]:
                a, b = c["subject"].split("#")[0].replace("method:", "type:"), c["object"].split("#")[0].replace("method:", "type:")
                ma, mb = module_of.get(a, a), module_of.get(b, b)
                if ma in mset and mb in unit_of and (cfg.calls == "all" or unit_of[mb] != name):
                    key = (c["subject"], c["object"])
                    if key not in seen:
                        seen.add(key)
                        calls.append(c)
            if java:
                calls = [c for c in calls if not ACCESSOR.match(c["object"].split("#")[-1])]  # getters and setters are noise at this scale
            if calls:
                body += ["", "## Calls"]
                rows = []
                for c in sorted(calls, key=lambda c: (c["subject"], c["object"])):
                    callee = c["object"].split("#")
                    mb = module_of.get(callee[0].replace("method:", "type:"), callee[0].replace("method:", "type:"))
                    who = c["subject"].replace("method:", "").split("#")
                    caller = (who[0].rsplit(".", 1)[-1] + "." if java or who[0] not in {short(t) for t in mset} else "") + who[-1]
                    owner_ = callee[0].replace("method:", "")
                    target = (owner_.rsplit(".", 1)[-1] + "." if java or owner_ not in {short(t) for t in module_of if module_of[t] == mb and kinds.get(t, {}).get("object") == "module"} and owner_ not in {short(t) for t in mset} else "") + callee[-1]
                    rows.append((caller, mb, target, c))
                if java:  # one line per caller and target concept: the callees are listed together, cited at the first call
                    grouped: dict = {}
                    for caller, mb, target, c in rows:
                        grouped.setdefault((caller, mb), []).append((target, c))
                    for (caller, mb), items in list(grouped.items())[: cfg.max_items]:
                        names = ", ".join(f"`{t}`" for t in dict.fromkeys(t for t, _ in items))
                        where = f"{link(mb)} " if unit_of[mb] != name else ""
                        body.append(f"- `{caller}` calls {where}{names}{cite(items[0][1], cfg)}{tag(items[0][1], cfg)}")
                        used += [c for _, c in items]
                else:
                    for caller, mb, target, c in rows[: cfg.max_items]:
                        where = f"{link(mb)} " if unit_of[mb] != name else ""
                        body.append(f"- `{caller}` calls {where}`{target}`{cite(c, cfg)}{tag(c, cfg)}")
                        used.append(c)
        tests = sorted({short(s) for t in mset for s in tests_of.get(t, [])})
        if tests:
            body += ["", "## Tests", *[f"- `{t}`" for t in tests]]
        top = [short(c["subject"]).rsplit(".", 1)[-1] for c in types if c["object"] == "class"][:4] or [c["attrs"].get("name", "") for c in funcs[:4]]
        desc = f"{name}: " + (", ".join(x for x in top if x) or "module") + (f"; {len(routes)} route(s)" if routes else "")
        summaries[name] = desc
        fm = ["---", "type: Code Module", f"title: {q(name)}", f"description: {q(desc)}"]
        if cfg.meta == "native":
            fm += [f"resource: {q(src_ref(files[0], cfg))}", f"tags: {q(['generated', 'code'])}"]
            fm += ["generated:", f"  by: {q(gen_by)}", f"  at: {q(commit_time or '1970-01-01T00:00:00Z')}"]
            if cfg.attest and all(c["confidence"] == "high" for c in used) and used:
                fm += ["verified:", f"  - by: {q('process:kgen-extract')}", f"    at: {q(commit_time or '1970-01-01T00:00:00Z')}"]
            fm.append("sources:")
            for f in files:
                fm += [f"  - resource: {q(src_ref(f, cfg))}", f"    sha256: {q(manifest.get('sources', {}).get(f, ''))}", f"    last_modified: {q(dict(cfg.file_times).get(f) or commit_time or '1970-01-01T00:00:00Z')}"]
            fm.append("status: stable")
            if stale:
                fm.append(f"stale_after: {q(stale)}")
            elif set(files) & set(cfg.stale_files):
                fm.append(f"stale_after: {q(commit_time)}")
        fm.append("---")
        banner = (f"> Auto-generated by {gen_by} from commit `{commit}`; not reviewed by a person. Check the cited source before relying on a line or before editing.\n\n" if cfg.banner else "")
        if cfg.line_tags:
            banner += "> `[high]` is stated directly in the source; `[medium]` is a deterministic inference.\n\n"
        out[mod_path(name).lstrip("/")] = "\n".join(fm) + "\n\n" + banner + "\n".join(body).rstrip() + "\n"
    routes = sorted(by["handled_by"], key=lambda c: (c["attrs"]["path"], c["attrs"]["http"]))
    routes = [c for c in routes if not is_test(c["source"]["file"])]
    if routes:  # the HTTP surface as one concept: what another repository needs to call this one
        body = ["# HTTP routes", "", f"{len(routes)} routes. Each line is `METHOD path`, the handler method, and where it is declared.", "", "| Method | Path | Handler | Source |", "|---|---|---|---|"]
        used = []
        for c in routes:
            t = c["object"].split("#")[0].replace("method:", "type:")
            handler = f"{short(t).rsplit('.', 1)[-1]}.{c['object'].split('#')[-1]}"
            mb = module_of.get(t, t)
            target = link(mb) if mb in unit_of else f"`{handler}`"
            body.append(f"| {c['attrs']['http']} | `{c['attrs']['path']}` | {target} `{handler}` | `{c['source']['file']}:{c['source']['line']}`{tag(c, cfg)} |")
            used.append(c)
        files = sorted({c["source"]["file"] for c in routes})
        desc = f"All {len(routes)} HTTP routes with their handlers"
        fm = ["---", "type: HTTP API", 'title: "HTTP routes"', f"description: {q(desc)}"]
        if cfg.meta == "native":
            fm += [f"tags: {q(['generated', 'api'])}", "generated:", f"  by: {q(gen_by)}", f"  at: {q(commit_time or '1970-01-01T00:00:00Z')}", "sources:"]
            for f in files:
                fm += [f"  - resource: {q(src_ref(f, cfg))}", f"    sha256: {q(manifest.get('sources', {}).get(f, ''))}", f"    last_modified: {q(dict(cfg.file_times).get(f) or commit_time or '1970-01-01T00:00:00Z')}"]
            fm.append("status: stable")
            if stale:
                fm.append(f"stale_after: {q(stale)}")
            elif set(files) & set(cfg.stale_files):
                fm.append(f"stale_after: {q(commit_time)}")
        fm.append("---")
        out["routes.md"] = "\n".join(fm) + "\n\n" + "\n".join(body).rstrip() + "\n"
    for ref, path in cfg.references:
        for p in sorted(Path(path).rglob("*.md")):
            rel = p.relative_to(path).as_posix()
            if rel == "log.md":
                continue
            text = p.read_text(encoding="utf-8")
            if rel == "index.md":
                text = re.sub(r"\A---\n.*?\n---\n\n?", "", text, count=1, flags=re.S)  # okf_version belongs to the root index only
            text = text.replace("](/", f"](/references/{ref}/")  # bundle-relative links must stay inside the mirrored bundle
            out[f"references/{ref}/{rel}"] = text
    if cfg.index != "none":
        fm = ["---", f'okf_version: "{OKF_VERSION}"', "---", ""]
        lines = [f"# {manifest.get('name', 'Repository')} knowledge", ""]
        if cfg.index == "routing":
            lines += ["Start here. Open the module that matches your task; each lists its types, functions, routes and what it depends on.", ""]
        if "routes.md" in out:
            lines += ["## API", f"- [HTTP routes](/routes.md)" + (" - every route with its handler" if cfg.index == "routing" else ""), "", "## Modules" if not java else "## Packages"]
        for name in sorted(units):
            lines.append(f"- [{name}]({mod_path(name)})" + (f" - {summaries[name].split(': ', 1)[1]}" if cfg.index == "routing" else ""))
        if cfg.references:
            lines += ["", "## Other repositories"]
            for ref, _ in cfg.references:
                lines.append(f"- [{ref}](/references/{ref}/index.md)")
        out["index.md"] = "\n".join(fm + lines) + "\n"
    return out


def remote_url_template(repo: Path, commit: str) -> str:
    """https://github.com/<owner>/<repo>/blob/<commit>/{path} from the repo's origin, or '' when there is none."""
    r = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"], text=True, capture_output=True)
    m = re.match(r"^(?:https?://|git@|ssh://git@)([^/:]+)[/:](.+?)(?:\.git)?/?$", r.stdout.strip()) if r.returncode == 0 else None
    return f"https://{m.group(1)}/{m.group(2)}/blob/{commit}/{{path}}" if m and commit else ""


def write(repo: Path, dest: Path, cfg: Config) -> int:
    claims, manifest = load(repo)
    if not cfg.file_times:
        times = []
        for path in sorted(manifest.get("sources", {})):
            r = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%cI", "--", path], text=True, capture_output=True)
            if r.returncode == 0 and r.stdout.strip():
                times.append((path, r.stdout.strip()))
        cfg = replace(cfg, file_times=tuple(times))
    if not cfg.source_url:
        cfg = replace(cfg, source_url=remote_url_template(repo, manifest.get("source_commit") or ""))
    files = render(claims, manifest, cfg)
    dest.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return len(files)


def from_args(preset: str, overrides: dict) -> Config:
    return replace(PRESETS[preset], **{k: v for k, v in overrides.items() if v is not None})
