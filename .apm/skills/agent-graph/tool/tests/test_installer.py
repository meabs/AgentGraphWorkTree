import importlib.util
import subprocess
import sys
from pathlib import Path

SKILL = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("agent_graph_install", SKILL / "install.py")
inst = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inst)


def test_block_is_added_refreshed_and_removed_without_touching_other_text(tmp_path: Path):
    f = tmp_path / "AGENTS.md"
    f.write_text("# Mine\n\nKeep this.\n", encoding="utf-8")
    assert inst.put_block(f, "first\n")
    assert not inst.put_block(f, "first\n")
    assert inst.put_block(f, "second\n")
    text = f.read_text(encoding="utf-8")
    assert text.startswith("# Mine\n\nKeep this.\n") and "second" in text and "first" not in text and text.count(inst.BEGIN) == 1
    assert inst.drop_block(f)
    assert f.read_text(encoding="utf-8").strip() == "# Mine\n\nKeep this.".strip()


def test_block_only_files_are_deleted_on_removal(tmp_path: Path):
    f = tmp_path / ".cursor" / "rules" / "x.mdc"
    inst.put_block(f, "body\n", inst.CURSOR_HEAD)
    assert f.read_text(encoding="utf-8").startswith("---\n")
    inst.drop_block(f)
    assert not f.exists()


def test_refuses_a_repository_without_java(tmp_path: Path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "a.rb").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    r = subprocess.run([sys.executable, str(SKILL / "install.py"), "--repo", str(tmp_path), "--home", str(tmp_path / "h")], capture_output=True, text=True)
    assert r.returncode != 0 and "Java and Python only" in r.stderr + r.stdout and not (tmp_path / ".agent-knowledge").exists() and not (tmp_path / "h").exists()


def test_launchers_name_both_shells(tmp_path: Path):
    inst.write_launchers(tmp_path, tmp_path / "venv" / "bin" / "python")
    assert (tmp_path / "bin" / "kgen").read_bytes().startswith(b"#!/bin/sh\nexec ")
    assert b"%*" in (tmp_path / "bin" / "kgen.cmd").read_bytes()
