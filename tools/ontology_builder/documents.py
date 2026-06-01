from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path


SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt"}
MAX_DOCUMENT_CHARS = 80_000


@dataclass
class SourceDocument:
    filename: str
    path: str
    suffix: str
    chars: int
    content: str


def read_documents(docs_dir: Path) -> list[SourceDocument]:
    docs: list[SourceDocument] = []
    for path in sorted(docs_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        if len(content) > MAX_DOCUMENT_CHARS:
            content = content[:MAX_DOCUMENT_CHARS] + "\n\n[TRUNCATED]"
        docs.append(SourceDocument(
            filename=path.name,
            path=str(path.relative_to(docs_dir)),
            suffix=path.suffix.lower(),
            chars=len(content),
            content=content,
        ))
    if not docs:
        raise FileNotFoundError(f"No supported documents found in {docs_dir}")
    return docs


def documents_state(docs: list[SourceDocument]) -> dict:
    return {
        "doc_count": len(docs),
        "total_chars": sum(doc.chars for doc in docs),
        "documents": [asdict(doc) for doc in docs],
    }


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))
