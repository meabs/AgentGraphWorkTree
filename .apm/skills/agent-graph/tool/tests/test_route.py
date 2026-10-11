import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from kgen import route  # noqa: E402


def write(results: Path, question: str, arm: str, agent: str, tokens: list[int]):
    for i, t in enumerate(tokens, 1):
        d = results / "runs" / question / arm / agent / str(i)
        d.mkdir(parents=True)
        (d / "run.json").write_text(json.dumps({"question": question, "arm": arm, "agent": agent, "status": "ok", "tokens": {"total": t}}), encoding="utf-8")


def test_question_kinds_come_from_the_prompt_words():
    assert route.kind_of("Which functions call it, directly or through up to three levels of callers?")[0] == "transitive callers"
    assert route.kind_of("Which classes inherit from Parameter?")[0] == "class hierarchy"
    assert route.kind_of("What are the http routes of the users controller?")[0] == "http routes"
    assert route.kind_of("something else entirely")[0] == "other"


def test_advice_is_use_prefer_either_or_not_enough_runs(tmp_path: Path):
    write(tmp_path, "deep_levels", "plain", "a", [1000, 1100, 900, 1000, 1050])
    write(tmp_path, "deep_levels", "graph", "a", [400, 420, 380, 410, 395])
    write(tmp_path, "inherit", "plain", "a", [500, 520, 480, 510, 490])
    write(tmp_path, "inherit", "graph", "a", [900, 950, 880, 910, 940])
    write(tmp_path, "routes", "plain", "a", [500, 700, 400, 800, 600])
    write(tmp_path, "routes", "graph", "a", [600, 500, 700, 450, 650])
    write(tmp_path, "impact_break", "plain", "a", [500, 510])
    write(tmp_path, "impact_break", "graph", "a", [300, 310])
    rows = {r["kind"]: r for r in route.advise(route.load(tmp_path, None, "plain", "graph"))}
    assert rows["transitive callers"]["verdict"] == "use kg" and rows["transitive callers"]["ratio"] < 0.5
    assert rows["class hierarchy"]["verdict"] == "prefer search"
    assert rows["http routes"]["verdict"] == "either"
    assert rows["change impact"]["verdict"] == "not enough runs"
    text = route.instruction(list(rows.values()), "a")
    assert "transitive callers" in text and "Search the text directly for: class hierarchy" in text
    assert "| transitive callers |" in route.markdown(list(rows.values()))
