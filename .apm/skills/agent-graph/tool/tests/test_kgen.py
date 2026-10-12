import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
from kgen import generate as gen  # noqa: E402

BASE = "src/main/java/demo/"
FILES = {
    BASE + "Base.java": "package demo;\n\npublic class Base {\n\tpublic Integer getId() { return 1; }\n}\n",
    BASE + "Thing.java": "package demo;\n\nimport jakarta.persistence.*;\n\n@Entity\n@Table(name = \"things\")\npublic class Thing extends Base {\n\t@Column(name = \"label_x\")\n\tprivate String label;\n}\n",
    BASE + "Widget.java": "package demo;\n\n@jakarta.persistence.Entity\npublic class Widget {\n}\n",
    BASE + "ThingRepository.java": "package demo;\n\nimport org.springframework.data.jpa.repository.JpaRepository;\n\npublic interface ThingRepository extends JpaRepository<Thing, Integer> {\n}\n",
    BASE + "ThingController.java": (
        "package demo;\n\nimport java.util.Optional;\nimport org.springframework.data.domain.Page;\n\n@Controller\n@RequestMapping(\"/things\")\nclass ThingController {\n"
        "\tprivate static final String VIEW = \"things/form\";\n\tprivate final ThingRepository repo;\n\tThingController(ThingRepository repo) { this.repo = repo; }\n"
        "\t@GetMapping(\"/{id}\")\n\tString show(int id) {\n\t\tThing t = repo.findById(id).get();\n\t\tPage<Widget> p = null;\n\t\tOptional<Thing> o = null;\n\t\tp.getTotalPages();\n\t\to.orElseThrow();\n\t\tt.getId();\n\t\treturn \"things/show\";\n\t}\n"
        "\t@PostMapping(\"/new\")\n\tString create() {\n\t\treturn VIEW;\n\t}\n"
        "\t@GetMapping(\"/mav\")\n\torg.springframework.web.servlet.ModelAndView mav() {\n\t\tModelAndView m = new ModelAndView(\"things/show\");\n\t\treturn m;\n\t}\n"
        "\t@GetMapping(\"/go\")\n\tString go(Thing t) {\n\t\tif (t == null) {\n\t\t\treturn \"things/form\";\n\t\t}\n\t\treturn \"redirect:/things/\" + t.getId();\n\t}\n"
        "\t@GetMapping(\"/list\")\n\tString list() {\n\t\treturn helper();\n\t}\n\tprivate String helper() {\n\t\treturn \"things/show\";\n\t}\n"
        "\t@GetMapping(\"/body\")\n\t@ResponseBody\n\tThing body() {\n\t\treturn null;\n\t}\n}\n"
    ),
    BASE + "package-info.java": "package demo;\n",
    "src/test/java/demo/ThingControllerTests.java": "package demo;\n\nclass ThingControllerTests {\n\t@Test void a() {}\n}\n",
    "src/main/resources/templates/things/show.html": "<html></html>\n",
    "src/main/resources/templates/things/form.html": "<html></html>\n",
    "src/main/resources/application.properties": "spring.datasource.url=jdbc:h2:mem:x\nspring.datasource.password=hunter2\n",
    "pom.xml": "<project><modelVersion>4.0.0</modelVersion><groupId>demo</groupId><artifactId>demo</artifactId><version>1</version></project>\n",
}


def git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    for rel, body in FILES.items():
        f = r / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
    git(r, "init", "-q")
    git(r, "-c", "user.email=a@b.c", "-c", "user.name=t", "add", ".")
    git(r, "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qm", "x")
    return r


def claims(repo):
    return [json.loads(l) for l in (repo / gen.OUT / "claims.jsonl").read_text(encoding="utf-8").splitlines()]


def find(cs, **kw):
    return [c for c in cs if all(c.get(k) == v or c["attrs"].get(k) == v for k, v in kw.items())]


def test_high_and_medium_are_assigned_by_derivation(repo):
    gen.generate(repo)
    cs = claims(repo)
    route = find(cs, predicate="handled_by", subject="route:GET /things/{id}")[0]
    assert route["confidence"] == "high" and route["source"]["line"] == 12
    tables = {c["subject"]: c for c in cs if c["predicate"] == "maps_to_table"}
    assert tables["type:demo.Thing"]["confidence"] == "high" and tables["type:demo.Thing"]["object"] == "table:things"
    assert tables["type:demo.Widget"]["confidence"] == "medium" and tables["type:demo.Widget"]["object"] == "table:widget"
    views = {c["subject"]: c for c in cs if c["predicate"] == "renders"}
    assert views["route:GET /things/{id}"]["confidence"] == "high"
    assert views["route:POST /things/new"]["confidence"] == "medium"
    assert not any(c["confidence"] == "low" for c in cs)


def test_generic_receivers_are_not_unwrapped_and_inherited_methods_find_their_owner(repo):
    gen.generate(repo)
    targets = {c["object"] for c in claims(repo) if c["predicate"] == "calls"}
    assert "method:demo.Widget#getTotalPages" not in targets
    assert "method:demo.Thing#orElseThrow" not in targets
    assert "method:demo.Base#getId" in targets
    assert "method:demo.ThingRepository#findById" in targets


def test_secret_values_are_redacted_everywhere(repo):
    gen.generate(repo)
    for f in (repo / gen.OUT).rglob("*"):
        if f.is_file():
            assert "hunter2" not in f.read_text(encoding="utf-8"), f


def test_generation_is_deterministic_and_idempotent(repo):
    gen.generate(repo)
    first = {str(p.relative_to(repo)): p.read_bytes() for p in (repo / gen.OUT).rglob("*") if p.is_file()}
    r = gen.generate(repo)
    assert r["written"] == []
    second = {str(p.relative_to(repo)): p.read_bytes() for p in (repo / gen.OUT).rglob("*") if p.is_file()}
    assert first == second


def test_output_stays_under_agent_knowledge_and_docs_are_labelled(repo):
    gen.generate(repo)
    top = {p.name for p in (repo / gen.OUT).iterdir()}
    assert {"claims.jsonl", "graph.json", "manifest.json", "okf", "skills", "bin", "AGENTS.snippet.md"} <= top
    routes = (repo / gen.OUT / "okf/routes.md").read_text(encoding="utf-8")
    assert routes.startswith("---\ntype: api")
    assert "generated: true" in routes and "Auto-generated" in routes and "`[medium]`" in routes


def run_kgen(*args):
    return subprocess.run([sys.executable, str(ROOT / "kgen.py"), *args], capture_output=True, text=True, cwd=ROOT)


def test_check_and_verify_detect_a_modified_source_file(repo):
    gen.generate(repo)
    assert run_kgen("check", "--repo", str(repo)).returncode == 0
    assert run_kgen("verify", "--repo", str(repo)).returncode == 0
    f = repo / BASE / "ThingController.java"
    f.write_text("// new first line\n" + f.read_text(encoding="utf-8"))
    c = run_kgen("check", "--repo", str(repo))
    assert c.returncode == 1 and "ThingController.java" in c.stdout and "okf/routes.md" in c.stdout
    assert run_kgen("verify", "--repo", str(repo)).returncode == 1
    (repo / BASE / "Extra.java").write_text("package demo;\nclass Extra {}\n", encoding="utf-8")
    git(repo, "add", ".")
    assert "Extra.java" in run_kgen("check", "--repo", str(repo)).stdout


def test_kg_cli_answers_from_the_generated_graph(repo):
    gen.generate(repo)
    kg = [sys.executable, str(repo / gen.OUT / "kg.py")]
    out = subprocess.run([*kg, "impact", "Thing"], capture_output=True, text=True).stdout
    assert "route" in out and "things/{id}" in out
    hi = subprocess.run([*kg, "impact", "--min", "high", "Thing"], capture_output=True, text=True).stdout
    assert "[medium]" not in hi
    assert subprocess.run([*kg, "routes"], capture_output=True, text=True).stdout.count("route:") == 6


def test_repository_generic_argument_is_managed_not_extended(repo):
    gen.generate(repo)
    cs = claims(repo)
    ext = [c["object"] for c in cs if c["predicate"] == "extends" and c["subject"] == "type:demo.ThingRepository"]
    assert ext == ["external:org.springframework.data.jpa.repository.JpaRepository"]
    assert [c["object"] for c in cs if c["predicate"] == "manages"] == ["type:demo.Thing"]


@pytest.mark.parametrize("mode", ["none", "provenance"])
def test_non_confidence_modes_leak_no_confidence_information(repo, tmp_path, mode):
    out = tmp_path / "o"
    gen.generate(repo, out, mode)
    banned = ["confidence", "[high]", "[medium]", "heuristic", "inference"]
    for f in out.rglob("*"):
        if f.is_file():
            text = f.read_text(encoding="utf-8").lower()
            assert not [b for b in banned if b in text], (f, mode)
    r = subprocess.run([sys.executable, str(out / "kg.py"), "show", "ThingController"], capture_output=True, text=True)
    assert r.returncode == 0 and "[" not in r.stdout.split("\n")[0].split("  ")[0]
    assert ("Auto-generated" in (out / "okf/index.md").read_text(encoding="utf-8") or "Auto-generated" in (out / "okf/packages/demo.md").read_text(encoding="utf-8")) == (mode == "provenance")


def test_freshness_flag_adds_stale_command_only_when_requested(repo, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    gen.generate(repo, a, "confidence", True)
    gen.generate(repo, b, "confidence", False)
    assert "def cmd_stale" in (a / "kg.py").read_text(encoding="utf-8")
    assert "def cmd_stale" not in (b / "kg.py").read_text(encoding="utf-8")
    assert "kg stale" in (a / "AGENTS.snippet.md").read_text(encoding="utf-8") and "kg stale" not in (b / "AGENTS.snippet.md").read_text(encoding="utf-8")


def test_route_views_cover_model_and_view_locals_concatenated_redirects_helpers_and_bodies(repo):
    gen.generate(repo)
    cs = claims(repo)

    def objs(pred, subject):
        return {(c["object"], c["confidence"]) for c in find(cs, predicate=pred, subject=subject)}

    assert objs("renders", "route:GET /things/mav") == {("template:things/show", "high")}
    assert objs("renders", "route:GET /things/go") == {("template:things/form", "high")}
    assert objs("redirects_to", "route:GET /things/go") == {("redirect:/things/${t.getId()}", "medium")}
    assert objs("renders", "route:GET /things/list") == {("template:things/show", "medium")}
    assert objs("returns_body", "route:GET /things/body") == {("Thing", "high")}
    routes = (repo / gen.OUT / "okf/routes.md").read_text(encoding="utf-8")
    assert "redirect:/things/${t.getId()}`" in routes and "response body `Thing`" in routes


def test_min_tier_high_drops_medium_claims_and_says_so(repo):
    gen.generate(repo, min_tier="high")
    assert {c["confidence"] for c in claims(repo)} == {"high"}
    idx = (repo / gen.OUT / "okf/index.md").read_text(encoding="utf-8")
    assert "[medium]" not in idx and "omitted" in idx
    assert "medium" not in (repo / gen.OUT / "AGENTS.snippet.md").read_text(encoding="utf-8")


def test_components_docs_and_graph_write_disjoint_files(repo, tmp_path):
    d, g = tmp_path / "d", tmp_path / "g"
    gen.generate(repo, d, components="docs")
    gen.generate(repo, g, components="graph")
    assert (d / "okf/index.md").exists() and not (d / "graph.json").exists() and not (d / "kg.py").exists()
    assert "bin/kg" not in (d / "AGENTS.snippet.md").read_text(encoding="utf-8")
    assert (g / "graph.json").exists() and (g / "bin/kg").exists() and not (g / "okf").exists()
    assert "okf/index.md" not in (g / "AGENTS.snippet.md").read_text(encoding="utf-8")


def test_extra_low_claims_are_tagged_low_in_docs_graph_and_kg(repo, tmp_path):
    ex = tmp_path / "llm.jsonl"
    rows = [
        {"subject": "type:demo.ThingController", "predicate": "purpose", "object": "Serves the thing pages", "source": {"file": BASE + "ThingController.java", "line": 8}, "basis": "llm-inferred"},
        {"subject": "type:demo.Widget", "predicate": "implicitly_depends_on", "object": "type:demo.Thing", "source": {"file": BASE + "Widget.java", "line": 3}, "basis": "shares the things table by convention"},
    ]
    ex.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    gen.generate(repo, extra_claims=ex)
    assert {c["confidence"] for c in claims(repo)} == {"high", "medium", "low"}
    inf = (repo / gen.OUT / "okf/inferred.md").read_text(encoding="utf-8")
    assert "`[low]`" in inf and "shares the things table" in inf
    assert "language model" in (repo / gen.OUT / "okf/index.md").read_text(encoding="utf-8")
    kg = [sys.executable, str(repo / gen.OUT / "kg.py")]
    out = subprocess.run([*kg, "show", "type:demo.Widget"], capture_output=True, text=True).stdout
    assert "[low] implicitly_depends_on" in out
    assert "[low]" not in subprocess.run([*kg, "show", "--min", "medium", "type:demo.Widget"], capture_output=True, text=True).stdout
    assert "Thing" in subprocess.run([*kg, "dependents", "Thing"], capture_output=True, text=True).stdout


def test_webmvctest_filter_classes_are_not_test_targets(repo):
    p = repo / "src/test/java/demo/ThingControllerTests.java"
    p.write_text("package demo;\n\n@WebMvcTest(value = ThingController.class,\n\t\tincludeFilters = @ComponentScan.Filter(value = Widget.class, type = FilterType.ASSIGNABLE_TYPE))\nclass ThingControllerTests {\n\t@Test void a() {}\n}\n", encoding="utf-8")
    git(repo, "add", "-A")
    gen.generate(repo)
    tested = [c["object"] for c in claims(repo) if c["predicate"] == "tests" and c["subject"] == "type:demo.ThingControllerTests"]
    assert tested == ["type:demo.ThingController"]


def test_tags_and_instruction_modes_separate_the_trust_signals(repo, tmp_path):
    import shutil
    outs = {}
    for mode in ("tags", "instruction"):
        r = tmp_path / mode
        shutil.copytree(repo, r)
        gen.generate(r, mode=mode)
        outs[mode] = r / ".agent-knowledge"
    routes_t = (outs["tags"] / "okf/routes.md").read_text(encoding="utf-8")
    routes_i = (outs["instruction"] / "okf/routes.md").read_text(encoding="utf-8")
    assert "`[high]`" in routes_t and "Auto-generated" not in routes_t and "generated: true" not in routes_t
    assert "`[high]`" not in routes_i and "Auto-generated" not in routes_i
    assert "trust the source" not in (outs["tags"] / "AGENTS.snippet.md").read_text(encoding="utf-8")
    assert "trust the source" in (outs["instruction"] / "AGENTS.snippet.md").read_text(encoding="utf-8")


def test_quarkus_jaxrs_routes_panache_and_nested_module_tests(repo):
    q = "mod/src/main/java/q/"
    (repo / "mod/src/main/java/q").mkdir(parents=True)
    (repo / "mod/src/test/java/q").mkdir(parents=True)
    (repo / q / "Fruit.java").write_text("package q;\n\n@jakarta.persistence.Entity\npublic class Fruit extends io.quarkus.hibernate.orm.panache.PanacheEntity {\n}\n", encoding="utf-8")
    (repo / q / "FruitRepo.java").write_text("package q;\n\n@ApplicationScoped\npublic class FruitRepo implements PanacheRepository<Fruit> {\n}\n", encoding="utf-8")
    (repo / q / "FruitResource.java").write_text(
        "package q;\n\n@Path(\"/fruits\")\npublic class FruitResource {\n\t@Inject FruitRepo repo;\n\t@GET\n\tpublic String all() { return \"\"; }\n"
        "\t@DELETE\n\t@Path(\"{id}\")\n\tpublic void del() {}\n}\n", encoding="utf-8")
    (repo / q / "Client.java").write_text("package q;\n\n@RegisterRestClient\n@Path(\"/remote\")\ninterface Client {\n\t@GET String get();\n}\n", encoding="utf-8")
    (repo / "mod/src/test/java/q/FruitResourceTest.java").write_text("package q;\n\n@QuarkusTest\nclass FruitResourceTest {\n\t@Test void a() {}\n}\n", encoding="utf-8")
    git(repo, "add", "-A")
    gen.generate(repo)
    cs = claims(repo)
    routes = sorted(c["subject"] for c in cs if c["predicate"] == "handled_by" and "FruitResource" in c["object"])
    assert routes == ["route:DELETE /fruits/{id}", "route:GET /fruits"]
    assert not any(c["subject"].startswith("route:GET /remote") for c in cs)
    (repo / "mod/src/main/java/q/Sub.java").write_text("package q;\n\npublic class Sub {\n\t@GET\n\t@Path(\"x\")\n\tpublic String x() { return \"\"; }\n}\n", encoding="utf-8")
    git(repo, "add", "-A")
    gen.generate(repo)
    sub = [c for c in claims(repo) if c["predicate"] == "handled_by" and "Sub#x" in c["object"]]
    assert [(c["subject"], c["confidence"]) for c in sub] == [("route:GET /x", "medium")]
    assert [c["object"] for c in cs if c["predicate"] == "manages" and c["subject"] == "type:q.FruitRepo"] == ["type:q.Fruit"]
    assert any(c["predicate"] == "tests" and c["subject"] == "type:q.FruitResourceTest" for c in cs)


def test_openapi_file_routes_link_to_methods_by_operation_id(repo):
    r = repo / "svc/src/main/resources/openapi"
    r.mkdir(parents=True)
    (r / "openapi.yml").write_text("openapi: 3.0.0\npaths:\n  /api/things:\n    get:\n      operationId: listThings\n    post:\n      operationId: addThing\n  /api/things/{id}:\n    delete:\n      operationId: dropThing\n", encoding="utf-8")
    d = repo / "svc/src/main/java/s"
    d.mkdir(parents=True)
    (d / "ThingResource.java").write_text("package s;\n\npublic class ThingResource {\n\tpublic void listThings() {}\n\tpublic void addThing() {}\n}\n", encoding="utf-8")
    (d / "ThingService.java").write_text("package s;\n\npublic class ThingService {\n\tpublic void listThings() {}\n}\n", encoding="utf-8")
    git(repo, "add", "-A")
    gen.generate(repo)
    cs = claims(repo)
    defs = sorted(c["subject"] for c in cs if c["predicate"] == "defined_in" and c["subject"].startswith("route:"))
    assert defs == ["route:DELETE /api/things/{id}", "route:GET /api/things", "route:POST /api/things"]
    linked = {c["subject"]: c["object"] for c in cs if c["predicate"] == "handled_by" and c["extractor"] == "openapi"}
    assert linked == {"route:GET /api/things": "method:s.ThingResource#listThings", "route:POST /api/things": "method:s.ThingResource#addThing"}


def test_route_paths_come_from_value_and_path_not_from_produces_or_consumes(tmp_path):
    repo = tmp_path / "r"
    d = repo / BASE
    d.mkdir(parents=True)
    (d / "Api.java").write_text(
        "package demo;\n\n@RestController\n@RequestMapping(value = \"/api\", produces = {\"application/json\"})\nclass Api {\n"
        "\t@RequestMapping(value = \"/login\", method = RequestMethod.POST, produces = {\"application/json\"})\n\tString login() { return \"\"; }\n"
        "\t@GetMapping(path = \"/y\", produces = \"text/plain\")\n\tString y() { return \"\"; }\n"
        "\t@GetMapping(\"/x\")\n\tString x() { return \"\"; }\n"
        "\t@PostMapping(consumes = \"application/json\")\n\tString root() { return \"\"; }\n}\n", encoding="utf-8")
    git(tmp_path, "init", "-q", str(repo))
    git(repo, "add", ".")
    gen.generate(repo)
    routes = sorted(c["subject"] for c in claims(repo) if c["predicate"] == "handled_by")
    assert routes == ["route:GET /api/x", "route:GET /api/y", "route:POST /api", "route:POST /api/login"], routes
