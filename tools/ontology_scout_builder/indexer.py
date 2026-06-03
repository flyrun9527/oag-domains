from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path


SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt"}
MAX_SECTION_CHARS = 16_000
KEYWORD_PATTERNS = [
    "流程",
    "处理",
    "申请",
    "校验",
    "规则",
    "条件",
    "生成",
    "推荐",
    "方案",
    "审批",
    "复核",
    "输出",
    "结果",
    "字段",
    "数据",
    "状态",
    "闭环",
    "任务",
    "步骤",
    "入口",
]


@dataclass
class Section:
    section_id: str
    doc_path: str
    title_path: list[str]
    level: int
    index: int
    chars: int
    keywords: list[str]
    preview: str
    content: str


def build_document_index(docs_dir: str | Path) -> dict:
    base = Path(docs_dir).resolve()
    if not base.exists():
        raise FileNotFoundError(f"Docs directory not found: {base}")

    sections: list[Section] = []
    documents = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        rel_path = str(path.relative_to(base))
        documents.append({
            "path": rel_path,
            "chars": len(content),
            "headings": _headings(content),
        })
        sections.extend(_split_sections(rel_path, content))

    if not documents:
        raise FileNotFoundError(f"No supported documents found in {base}")

    return {
        "doc_count": len(documents),
        "section_count": len(sections),
        "total_chars": sum(doc["chars"] for doc in documents),
        "documents": documents,
        "sections": [asdict(section) for section in sections],
    }


def choose_evidence_sections(index: dict, loop_seed: dict, max_sections: int = 4) -> list[dict]:
    sections = index.get("sections") or []
    if not sections:
        return []

    query_terms = _terms_for_loop(loop_seed)
    scored = []
    for section in sections:
        haystack = " ".join([
            section.get("doc_path", ""),
            " ".join(section.get("title_path") or []),
            " ".join(section.get("keywords") or []),
            section.get("preview", ""),
        ]).lower()
        score = 0
        for term in query_terms:
            if term and term.lower() in haystack:
                score += 4 if term.lower() in " ".join(section.get("title_path") or []).lower() else 1
        score += min(5, len(section.get("keywords") or []))
        if score > 0:
            scored.append((score, section))

    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        source_id = loop_seed.get("source_section_id")
        return [s for s in sections if s.get("section_id") == source_id][:1]
    return [section for _, section in scored[:max_sections]]


def compact_index_for_prompt(index: dict, *, max_sections: int = 80) -> str:
    lines = [
        f"documents: {index.get('doc_count', 0)}, sections: {index.get('section_count', 0)}",
        "",
        "Documents:",
    ]
    for doc in index.get("documents") or []:
        headings = " | ".join((doc.get("headings") or [])[:16])
        lines.append(f"- {doc['path']} ({doc['chars']} chars): {headings}")

    lines.append("")
    lines.append("High-value sections:")
    ranked = sorted(
        index.get("sections") or [],
        key=lambda section: len(section.get("keywords") or []),
        reverse=True,
    )
    for section in ranked[:max_sections]:
        title = " > ".join(section.get("title_path") or []) or section.get("doc_path", "")
        keywords = ", ".join(section.get("keywords") or [])
        preview = section.get("preview", "").replace("\n", " ")
        lines.append(
            f"- {section['section_id']} | {section['doc_path']} | {title} | "
            f"keywords=[{keywords}] | {preview[:260]}"
        )
    return "\n".join(lines)


def evidence_bundle(sections: list[dict], max_chars: int = 32_000) -> str:
    parts = []
    used = 0
    for section in sections:
        content = section.get("content", "")
        header = (
            f"section_id: {section.get('section_id')}\n"
            f"document: {section.get('doc_path')}\n"
            f"title: {' > '.join(section.get('title_path') or [])}\n"
        )
        remaining = max_chars - used - len(header)
        if remaining <= 0:
            break
        text = content[:remaining]
        used += len(header) + len(text)
        parts.append(header + "\n" + text)
    return "\n\n---\n\n".join(parts)


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _split_sections(doc_path: str, content: str) -> list[Section]:
    blocks: list[tuple[int, list[str], list[str]]] = []
    title_stack: list[str] = []
    current_level = 0
    current_lines: list[str] = []
    current_titles: list[str] = [Path(doc_path).stem]

    def flush():
        if not current_lines:
            return
        blocks.append((current_level, list(current_titles), list(current_lines)))

    for line in content.splitlines():
        match = re.match(r"^(#{1,4})\s+(.+?)\s*$", line)
        if match:
            flush()
            level = len(match.group(1))
            title = match.group(2).strip()
            title_stack[:] = title_stack[:level - 1]
            title_stack.append(title)
            current_level = level
            current_titles = title_stack[:] or [Path(doc_path).stem]
            current_lines = [line]
            continue
        current_lines.append(line)
    flush()

    if not blocks:
        blocks = [(0, [Path(doc_path).stem], content.splitlines())]

    sections: list[Section] = []
    index = 0
    for level, titles, lines in blocks:
        text = "\n".join(lines).strip()
        if not text:
            continue
        for piece in _split_large_text(text, MAX_SECTION_CHARS):
            index += 1
            keywords = _keywords(piece, titles)
            sections.append(Section(
                section_id=_section_id(doc_path, index),
                doc_path=doc_path,
                title_path=titles,
                level=level,
                index=index,
                chars=len(piece),
                keywords=keywords,
                preview=_preview(piece),
                content=piece,
            ))
    return sections


def _split_large_text(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    pieces = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        if end < len(text):
            split_at = text.rfind("\n\n", start, end)
            if split_at > start + max_chars // 2:
                end = split_at
        pieces.append(text[start:end].strip())
        start = end
    return [piece for piece in pieces if piece]


def _headings(content: str) -> list[str]:
    result = []
    for line in content.splitlines():
        match = re.match(r"^#{1,4}\s+(.+?)\s*$", line)
        if match:
            result.append(match.group(1).strip())
        if len(result) >= 40:
            break
    return result


def _keywords(text: str, titles: list[str]) -> list[str]:
    haystack = "\n".join(titles) + "\n" + text
    return [kw for kw in KEYWORD_PATTERNS if kw in haystack]


def _preview(text: str) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    return clean[:500]


def _section_id(doc_path: str, index: int) -> str:
    safe = re.sub(r"[^0-9A-Za-z_]+", "_", doc_path).strip("_")
    return f"{safe}_{index:03d}"


def _terms_for_loop(loop_seed: dict) -> list[str]:
    sub_process_terms = []
    for item in loop_seed.get("sub_processes") or []:
        if isinstance(item, dict):
            sub_process_terms.extend([str(item.get("name", "")), str(item.get("purpose", ""))])
        else:
            sub_process_terms.append(str(item))
    text = " ".join([
        str(loop_seed.get("name", "")),
        str(loop_seed.get("trigger", "")),
        str(loop_seed.get("entry", "")),
        " ".join(loop_seed.get("key_terms") or []),
        " ".join(loop_seed.get("evidence_section_ids") or []),
        " ".join(sub_process_terms),
        " ".join(loop_seed.get("decision_points") or []),
        " ".join(loop_seed.get("final_outputs") or []),
    ])
    terms = []
    for part in re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", text):
        part = part.strip()
        if len(part) >= 2:
            terms.append(part)
    source_id = loop_seed.get("source_section_id")
    if source_id:
        terms.append(str(source_id))
    return terms[:40]
