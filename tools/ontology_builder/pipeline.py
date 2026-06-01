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
    FIX_SECTION_SYSTEM,
    FIX_SECTION_USER,
    LOOP_DISCOVERY_SYSTEM,
    LOOP_DISCOVERY_USER,
    LOOP_MODEL_SYSTEM,
    LOOP_MODEL_USER,
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

        task_loops = self._phase1_discover_loops(docs_state)
        loop_models = self._phase1_model_loops(docs_state, task_loops)
        blueprint = self._phase1_blueprint(docs_state, task_loops, loop_models)
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

    def _phase1_discover_loops(self, docs_state: dict[str, Any]) -> list[dict[str, Any]]:
        task_loops_path = self.state_dir / "task_loops.json"
        if task_loops_path.exists():
            log.info("Phase 1a: using cached task_loops.json")
            return read_json(task_loops_path)
        out_dir = self.state_dir / "loop_discovery"
        out_dir.mkdir(parents=True, exist_ok=True)
        chunks = docs_state.get("chunks") or []
        if not chunks:
            raise ValueError("documents.json has no chunks")
        document_map = _document_map(docs_state)
        per_chunk = []
        log.info("Phase 1a: discovering task loops from %d chunks", len(chunks))
        for chunk in chunks:
            path = out_dir / _chunk_file_name(chunk)
            if path.exists():
                per_chunk.append(read_json(path))
                continue
            log.info("  extracting %s (%d chars)", chunk["chunk_id"], chunk["chars"])
            result = self.llm.call_json(
                LOOP_DISCOVERY_SYSTEM,
                LOOP_DISCOVERY_USER.format(
                    document_map=document_map,
                    chunk_id=chunk["chunk_id"],
                    doc_path=chunk["doc_path"],
                    title=chunk["title"],
                    content=chunk["content"],
                ),
                temperature=0.05,
            )
            write_json(path, result)
            per_chunk.append(result)

        task_loops = []
        for result in per_chunk:
            for loop in result.get("task_loops") or []:
                item = dict(loop)
                item.setdefault("source_chunk_id", result.get("chunk_id", ""))
                task_loops.append(item)
        write_json(task_loops_path, task_loops)
        return task_loops

    def _phase1_model_loops(
        self,
        docs_state: dict[str, Any],
        task_loops: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        path = self.state_dir / "loop_models.json"
        if path.exists():
            log.info("Phase 1b: using cached loop_models.json")
            return read_json(path)
        out_dir = self.state_dir / "loop_models"
        out_dir.mkdir(parents=True, exist_ok=True)
        chunks_by_id = {
            chunk["chunk_id"]: chunk
            for chunk in docs_state.get("chunks") or []
        }
        system = LOOP_MODEL_SYSTEM.format(modeling_guide=read_modeling_guide())
        models = []
        log.info("Phase 1b: modeling %d task loops", len(task_loops))
        for index, loop in enumerate(task_loops, start=1):
            file_path = out_dir / f"loop_{index:03d}.json"
            if file_path.exists():
                models.append(read_json(file_path))
                continue
            evidence = self._loop_evidence(loop, chunks_by_id)
            log.info("  modeling loop %d: %s", index, loop.get("name", ""))
            model = self.llm.call_json(
                system,
                LOOP_MODEL_USER.format(
                    task_loop=json.dumps(loop, ensure_ascii=False, indent=2),
                    evidence=evidence,
                ),
                temperature=0.05,
            )
            write_json(file_path, model)
            models.append(model)
        write_json(path, models)
        return models

    def _phase1_blueprint(
        self,
        docs_state: dict[str, Any],
        task_loops: list[dict[str, Any]],
        loop_models: list[dict[str, Any]],
    ) -> dict[str, Any]:
        path = self.state_dir / "blueprint.json"
        if path.exists():
            log.info("Phase 1c: using cached blueprint.json")
            return read_json(path)
        log.info("Phase 1c: synthesizing global blueprint from loop models")
        blueprint = self.llm.call_json(
            BLUEPRINT_SYSTEM,
            BLUEPRINT_USER.format(
                document_map=_document_map(docs_state),
                task_loops=_compact_json(task_loops, limit=10_000),
                loop_models=_compact_json(loop_models, limit=24_000),
            ),
            temperature=0.05,
        )
        write_json(path, blueprint)
        return blueprint

    def _loop_evidence(
        self,
        loop: dict[str, Any],
        chunks_by_id: dict[str, dict[str, Any]],
    ) -> str:
        chunk_id = loop.get("source_chunk_id")
        chunk = chunks_by_id.get(chunk_id)
        if chunk:
            return (
                f"chunk_id: {chunk['chunk_id']}\n"
                f"document: {chunk['doc_path']}\n"
                f"title: {chunk['title']}\n\n"
                f"{chunk['content']}"
            )
        return "未找到闭环来源 chunk；请只根据 task_loop 本身建模。"

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
        fixed_dir = self.state_dir / "fixed_sections"
        fixed_dir.mkdir(parents=True, exist_ok=True)
        fixed_summary: dict[str, Any] = {"sections": {}, "issues": len(issues)}
        for section in ("objects", *SECTIONS):
            section_issues = _issues_for_section(issues, section)
            if not section_issues:
                fixed_summary["sections"][section] = {
                    "status": "unchanged",
                    "issues": 0,
                }
                continue
            section_path = fixed_dir / f"{section}.yaml"
            if section_path.exists():
                log.info("  using cached fixed section %s", section)
                section_raw = _load_yaml(section_path.read_text(encoding="utf-8"))
                fixed[section] = section_raw.get(section) or {}
                fixed_summary["sections"][section] = {
                    "status": "cached",
                    "issues": len(section_issues),
                    "path": str(section_path),
                }
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
            section_path.write_text(
                _dump_yaml({section: fixed[section]}),
                encoding="utf-8",
            )
            fixed_summary["sections"][section] = {
                "status": "fixed",
                "issues": len(section_issues),
                "path": str(section_path),
            }

        normalized = self._normalize(fixed)
        final_yaml = _dump_yaml(normalized)
        Ontology.model_validate(yaml.safe_load(final_yaml))
        (self.state_dir / "reviewed.yaml").write_text(final_yaml, encoding="utf-8")
        write_json(self.state_dir / "fixed_sections.json", normalized)
        write_json(self.state_dir / "fixed_sections_summary.json", fixed_summary)
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
        _normalize_schema_shapes(normalized)
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
            ("Phase 1 task loops", "task_loops.json"),
            ("Phase 1 loop models", "loop_models.json"),
            ("Phase 1 blueprint", "blueprint.json"),
            ("Phase 2 sections", "sections.json"),
            ("Phase 2 assembled ontology", "assembled.yaml"),
            ("Phase 3 review", "review.json"),
            ("Phase 4 fixed sections", "fixed_sections_summary.json"),
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


def _normalize_schema_shapes(raw: dict[str, Any]) -> None:
    """Coerce common LLM YAML shapes into the strict OAG metamodel shape."""

    for obj in (raw.get("objects") or {}).values():
        props = obj.get("properties")
        if isinstance(props, list):
            obj["properties"] = {
                str(prop.get("name")): {k: v for k, v in prop.items() if k != "name"}
                for prop in props
                if isinstance(prop, dict) and prop.get("name")
            }

    for link in (raw.get("links") or {}).values():
        join = link.get("join")
        if isinstance(join, list):
            merged_join: dict[str, Any] = {}
            for item in join:
                if isinstance(item, dict):
                    merged_join.update(item)
            join = merged_join
            link["join"] = join
        if isinstance(join, dict):
            link["join"] = {
                str(key): _stringify_join_value(value)
                for key, value in join.items()
            }

    for fn in (raw.get("functions") or {}).values():
        params = fn.get("params")
        if isinstance(params, list):
            fn["params"] = {
                str(param.get("name")): {k: v for k, v in param.items() if k != "name"}
                for param in params
                if isinstance(param, dict) and param.get("name")
            }

    for rule in (raw.get("rules") or {}).values():
        conditions = rule.get("conditions")
        if isinstance(conditions, list):
            rule["conditions"] = [
                _normalize_rule_condition(condition)
                for condition in conditions
                if isinstance(condition, dict)
            ]


def _stringify_join_value(value: Any) -> str:
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _normalize_rule_condition(condition: dict[str, Any]) -> dict[str, Any]:
    if condition.get("field"):
        return condition

    normalized = {
        "field": "__compound__",
        "operator": "all" if "all" in condition else "any" if "any" in condition else "eq",
        "value": condition.get("all", condition.get("any", condition.get("value"))),
        "result": condition.get("result"),
    }
    if normalized["value"] is None:
        normalized["value"] = {
            key: value
            for key, value in condition.items()
            if key not in {"result", "priority"}
        }
    return normalized
