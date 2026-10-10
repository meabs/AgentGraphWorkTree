"""Claim model. Every generated statement is a triple with provenance and a confidence tier."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

HIGH, MEDIUM, LOW = "high", "medium", "low"
RANK = {HIGH: 3, MEDIUM: 2, LOW: 1}


@dataclass
class Claim:
    subject: str
    predicate: str
    object: str
    confidence: str
    basis: str
    file: str
    line: int
    end_line: int = 0
    evidence: str = ""
    attrs: dict = field(default_factory=dict)
    extractor: str = ""

    @property
    def id(self) -> str:
        key = f"{self.subject}|{self.predicate}|{self.object}|{self.file}|{self.line}"
        return hashlib.sha256(key.encode()).hexdigest()[:12]

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object,
            "confidence": self.confidence,
            "basis": self.basis,
            "source": {"file": self.file, "line": self.line, "end_line": self.end_line or self.line},
            "evidence": self.evidence,
            "attrs": self.attrs,
            "extractor": self.extractor,
        }


def dump_claims(claims: list[Claim]) -> str:
    rows = sorted((c.to_json() for c in claims), key=lambda r: (r["source"]["file"], r["source"]["line"], r["predicate"], r["subject"], r["object"], r["id"]))
    return "".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows)


def sha256_file(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
