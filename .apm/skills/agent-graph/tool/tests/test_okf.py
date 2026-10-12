import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
from kgen import generate as gen  # noqa: E402
from kgen import okf  # noqa: E402

FILES = {
    "pkg/__init__.py": "",
    "pkg/store.py": "class Store:\n    def get(self, key: str) -> str:\n        return key\n\n\ndef open_store() -> Store:\n    return Store()\n",
    "pkg/service.py": "from pkg.store import Store, open_store\n\n\nclass Service(Store):\n    def load(self, key: str):\n        s = open_store()\n        return s.get(key)\n",
    "tests/test_service.py": "from pkg.service import Service\n\n\ndef test_load():\n    assert Service().load('a') == 'a'\n",
}


def synced(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    for rel, text in FILES.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "x"], check=True)
    gen.generate(repo)
    return repo


def frontmatter(text: str) -> str:
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, "no frontmatter"
    return m.group(1)


def test_every_concept_has_a_type_and_links_resolve(tmp_path: Path):
    repo = synced(tmp_path)
    claims, manifest = okf.load(repo)
    files = okf.render(claims, manifest, okf.PRESETS["guarded"])
    yaml = __import__("pytest").importorskip("yaml")
    assert 'okf_version: "0.2"' in files["index.md"]
    concepts = {p: t for p, t in files.items() if p not in ("index.md", "log.md")}
    assert "modules/pkg.service.md" in concepts and not any("test_service" in p for p in concepts)
    for p, t in concepts.items():
        fm = yaml.safe_load(frontmatter(t))
        assert fm["type"] and fm["generated"]["by"].startswith("kgen/") and fm["stale_after"]
    for t in files.values():
        for target in re.findall(r"\]\((/[^)]+\.md)\)", t):
            assert target.lstrip("/") in files, target


def test_switches_remove_only_what_they_name(tmp_path: Path):
    repo = synced(tmp_path)
    claims, manifest = okf.load(repo)
    base = okf.render(claims, manifest, okf.Config())["modules/pkg.service.md"]
    assert "`[high]`" not in base and "sources:" in base and "pkg/service.py:" in base and "[pkg.store](/modules/pkg.store.md)" in base
    tagged = okf.render(claims, manifest, okf.Config(line_tags=True))["modules/pkg.service.md"]
    assert "`[high]`" in tagged and tagged.replace(" `[high]`", "").replace(" `[medium]`", "") .count("pkg/service.py:") == base.count("pkg/service.py:")
    minimal = okf.render(claims, manifest, okf.PRESETS["minimal"])
    assert "sources:" not in minimal["modules/pkg.service.md"] and "index.md" not in minimal and "](/modules/" not in minimal["modules/pkg.service.md"]
    assert "pkg/service.py:" not in minimal["modules/pkg.service.md"]


def test_package_unit_merges_modules_and_a_reference_bundle_is_mirrored(tmp_path: Path):
    repo = synced(tmp_path)
    other = tmp_path / "other"
    (other / "modules").mkdir(parents=True)
    (other / "index.md").write_text('---\nokf_version: "0.2"\n---\n\n# Other\n', encoding="utf-8")
    (other / "modules" / "x.md").write_text("---\ntype: Code Module\n---\n[self](/modules/x.md)\n", encoding="utf-8")
    claims, manifest = okf.load(repo)
    files = okf.render(claims, manifest, okf.Config(unit="package", references=(("other", str(other)),)))
    assert "modules/pkg.md" in files and "modules/pkg.service.md" not in files
    assert "](/references/other/modules/x.md)" in files["references/other/modules/x.md"]
    assert files["references/other/index.md"].startswith("# Other") and "references/other/modules/x.md" in files and "/references/other/index.md" in files["index.md"]


def test_only_concepts_with_changed_sources_get_a_past_stale_after(tmp_path: Path):
    repo = synced(tmp_path)
    claims, manifest = okf.load(repo)
    files = okf.render(claims, manifest, okf.Config(stale_files=("pkg/store.py",)))
    assert "stale_after" in files["modules/pkg.store.md"] and "stale_after" not in files["modules/pkg.service.md"]


def test_recommended_preset_and_pointer_text():
    assert okf.PRESETS["recommended"] == okf.Config()
    plain = okf.pointer(okf.Config())
    assert "read the source to confirm before you rely on it" in plain and "stale_after" not in plain and "references" not in plain
    full = okf.pointer(okf.Config(stale_files=("a.py",), references=(("x", "/p"),)))
    assert "stale_after" in full and "okf/references/" in full


def test_resource_fields_are_urls_or_relative_paths_and_a_module_named_index_does_not_take_a_reserved_name(tmp_path: Path):
    repo = synced(tmp_path)
    (repo / "pkg" / "index.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    gen.generate(repo)
    claims, manifest = okf.load(repo)
    files = okf.render(claims, manifest, okf.Config(source_url="https://example.com/o/r/blob/abc/{path}"))
    text = files["modules/pkg.service.md"]
    assert 'resource: "https://example.com/o/r/blob/abc/pkg/service.py"' in text and "repo:" not in text and "    id:" not in text and "sha256:" in text
    rel = okf.render(claims, manifest, okf.Config())["modules/pkg.service.md"]
    assert 'resource: "../../pkg/service.py"' in rel
    assert "modules/pkg.index.md" in files and not any(p.endswith("/index.md") and p != "index.md" for p in files)
    assert okf.mod_path("index") == "/modules/index-module.md" and okf.mod_path("log") == "/modules/log-module.md"



def test_last_modified_is_the_files_own_time(tmp_path: Path):
    repo = synced(tmp_path)
    claims, manifest = okf.load(repo)
    files = okf.render(claims, manifest, okf.Config(file_times=(("pkg/store.py", "2020-01-02T03:04:05+00:00"),)))
    assert 'last_modified: "2020-01-02T03:04:05+00:00"' in files["modules/pkg.store.md"]
    assert 'last_modified: "2020-01-02T03:04:05+00:00"' not in files["modules/pkg.service.md"]


def test_routes_become_one_concept_and_java_packages_are_concepts(tmp_path: Path):
    repo = tmp_path / "j"
    src = repo / "src/main/java/com/acme/web"
    src.mkdir(parents=True)
    (src / "ItemController.java").write_text('package com.acme.web;\nimport org.springframework.web.bind.annotation.*;\n@RestController\n@RequestMapping("/api/v1")\npublic class ItemController {\n  @GetMapping("/items/{id}")\n  public String get(String id) { return helper(id); }\n  private String helper(String id) { return id; }\n  private String name;\n  public String getName() { return "x"; }\n}\n', encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    gen.generate(repo)
    claims, manifest = okf.load(repo)
    files = okf.render(claims, manifest, okf.Config())
    assert "modules/com.acme.web.md" in files and "routes.md" in files
    assert "/api/v1/items/{id}" in files["routes.md"] and "ItemController.get" in files["routes.md"]
    assert "`ItemController.name: String`" in files["modules/com.acme.web.md"]
    assert "ItemController.get(" in files["modules/com.acme.web.md"] and "[HTTP routes](/routes.md)" in files["index.md"]


def test_pointer_asks_the_reader_to_say_when_it_relies_on_a_medium_entry_only_when_lines_are_tagged():
    assert "relies on one, say so" not in okf.pointer(okf.Config())
    assert "relies on one, say so" in okf.pointer(okf.Config(line_tags=True))


def test_committing_the_guide_itself_does_not_change_it(tmp_path: Path):
    repo = synced(tmp_path)
    claims, manifest = okf.load(repo)
    first = okf.write(repo, tmp_path / "g1", okf.Config())
    (repo / "NOTES.md").write_text("not source\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", "docs only"], check=True)
    gen.generate(repo)
    okf.write(repo, tmp_path / "g2", okf.Config())
    a = {p.relative_to(tmp_path / "g1").as_posix(): p.read_text(encoding="utf-8") for p in (tmp_path / "g1").rglob("*.md")}
    b = {p.relative_to(tmp_path / "g2").as_posix(): p.read_text(encoding="utf-8") for p in (tmp_path / "g2").rglob("*.md")}
    assert a == b and first == len(a), "a commit that touches no cited source file must not change the guide"


def test_generated_at_is_the_newest_change_among_the_cited_files(tmp_path: Path):
    repo = synced(tmp_path)
    claims, manifest = okf.load(repo)
    files = okf.render(claims, manifest, okf.Config(generated_at="2021-05-06T07:08:09+00:00", file_times=(("pkg/store.py", "2020-01-02T03:04:05+00:00"),)))
    assert 'at: "2021-05-06T07:08:09+00:00"' in files["modules/pkg.store.md"]
