from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import yaml

from oag.ontology.schema import Ontology

from domains.tools.ontology_builder.llm import DistillerLLM

from .indexer import (
    build_document_index,
    choose_evidence_sections,
    compact_index_for_prompt,
    evidence_bundle,
    read_json,
    write_json,
)
from .prompts import (
    BLUEPRINT_SYSTEM,
    BLUEPRINT_USER,
    FIX_SECTION_SYSTEM,
    FIX_SECTION_USER,
    LOOP_SYSTEM,
    LOOP_USER,
    OBJECTS_USER,
    REVIEW_SECTION_SYSTEM,
    REVIEW_SECTION_USER,
    SCOUT_SYSTEM,
    SCOUT_USER,
    SECTION_SYSTEM,
    SECTION_USER,
    read_metamodel_spec,
    read_modeling_guide,
)

log = logging.getLogger(__name__)

SECTIONS = ("links", "functions", "rules", "workflows")
SECTION_CACHE_VERSION = "scout_hybrid_resource_entities_v2"


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


def _load_yaml(text: str) -> dict[str, Any]:
    data = yaml.safe_load(_strip_fences(text))
    if not isinstance(data, dict):
        raise ValueError("Generated YAML is not a mapping")
    return data


def _dump_yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


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


def _safe_file_name(value: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff]+", "_", value).strip("_")
    return safe[:80] or "loop"


def _snake(value: str) -> str:
    value = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", value).strip("_")
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return value.lower() or "new_domain"


class ScoutPipeline:
    """Codex-style selective-evidence ontology builder.

    LLM calls are intentionally sequential. The scout builder may optimize
    local indexing/retrieval, but it must not issue concurrent model requests.
    """

    def __init__(
        self,
        docs_dir: str,
        output_dir: str | None = None,
        llm_config: dict[str, Any] | None = None,
        min_llm_interval_seconds: float = 2.0,
    ):
        self.docs_dir = Path(docs_dir).resolve()
        if not self.docs_dir.exists():
            raise FileNotFoundError(f"Docs directory not found: {self.docs_dir}")
        self.output_dir = Path(output_dir).resolve() if output_dir else self.docs_dir.parent.resolve()
        self.state_dir = self.output_dir / "scout_v1_state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.llm = DistillerLLM(llm_config)
        self.started_at = time.time()
        self.min_llm_interval_seconds = min_llm_interval_seconds
        self._last_llm_call_at = 0.0

    def run(self, up_to_phase: float = 5) -> Path:
        index = self.phase0_index()
        if up_to_phase <= 0:
            return self.state_dir / "document_index.json"

        seeds = self.phase1_scout(index)
        if up_to_phase <= 1:
            return self.state_dir / "loop_seeds.json"

        models = self.phase2_model_loops(index, seeds)
        if up_to_phase <= 2:
            return self.state_dir / "loop_models.json"

        blueprint = self.phase25_blueprint(seeds, models)
        if up_to_phase <= 2.5:
            return self.state_dir / "blueprint.json"

        raw = self.phase3_generate_sections(seeds, blueprint)
        assembled_yaml = self._write_assembled(raw)
        if up_to_phase <= 3:
            return self.state_dir / "assembled.yaml"

        review = self.phase4_review_sections(raw)
        if up_to_phase <= 4:
            return self.state_dir / "review.json"

        final_yaml = self.phase5_fix_sections(raw, review)
        output_path = self.output_dir / "ontology_scout.yaml"
        output_path.write_text(final_yaml, encoding="utf-8")
        write_json(self.state_dir / "generation_log.json", {
            "elapsed_seconds": round(time.time() - self.started_at, 2),
            "llm_usage": dict(self.llm.usage),
            "llm_calls": self.llm.call_log,
            "index_sections": index.get("section_count", 0),
            "loop_count": len(seeds.get("task_loops") or []),
            "blueprint_objects": len(blueprint.get("objects") or []),
            "blueprint_functions": len(blueprint.get("functions") or []),
            "output": str(output_path),
        })
        log.info("Scout ontology written to %s", output_path)
        return output_path

    def phase0_index(self) -> dict[str, Any]:
        path = self.state_dir / "document_index.json"
        if path.exists():
            log.info("Phase 0: using cached document_index.json")
            return read_json(path)
        log.info("Phase 0: building local document index")
        index = build_document_index(self.docs_dir)
        write_json(path, index)
        return index

    def phase1_scout(self, index: dict[str, Any]) -> dict[str, Any]:
        path = self.state_dir / "loop_seeds.json"
        if path.exists():
            log.info("Phase 1: using cached loop_seeds.json")
            return read_json(path)
        log.info("Phase 1: scouting task-loop seeds from compact local index")
        result = self._call_json(
            SCOUT_SYSTEM,
            SCOUT_USER.format(document_index=compact_index_for_prompt(index)),
            temperature=0.05,
        )
        result.setdefault("task_loops", [])
        write_json(path, result)
        return result

    def phase2_model_loops(self, index: dict[str, Any], seeds: dict[str, Any]) -> list[dict[str, Any]]:
        path = self.state_dir / "loop_models.json"
        if path.exists():
            log.info("Phase 2: using cached loop_models.json")
            return read_json(path)

        out_dir = self.state_dir / "loop_models"
        out_dir.mkdir(parents=True, exist_ok=True)
        system = LOOP_SYSTEM.format(modeling_guide=read_modeling_guide())
        models = []
        loops = seeds.get("task_loops") or []
        log.info("Phase 2: modeling %d loops with selective evidence", len(loops))
        for index_no, loop_seed in enumerate(loops, start=1):
            file_path = out_dir / f"{index_no:03d}_{_safe_file_name(loop_seed.get('name', 'loop'))}.json"
            if file_path.exists():
                models.append(read_json(file_path))
                continue
            sections = self._evidence_for_loop(index, loop_seed)
            evidence = evidence_bundle(sections, max_chars=18_000)
            log.info("  modeling loop %d: %s (%d evidence sections)", index_no, loop_seed.get("name", ""), len(sections))
            model = self._call_json(
                system,
                LOOP_USER.format(
                    loop_seed=json.dumps(loop_seed, ensure_ascii=False, indent=2),
                    evidence=evidence,
                ),
                temperature=0.05,
            )
            model["_evidence_section_ids"] = [section.get("section_id") for section in sections]
            write_json(file_path, model)
            models.append(model)

        write_json(path, models)
        return models

    def phase25_blueprint(self, seeds: dict[str, Any], models: list[dict[str, Any]]) -> dict[str, Any]:
        path = self.state_dir / "blueprint.json"
        if path.exists():
            log.info("Phase 2.5: using cached blueprint.json")
            return read_json(path)

        log.info("Phase 2.5: merging loop seeds into canonical blueprint")
        blueprint = self._call_json(
            BLUEPRINT_SYSTEM,
            BLUEPRINT_USER.format(
                loop_seeds=_compact_json(seeds.get("task_loops") or [], limit=12_000),
                loop_models=_compact_json(models, limit=22_000),
            ),
            temperature=0.03,
        )
        write_json(path, blueprint)
        return blueprint

    def phase3_generate_sections(self, seeds: dict[str, Any], blueprint: dict[str, Any]) -> dict[str, Any]:
        path = self.state_dir / "sections.json"
        if path.exists() and self._cache_valid("phase3_sections"):
            log.info("Phase 3: using cached sections.json")
            return read_json(path)

        log.info("Phase 3: generating ontology by section")
        system = SECTION_SYSTEM.format(metamodel_spec=read_metamodel_spec())
        blueprint_text = _compact_json(blueprint, limit=28_000)
        domain_hint = _snake(str(blueprint.get("domain_name") or seeds.get("domain_hint") or self.docs_dir.parent.name))
        description_hint = str(blueprint.get("domain_description") or seeds.get("description_hint") or "")

        objects_yaml = self._call(
            system,
            OBJECTS_USER.format(
                domain_hint=domain_hint,
                description_hint=description_hint,
                blueprint=blueprint_text,
            ),
            temperature=0.02,
        )
        raw = _load_yaml(objects_yaml)
        raw.setdefault("links", {})
        raw.setdefault("functions", {})
        raw.setdefault("rules", {})
        raw.setdefault("workflows", {})
        raw["name"] = _snake(str(raw.get("name") or domain_hint or "new_domain"))
        raw.setdefault("description", description_hint)
        if not raw.get("objects"):
            raise ValueError("Object section generation produced no objects")

        for section in SECTIONS:
            section_yaml = self._call(
                system,
                SECTION_USER.format(
                    section=section,
                    object_summary=_object_summary(raw),
                    function_summary=_function_summary(raw),
                    domain_hint=domain_hint,
                    description_hint=description_hint,
                    blueprint=blueprint_text,
                ),
                temperature=0.02,
            )
            section_raw = _load_yaml(section_yaml)
            raw[section] = section_raw.get(section) or {}

        write_json(path, raw)
        self._mark_cache("phase3_sections")
        return raw

    def _write_assembled(self, raw: dict[str, Any]) -> str:
        normalized = self._normalize(raw)
        yaml_text = _dump_yaml(normalized)
        (self.state_dir / "assembled.yaml").write_text(yaml_text, encoding="utf-8")
        return yaml_text

    def phase4_review_sections(self, raw: dict[str, Any]) -> dict[str, Any]:
        path = self.state_dir / "review.json"
        if path.exists() and self._cache_valid("phase4_review"):
            log.info("Phase 4: using cached review.json")
            return read_json(path)
        log.info("Phase 4: reviewing ontology by section")
        reviews: dict[str, Any] = {"sections": {}, "issues": []}
        context = self._review_context(raw)
        for section in ("objects", *SECTIONS):
            yaml_text = _dump_yaml({section: raw.get(section) or {}})
            review = self._call_json(
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
        self._mark_cache("phase4_review")
        return reviews

    def phase5_fix_sections(self, raw: dict[str, Any], review: dict[str, Any]) -> str:
        log.info("Phase 5: fixing ontology by section")
        fixed = dict(raw)
        issues = review.get("issues") or []
        fixed_dir = self.state_dir / "fixed_sections"
        fixed_dir.mkdir(parents=True, exist_ok=True)
        fixed_cache_valid = self._cache_valid("phase5_fixed")
        fixed_summary: dict[str, Any] = {"sections": {}, "issues": len(issues)}
        for section in ("objects", *SECTIONS):
            context = self._review_context(fixed)
            section_issues = _issues_for_section(issues, section)
            if not section_issues:
                fixed_summary["sections"][section] = {
                    "status": "unchanged",
                    "issues": 0,
                }
                continue
            section_path = fixed_dir / f"{section}.yaml"
            if section_path.exists() and fixed_cache_valid:
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
            result = self._call(
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
        self._mark_cache("phase5_fixed")
        return final_yaml

    def _call(self, *args, **kwargs) -> str:
        self._wait_for_llm_slot()
        try:
            return self.llm.call(*args, **kwargs)
        finally:
            self._last_llm_call_at = time.time()

    def _call_json(self, *args, **kwargs) -> dict[str, Any]:
        self._wait_for_llm_slot()
        try:
            return self.llm.call_json(*args, **kwargs)
        finally:
            self._last_llm_call_at = time.time()

    def _wait_for_llm_slot(self) -> None:
        elapsed = time.time() - self._last_llm_call_at
        remaining = self.min_llm_interval_seconds - elapsed
        if remaining > 0:
            log.info("Waiting %.1fs before next LLM call", remaining)
            time.sleep(remaining)

    def _evidence_for_loop(self, index: dict[str, Any], loop_seed: dict[str, Any]) -> list[dict]:
        sections_by_id = {
            section["section_id"]: section
            for section in index.get("sections") or []
        }
        selected = []
        seen = set()
        for section_id in loop_seed.get("evidence_section_ids") or []:
            section = sections_by_id.get(section_id)
            if section and section_id not in seen:
                selected.append(section)
                seen.add(section_id)
        for section in choose_evidence_sections(index, loop_seed):
            section_id = section.get("section_id")
            if section_id not in seen:
                selected.append(section)
                seen.add(section_id)
        return selected[:5]

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

    def _cache_valid(self, name: str) -> bool:
        marker = self.state_dir / f"{name}.version"
        return marker.exists() and marker.read_text(encoding="utf-8").strip() == SECTION_CACHE_VERSION

    def _mark_cache(self, name: str) -> None:
        marker = self.state_dir / f"{name}.version"
        marker.write_text(SECTION_CACHE_VERSION, encoding="utf-8")

    def status(self) -> str:
        lines = [f"Scout builder state: {self.state_dir}"]
        for label, filename in (
            ("Phase 0 index", "document_index.json"),
            ("Phase 1 loop seeds", "loop_seeds.json"),
            ("Phase 2 loop models", "loop_models.json"),
            ("Phase 2.5 blueprint", "blueprint.json"),
            ("Phase 3 sections", "sections.json"),
            ("Phase 3 assembled", "assembled.yaml"),
            ("Phase 4 review", "review.json"),
            ("Phase 5 fixed sections", "fixed_sections_summary.json"),
            ("Phase 5 reviewed", "reviewed.yaml"),
            ("Generation log", "generation_log.json"),
        ):
            path = self.state_dir / filename
            lines.append(f"- {label}: {'done' if path.exists() else 'pending'}")
        log_path = self.state_dir / "generation_log.json"
        if log_path.exists():
            data = read_json(log_path)
            lines.append(f"- elapsed_seconds: {data.get('elapsed_seconds')}")
            usage = data.get("llm_usage") or {}
            lines.append(
                f"- llm: {usage.get('calls', 0)} calls, "
                f"{usage.get('prompt_tokens', 0)} prompt, "
                f"{usage.get('completion_tokens', 0)} completion"
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
