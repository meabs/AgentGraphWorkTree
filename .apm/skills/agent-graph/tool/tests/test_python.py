import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
from kgen import generate as gen  # noqa: E402

FILES = {
    "app/__init__.py": "",
    "app/models.py": "from dataclasses import dataclass\n\n\nclass Base:\n    def ident(self):\n        return 1\n\n\n@dataclass\nclass User(Base):\n    name: str = ''\n\n    def greet(self) -> str:\n        return self.name\n",
    "app/repo.py": "from .models import User\n\n\nclass Repo:\n    def __init__(self):\n        self.cache = {}\n\n    def get(self, key) -> User:\n        return User()\n",
    "app/service.py": (
        "from typing import overload\n\nfrom . import repo as repo_mod\nfrom .models import User\nfrom .repo import Repo\n\n\n"
        "class Service:\n    def __init__(self, repo: Repo):\n        self.repo = repo\n        self.users = repo_mod.Repo()\n\n"
        "    def load(self, key):\n        user = self.repo.get(key)\n        self.audit(user)\n        return user.ident()\n\n"
        "    def audit(self, user: User):\n        return user.greet()\n\n    def other(self):\n        return self.users.get(1)\n\n"
        "    @overload\n    def pick(self, a: int) -> int: ...\n\n    @overload\n    def pick(self, a: str) -> str: ...\n\n    def pick(self, a):\n        return self.audit(None)\n\n\n"
        "def helper(x):\n    return Service(Repo()).load(x)\n"
    ),
    "app/web.py": "from flask import Flask\nfrom .service import helper\n\napp = Flask(__name__)\nbp = object()\n\n\n@app.route('/x', methods=['GET', 'POST'])\ndef x():\n    return helper(1)\n\n\n@app.get('/y')\ndef y():\n    return helper(2)\n",
    "tests/test_service.py": "from app.service import Service\n\n\ndef test_a():\n    Service(None)\n\n\nclass TestService:\n    def test_b(self):\n        pass\n",
    "broken.py": "def (:\n",
}


def build(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    for rel, body in FILES.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    gen.generate(repo)
    return repo


def claims(repo: Path) -> list[dict]:
    return [json.loads(line) for line in (repo / gen.OUT / "claims.jsonl").read_text(encoding="utf-8").splitlines()]


def edges(repo: Path, predicate: str) -> set[tuple[str, str, str]]:
    return {(c["subject"], c["object"], c["confidence"]) for c in claims(repo) if c["predicate"] == predicate}


def test_classes_methods_and_inheritance(tmp_path: Path):
    repo = build(tmp_path)
    c = claims(repo)
    kinds = {x["subject"]: x["object"] for x in c if x["predicate"] == "is_a"}
    assert kinds["type:app.models.User"] == "class" and kinds["type:app.models"] == "module"
    assert ("type:app.models.User", "type:app.models.Base", "high") in edges(repo, "extends")
    assert any(x["predicate"] == "has_method" and x["object"] == "method:app.service#helper" for x in c)
    user = next(x for x in c if x["predicate"] == "is_a" and x["subject"] == "type:app.models.User")
    assert user["attrs"]["stereotypes"] == ["dataclass"]


def test_calls_resolve_through_annotations_self_and_imports(tmp_path: Path):
    repo = build(tmp_path)
    calls = edges(repo, "calls")
    medium = lambda a, b: (a, b, "medium") in calls  # noqa: E731
    assert medium("method:app.service.Service#load", "method:app.repo.Repo#get")        # self.repo typed by a parameter annotation
    assert medium("method:app.service.Service#load", "method:app.service.Service#audit")  # self call
    assert medium("method:app.service.Service#load", "method:app.models.Base#ident")      # return annotation, method found on the base class
    assert medium("method:app.service.Service#audit", "method:app.models.User#greet")     # annotated parameter
    assert medium("method:app.service.Service#other", "method:app.repo.Repo#get")         # attribute from a constructor call via a module import
    assert medium("method:app.service#helper", "method:app.service.Service#load")         # chained call on a constructor result
    assert ("method:app.service#helper", "type:app.service.Service", "medium") in edges(repo, "instantiates")


def test_overload_stubs_do_not_hide_the_real_body(tmp_path: Path):
    repo = build(tmp_path)
    assert ("method:app.service.Service#pick", "method:app.service.Service#audit", "medium") in edges(repo, "calls")


def test_imports_routes_tests_and_unparseable_files(tmp_path: Path):
    repo = build(tmp_path)
    assert ("type:app.service", "type:app.repo", "high") in edges(repo, "depends_on")
    routes = {(x["attrs"]["http"], x["attrs"]["path"], x["confidence"], x["object"]) for x in claims(repo) if x["predicate"] == "handled_by"}
    assert ("GET", "/x", "high", "method:app.web#x") in routes and ("POST", "/x", "high", "method:app.web#x") in routes and ("GET", "/y", "high", "method:app.web#y") in routes
    tests = edges(repo, "tests")
    assert ("type:test_service.TestService", "type:app.service.Service", "medium") in tests
    assert ("type:test_service", "type:app.service", "medium") in tests
    assert not any("broken" in x["source"]["file"] for x in claims(repo))


def test_sync_is_idempotent_for_python(tmp_path: Path):
    repo = build(tmp_path)
    again = gen.generate(repo)
    assert again["written"] == [] and again["removed"] == []
