from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import yaml

from oag.ontology.schema import Ontology

from .documents import documents_state, read_documents, read_json, write_json
from .llm import DistillerLLM
from .prompts import (
    BLUEPRINT_SYSTEM,
    BLUEPRINT_USER,
    CHUNK_EXTRACT_SYSTEM,
    CHUNK_EXTRACT_USER,
    FIX_SECTION_SYSTEM,
    FIX_SECTION_USER,
    OBJECTS_USER,
    REVIEW_SECTION_SYSTEM,
    REVIEW_SECTION_USER,
    SECTION_SYSTEM,
    SECTION_USER,
    read_metamodel_spec,
    read_modeling_guide,
)

log = logging.getLogger(__name__)

SECTIONS = ("links", "functions", "rules", "workflows")


def _strip_fences(text: str) -> str:
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _snake(name: str) -> str:
    name = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "_", name).strip("_")
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return name.lower() or "domain"


def _load_yaml(text: str) -> dict[str, Any]:
    data = yaml.safe_load(_strip_fences(text))
    if not isinstance(data, dict):
        raise ValueError("Generated YAML is not a mapping")
    return data


def _dump_yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def _document_map(docs_state: dict[str, Any]) -> str:
    lines = []
    for doc in docs_state["documents"]:
        headings = []
        for line in doc["content"].splitlines():
            match = re.match(r"^(#{1,3})\s+(.+?)\s*$", line)
            if match:
                headings.append(match.group(2).strip())
            if len(headings) >= 20:
                break
        heading_text = " | ".join(headings[:20]) if headings else doc["content"][:300]
        lines.append(f"- {doc['path']} ({doc['chars']} chars): {heading_text}")
    return "\n".join(lines)


def _chunk_file_name(chunk: dict[str, Any]) -> str:
    safe = re.sub(r"[^0-9A-Za-z_]+", "_", chunk["chunk_id"]).strip("_")
    return f"{safe}.json"


def _compact_json(data: Any, limit: int = 28_000) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[TRUNCATED]"


def _object_summary(raw: dict[str, Any]) -> str:
    objects = raw.get("objects", {}) or {}
    lines = []
    for name, obj in objects.items():
        props = ", ".join(list((obj.get("properties") or {}).keys())[:20])
        lines.append(
            f"- {name}: {obj.get('summary') or obj.get('description', '')}; "
            f"data_source={obj.get('data_source')}; mutability={obj.get('mutability')}; props={props}"
        )
    return "\n".join(lines) or "无"


def _function_summary(raw: dict[str, Any]) -> str:
    functions = raw.get("functions", {}) or {}
    lines = []
    for name, fn in functions.items():
        writes = ", ".join(fn.get("writes_to") or [])
        lines.append(f"- {name}: {fn.get('summary') or fn.get('description', '')}; writes_to={writes}")
    return "\n".join(lines) or "无"


def _issues_for_section(issues: list[dict[str, Any]], section: str) -> list[dict[str, Any]]:
    matched = []
    for issue in issues:
        path = str(issue.get("path") or "")
        message = str(issue.get("message") or issue.get("issue") or "")
        if path.startswith(section) or f"{section}." in path or section in message:
            matched.append(issue)
    return matched


class DistillerPipeline:
    """Chunked document-to-ontology pipeline based on task-loop modeling."""

    def __init__(
        self,
        docs_dir: str,
        output_dir: str | None = None,
        llm_config: dict[str, Any] | None = None,
    ):
        self.docs_dir = Path(docs_dir).resolve()
        if not self.docs_dir.exists():
            raise FileNotFoundError(f"Docs directory not found: {self.docs_dir}")
        self.output_dir = Path(output_dir).resolve() if output_dir else self.docs_dir.parent.resolve()
        self.state_dir = self.output_dir / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.llm = DistillerLLM(llm_config)

    def run(self, up_to_phase: int = 4) -> Path:
        started = time.time()
        log.info("Ontology Builder starting")
        log.info("docs_dir=%s", self.docs_dir)
        log.info("output_dir=%s", self.output_dir)
        log.info("model=%s", self.llm.model)

        docs_state = self._phase0_documents()
        if up_to_phase <= 0:
            return self.state_dir / "documents.json"

        extractions = self._phase1_extract_chunks(docs_state)
        blueprint = self._phase1_blueprint(docs_state, extractions)
        if up_to_phase <= 1:
            return self.state_dir / "blueprint.json"

        raw = self._phase2_generate_sections(blueprint)
        assembled_yaml = self._write_assembled(raw)
        if up_to_phase <= 2:
            return self.state_dir / "assembled.yaml"

        review = self._phase3_review_sections(raw)
        if up_to_phase <= 3:
            return self.state_dir / "review.json"

        final_yaml = self._phase4_fix_sections(raw, review)
        output_path = self.output_dir / "ontology.yaml"
        output_path.write_text(final_yaml, encoding="utf-8")

        elapsed = time.time() - started
        write_json(self.state_dir / "generation_log.json", {
            "elapsed_seconds": round(elapsed, 2),
            "model": self.llm.model,
            "doc_count": docs_state["doc_count"],
            "chunk_count": docs_state.get("chunk_count", 0),
            "total_doc_chars": docs_state["total_chars"],
            "llm_usage": self.llm.usage,
            "call_log": self.llm.call_log,
            "output_path": str(output_path),
        })
        log.info("Ontology Builder complete in %.1fs: %s", elapsed, output_path)
        log.info(self.llm.usage_summary())
        return output_path

    def _phase0_documents(self) -> dict[str, Any]:
        path = self.state_dir / "documents.json"
        if path.exists():
            log.info("Phase 0: using cached documents.json")
            return read_json(path)
        log.info("Phase 0: reading and chunking documents")
        docs = read_documents(self.docs_dir)
        state = documents_state(docs)
        write_json(path, state)
        return state

    def _phase1_extract_chunks(self, docs_state: dict[str, Any]) -> list[dict[str, Any]]:
        out_dir = self.state_dir / "extractions"
        out_dir.mkdir(parents=True, exist_ok=True)
        chunks = docs_state.get("chunks") or []
        if not chunks:
            raise ValueError("documents.json has no chunks")
        system = CHUNK_EXTRACT_SYSTEM.format(modeling_guide=read_modeling_guide())
        document_map = _document_map(docs_state)
        results = []
        log.info("Phase 1a: extracting modeling candidates from %d chunks", len(chunks))
        for chunk in chunks:
            path = out_dir / _chunk_file_name(chunk)
            if path.exists():
                results.append(read_json(path))
                continue
            log.info("  extracting %s (%d chars)", chunk["chunk_id"], chunk["chars"])
            result = self.llm.call_json(
                system,
                CHUNK_EXTRACT_USER.format(
                    document_map=document_map,
                    chunk_id=chunk["chunk_id"],
                    doc_path=chunk["doc_path"],
                    title=chunk["title"],
                    content=chunk["content"],
                ),
                temperature=0.05,
            )
            write_json(path, result)
            results.append(result)
        write_json(self.state_dir / "extractions.json", results)
        return results

    def _phase1_blueprint(self, docs_state: dict[str, Any], extractions: list[dict[str, Any]]) -> dict[str, Any]:
        path = self.state_dir / "blueprint.json"
        if path.exists():
            log.info("Phase 1b: using cached blueprint.json")
            return read_json(path)
        log.info("Phase 1b: synthesizing global task-loop blueprint")
        blueprint = self.llm.call_json(
            BLUEPRINT_SYSTEM,
            BLUEPRINT_USER.format(
                document_map=_document_map(docs_state),
                extractions=_compact_json(extractions, limit=34_000),
            ),
            temperature=0.05,
        )
        write_json(path, blueprint)
        return blueprint

    def _phase2_generate_sections(self, blueprint: dict[str, Any]) -> dict[str, Any]:
        path = self.state_dir / "sections.json"
        if path.exists():
            log.info("Phase 2: using cached sections.json")
            return read_json(path)
        log.info("Phase 2: generating ontology by section")
        system = SECTION_SYSTEM.format(metamodel_spec=read_metamodel_spec())
        blueprint_text = _compact_json(blueprint, limit=30_000)

        objects_yaml = self.llm.call(
            system,
            OBJECTS_USER.format(blueprint=blueprint_text),
            temperature=0.02,
        )
        raw = _load_yaml(objects_yaml)
        raw.setdefault("links", {})
        raw.setdefault("functions", {})
        raw.setdefault("rules", {})
        raw.setdefault("workflows", {})
        raw["name"] = _snake(str(raw.get("name") or blueprint.get("domain_name") or "new_domain"))
        raw.setdefault("description", str(blueprint.get("domain_description") or ""))
        if not raw.get("objects"):
            raise ValueError("Object section generation produced no objects")

        for section in SECTIONS:
            section_yaml = self.llm.call(
                system,
                SECTION_USER.format(
                    section=section,
                    object_summary=_object_summary(raw),
                    function_summary=_function_summary(raw),
                    blueprint=blueprint_text,
                ),
                temperature=0.02,
            )
            section_raw = _load_yaml(section_yaml)
            raw[section] = section_raw.get(section) or {}
        write_json(path, raw)
        return raw

    def _write_assembled(self, raw: dict[str, Any]) -> str:
        normalized = self._normalize(raw)
        yaml_text = _dump_yaml(normalized)
        (self.state_dir / "assembled.yaml").write_text(yaml_text, encoding="utf-8")
        return yaml_text

    def _phase3_review_sections(self, raw: dict[str, Any]) -> dict[str, Any]:
        path = self.state_dir / "review.json"
        if path.exists():
            log.info("Phase 3: using cached review.json")
            return read_json(path)
        log.info("Phase 3: reviewing ontology by section")
        reviews: dict[str, Any] = {"sections": {}, "issues": []}
        context = self._review_context(raw)
        for section in ("objects", *SECTIONS):
            yaml_text = _dump_yaml({section: raw.get(section) or {}})
            review = self.llm.call_json(
                REVIEW_SECTION_SYSTEM,
                REVIEW_SECTION_USER.format(
                    section=section,
                    context=context,
                    yaml_text=yaml_text,
                ),
                temperature=0,
            )
            reviews["sections"][section] = review
            reviews["issues"].extend(review.get("issues") or [])
        reviews["summary"] = f"{len(reviews['issues'])} section issues"
        write_json(path, reviews)
        return reviews

    def _phase4_fix_sections(self, raw: dict[str, Any], review: dict[str, Any]) -> str:
        log.info("Phase 4: fixing ontology by section")
        fixed = dict(raw)
        context = self._review_context(raw)
        issues = review.get("issues") or []
        for section in ("objects", *SECTIONS):
            section_issues = _issues_for_section(issues, section)
            if not section_issues:
                continue
            log.info("  fixing %s (%d issues)", section, len(section_issues))
            yaml_text = _dump_yaml({section: fixed.get(section) or {}})
            result = self.llm.call(
                FIX_SECTION_SYSTEM,
                FIX_SECTION_USER.format(
                    section=section,
                    context=context,
                    yaml_text=yaml_text,
                    issues=json.dumps(section_issues, ensure_ascii=False, indent=2),
                ),
                temperature=0,
            )
            section_raw = _load_yaml(result)
            fixed[section] = section_raw.get(section) or {}

        normalized = self._normalize(fixed)
        final_yaml = _dump_yaml(normalized)
        Ontology.model_validate(yaml.safe_load(final_yaml))
        (self.state_dir / "reviewed.yaml").write_text(final_yaml, encoding="utf-8")
        write_json(self.state_dir / "fixed_sections.json", normalized)
        return final_yaml

    def _normalize(self, raw: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(raw)
        normalized["name"] = _snake(str(normalized.get("name") or "new_domain"))
        normalized.setdefault("description", "")
        normalized.setdefault("objects", {})
        normalized.setdefault("links", {})
        normalized.setdefault("functions", {})
        normalized.setdefault("rules", {})
        normalized.setdefault("workflows", {})
        if not normalized["objects"]:
            raise ValueError("Generated ontology has no objects")
        return normalized

    def _review_context(self, raw: dict[str, Any]) -> str:
        return (
            "对象摘要:\n"
            f"{_object_summary(raw)}\n\n"
            "函数摘要:\n"
            f"{_function_summary(raw)}"
        )

    def status(self) -> str:
        lines = [f"State directory: {self.state_dir}", ""]
        checks = [
            ("Phase 0 documents", "documents.json"),
            ("Phase 1 chunk extractions", "extractions.json"),
            ("Phase 1 blueprint", "blueprint.json"),
            ("Phase 2 sections", "sections.json"),
            ("Phase 2 assembled ontology", "assembled.yaml"),
            ("Phase 3 review", "review.json"),
            ("Phase 4 reviewed ontology", "reviewed.yaml"),
            ("Generation log", "generation_log.json"),
        ]
        for label, filename in checks:
            path = self.state_dir / filename
            if path.exists():
                lines.append(f"  ok  {label}: {filename} ({path.stat().st_size:,} bytes)")
            else:
                lines.append(f"  --  {label}: not generated")

        output = self.output_dir / "ontology.yaml"
        if output.exists():
            lines.append(f"\nFinal output: {output} ({output.stat().st_size:,} bytes)")
        else:
            lines.append(f"\nFinal output: {output} (not generated)")

        log_path = self.state_dir / "generation_log.json"
        if log_path.exists():
            log_data = read_json(log_path)
            usage = log_data.get("llm_usage", {})
            lines.append(
                f"LLM: {usage.get('calls', 0)} calls, "
                f"{usage.get('prompt_tokens', 0):,} prompt tokens, "
                f"{usage.get('completion_tokens', 0):,} completion tokens"
            )
        return "\n".join(lines)
