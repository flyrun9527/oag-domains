from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path


SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt"}
MAX_DOCUMENT_CHARS = 120_000
MAX_CHUNK_CHARS = 12_000


@dataclass
class SourceDocument:
    filename: str
    path: str
    suffix: str
    chars: int
    content: str


@dataclass
class DocumentChunk:
    chunk_id: str
    doc_path: str
    title: str
    index: int
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


def build_chunks(docs: list[SourceDocument], max_chars: int = MAX_CHUNK_CHARS) -> list[DocumentChunk]:
    chunks: list[DocumentChunk] = []
    for doc in docs:
        sections = _split_markdown_sections(doc.content)
        pending_title = ""
        pending: list[str] = []
        pending_chars = 0
        index = 0

        def flush() -> None:
            nonlocal pending, pending_chars, pending_title, index
            text = "\n\n".join(part.strip() for part in pending if part.strip()).strip()
            if not text:
                pending = []
                pending_chars = 0
                return
            index += 1
            chunks.append(DocumentChunk(
                chunk_id=_chunk_id(doc.path, index),
                doc_path=doc.path,
                title=pending_title or doc.filename,
                index=index,
                chars=len(text),
                content=text,
            ))
            pending = []
            pending_chars = 0
            pending_title = ""

        for title, text in sections:
            if len(text) > max_chars:
                flush()
                for piece in _split_large_text(text, max_chars):
                    index += 1
                    chunks.append(DocumentChunk(
                        chunk_id=_chunk_id(doc.path, index),
                        doc_path=doc.path,
                        title=title or doc.filename,
                        index=index,
                        chars=len(piece),
                        content=piece,
                    ))
                continue
            if pending and pending_chars + len(text) > max_chars:
                flush()
            pending.append(text)
            pending_chars += len(text)
            pending_title = pending_title or title or doc.filename
        flush()
    return chunks


def documents_state(docs: list[SourceDocument]) -> dict:
    chunks = build_chunks(docs)
    return {
        "doc_count": len(docs),
        "chunk_count": len(chunks),
        "total_chars": sum(doc.chars for doc in docs),
        "documents": [asdict(doc) for doc in docs],
        "chunks": [asdict(chunk) for chunk in chunks],
    }


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _split_markdown_sections(text: str) -> list[tuple[str, str]]:
    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = []
    current_title = ""
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_lines
        content = "\n".join(current_lines).strip()
        if content:
            sections.append((current_title, [content]))
        current_lines = []

    for line in lines:
        match = re.match(r"^(#{1,4})\s+(.+?)\s*$", line)
        if match:
            flush()
            current_title = match.group(2).strip()
        current_lines.append(line)
    flush()
    if not sections and text.strip():
        return [("", text.strip())]
    return [(title, "\n".join(parts)) for title, parts in sections]


def _split_large_text(text: str, max_chars: int) -> list[str]:
    paragraphs = re.split(r"\n{2,}", text)
    pieces: list[str] = []
    pending: list[str] = []
    pending_chars = 0
    for para in paragraphs:
        if len(para) > max_chars:
            if pending:
                pieces.append("\n\n".join(pending))
                pending = []
                pending_chars = 0
            for start in range(0, len(para), max_chars):
                pieces.append(para[start:start + max_chars])
            continue
        if pending and pending_chars + len(para) > max_chars:
            pieces.append("\n\n".join(pending))
            pending = []
            pending_chars = 0
        pending.append(para)
        pending_chars += len(para)
    if pending:
        pieces.append("\n\n".join(pending))
    return pieces


def _chunk_id(doc_path: str, index: int) -> str:
    stem = re.sub(r"[^0-9A-Za-z_]+", "_", Path(doc_path).stem).strip("_").lower()
    return f"{stem or 'doc'}_{index:03d}"
