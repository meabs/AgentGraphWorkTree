#!/usr/bin/env python3
"""Generate the code graph and notes for a Java repository and tell agents to use them (Windows, macOS, Linux).

  python install.py --repo PATH [--home DIR] [--labels confidence|tags|instruction] [--no-agents-md]
                    [--harness claude,copilot,cursor|none] [--uninstall]

The tool is copied to --home (default: ~/.local/share/agent-graph, %LOCALAPPDATA%\\agent-graph on Windows)
with its own virtual environment, so nothing is installed into your global Python.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE / "tool"
ITEMS = ["kgen.py", "kgen", "requirements.txt"]
OUT = ".agent-knowledge"
BEGIN, END = "<!-- agent-graph:begin -->", "<!-- agent-graph:end -->"
CURSOR_RULE = ".cursor/rules/generated-knowledge.mdc"
CURSOR_HEAD = "---\ndescription: Use the generated repository knowledge and code graph\nalwaysApply: true\n---\n\n"


def default_home() -> Path:
    if os.environ.get("AGENT_GRAPH_HOME"):
        return Path(os.environ["AGENT_GRAPH_HOME"])
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "agent-graph"
    return Path.home() / ".local/share/agent-graph"


def run(*cmd: str, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, **kw)


def venv_python(home: Path) -> Path:
    return home / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def ensure_env(home: Path) -> Path:
    py = venv_python(home)
    if not py.exists():
        print(f"creating a virtual environment at {home / 'venv'}")
        if run(sys.executable, "-m", "venv", str(home / "venv")).returncode:
            sys.exit("could not create a virtual environment (on Debian/Ubuntu: apt install python3-venv)")
    if run(str(py), "-c", "import tree_sitter, tree_sitter_java", capture_output=True).returncode:
        print("installing tree-sitter and tree-sitter-java")
        if run(str(py), "-m", "pip", "install", "--quiet", "-r", str(home / "requirements.txt")).returncode:
            sys.exit("pip could not install tree-sitter; check your network and Python version (3.10 or newer)")
    return py


def write_launchers(home: Path, py: Path) -> None:
    bindir = home / "bin"
    bindir.mkdir(exist_ok=True)
    script = home / "kgen.py"
    (bindir / "kgen").write_bytes(f'#!/bin/sh\nexec "{py.as_posix()}" "{script.as_posix()}" "$@"\n'.encode())
    (bindir / "kgen").chmod(0o755)
    (bindir / "kgen.cmd").write_bytes(f'@echo off\r\n"{py}" "{script}" %*\r\nexit /b %errorlevel%\r\n'.encode())


def java_files(repo: Path) -> int:
    listed = run("git", "-C", str(repo), "ls-files", "*.java", capture_output=True).stdout.split()
    return len(listed)


def snippet_of(repo: Path) -> str:
    return (repo / OUT / "AGENTS.snippet.md").read_text(encoding="utf-8").strip() + "\n"


def put_block(path: Path, block: str, prefix: str = "") -> bool:
    """Insert or refresh the marked block; text outside the markers is never touched."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    wrapped = f"{BEGIN}\n{block}{END}\n"
    if BEGIN in existing and END in existing:
        new = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", lambda _: wrapped, existing, flags=re.S)
    else:
        new = existing + ("" if not existing or existing.endswith("\n") else "\n") + ("\n" if existing.strip() else "") + (prefix if not existing else "") + wrapped
    if new == existing:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8", newline="\n")
    return True


def drop_block(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    new = re.sub(r"\n?" + re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", "", text, flags=re.S)
    if new == text:
        return False
    if not new.strip() or new.strip() == CURSOR_HEAD.strip() or new.strip() == "@AGENTS.md":
        path.unlink()
    else:
        path.write_text(new, encoding="utf-8", newline="\n")
    return True


def uninstall(repo: Path) -> int:
    if (repo / OUT).exists():
        shutil.rmtree(repo / OUT)
        print(f"removed {OUT}/")
    for rel in ("AGENTS.md", ".github/copilot-instructions.md", CURSOR_RULE):
        if drop_block(repo / rel):
            print(f"removed the generated-knowledge block from {rel}")
    claude = repo / "CLAUDE.md"
    if claude.exists() and claude.read_text(encoding="utf-8").strip() == "@AGENTS.md" and not (repo / "AGENTS.md").exists():
        claude.unlink()
        print("removed CLAUDE.md (it only imported AGENTS.md)")
    print("kept: any CLAUDE.md with other content, and the installed tool under --home (delete by hand)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--home", type=Path, default=None)
    ap.add_argument("--labels", choices=("confidence", "tags", "instruction"), default="confidence",
                    help="how generated facts are marked for agents; the default is the tested one (tags plus the trust-the-source instruction)")
    ap.add_argument("--no-agents-md", action="store_true", help="generate the files but do not edit AGENTS.md or the other instruction files")
    ap.add_argument("--harness", default="claude,copilot,cursor", help="extra instruction files besides AGENTS.md (Codex reads AGENTS.md): any of claude, copilot, cursor, or none")
    ap.add_argument("--uninstall", action="store_true", help="remove .agent-knowledge/ and the instruction blocks from this repository")
    args = ap.parse_args()

    wanted = {h.strip() for h in args.harness.split(",") if h.strip()} - {"none"}
    unknown = wanted - {"claude", "copilot", "cursor"}
    if unknown:
        sys.exit("unknown --harness: " + ", ".join(sorted(unknown)))
    if not shutil.which("git"):
        sys.exit("git is required")
    top = run("git", "-C", args.repo, "rev-parse", "--show-toplevel", capture_output=True)
    if top.returncode:
        sys.exit(f"not a git repository: {args.repo}")
    repo = Path(top.stdout.strip()).resolve()

    if args.uninstall:
        return uninstall(repo)
    if not java_files(repo):
        sys.exit("no tracked .java files: this generator reads Java only (Spring, Quarkus, JPA). Nothing was changed.")

    home = (args.home or default_home()).resolve()
    home.mkdir(parents=True, exist_ok=True)
    for item in ITEMS:
        src, dst = TOOL / item, home / item
        if dst.is_dir():
            shutil.rmtree(dst)
        elif dst.exists():
            dst.unlink()
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(src, dst)
    py = ensure_env(home)
    write_launchers(home, py)
    print(f"tool installed at {home}")

    result = run(str(py), str(home / "kgen.py"), "sync", "--repo", str(repo), "--labels", args.labels)
    if result.returncode:
        sys.exit(1)

    if not args.no_agents_md:
        block = snippet_of(repo)
        written = []
        if put_block(repo / "AGENTS.md", block):
            written.append("AGENTS.md")
        if "claude" in wanted:  # Claude Code reads CLAUDE.md, not AGENTS.md; import it
            claude = repo / "CLAUDE.md"
            existing = claude.read_text(encoding="utf-8") if claude.exists() else ""
            if "@AGENTS.md" not in existing:
                with claude.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(("\n" if existing.strip() and not existing.endswith("\n") else "") + "@AGENTS.md\n")
                written.append("CLAUDE.md")
        if "copilot" in wanted and put_block(repo / ".github" / "copilot-instructions.md", block):
            written.append(".github/copilot-instructions.md")
        if "cursor" in wanted and put_block(repo / CURSOR_RULE, block, CURSOR_HEAD):
            written.append(CURSOR_RULE)
        if written:
            print("wrote the instructions to: " + ", ".join(written) + " (review and commit)")
    print(f"done. Commit {OUT}/ so agents and teammates get it. Refresh after code changes with: {home / 'bin' / ('kgen.cmd' if os.name == 'nt' else 'kgen')} sync --repo {repo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
