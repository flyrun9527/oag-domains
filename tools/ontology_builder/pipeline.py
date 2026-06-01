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
    FIX_SYSTEM,
    FIX_USER,
    ONTOLOGY_SYSTEM,
    ONTOLOGY_USER,
    REVIEW_SYSTEM,
    REVIEW_USER,
    read_metamodel_spec,
    read_modeling_guide,
)

log = logging.getLogger(__name__)


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


def _doc_prompt(docs_state: dict[str, Any], *, include_content: bool = True) -> str:
    chunks: list[str] = []
    for doc in docs_state["documents"]:
        content = doc["content"] if include_content else doc["content"][:1200]
        chunks.append(
            f"## {doc['path']}\n"
            f"chars: {doc['chars']}\n\n"
            f"{content}"
        )
    return "\n\n---\n\n".join(chunks)


def _summaries(docs_state: dict[str, Any]) -> str:
    lines = []
    for doc in docs_state["documents"]:
        first = " ".join(doc["content"].split())[:500]
        lines.append(f"- {doc['path']} ({doc['chars']} chars): {first}")
    return "\n".join(lines)


def _load_yaml(text: str) -> dict[str, Any]:
    data = yaml.safe_load(_strip_fences(text))
    if not isinstance(data, dict):
        raise ValueError("Generated ontology is not a YAML mapping")
    return data


class DistillerPipeline:
    """Document-to-ontology pipeline based on task-loop modeling."""

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

        blueprint = self._phase1_blueprint(docs_state)
        if up_to_phase <= 1:
            return self.state_dir / "blueprint.json"

        ontology_yaml = self._phase2_generate_ontology(docs_state, blueprint)
        if up_to_phase <= 2:
            return self.state_dir / "assembled.yaml"

        review = self._phase3_review(ontology_yaml)
        if up_to_phase <= 3:
            return self.state_dir / "review.json"

        final_yaml = self._phase4_fix_and_validate(ontology_yaml, review)
        output_path = self.output_dir / "ontology.yaml"
        output_path.write_text(final_yaml, encoding="utf-8")

        elapsed = time.time() - started
        write_json(self.state_dir / "generation_log.json", {
            "elapsed_seconds": round(elapsed, 2),
            "model": self.llm.model,
            "doc_count": docs_state["doc_count"],
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
        log.info("Phase 0: reading documents")
        docs = read_documents(self.docs_dir)
        state = documents_state(docs)
        write_json(path, state)
        return state

    def _phase1_blueprint(self, docs_state: dict[str, Any]) -> dict[str, Any]:
        path = self.state_dir / "blueprint.json"
        if path.exists():
            log.info("Phase 1: using cached blueprint.json")
            return read_json(path)
        log.info("Phase 1: building task-loop modeling blueprint")
        blueprint = self.llm.call_json(
            BLUEPRINT_SYSTEM.format(modeling_guide=read_modeling_guide()),
            BLUEPRINT_USER.format(documents=_doc_prompt(docs_state)),
            temperature=0.1,
        )
        write_json(path, blueprint)
        return blueprint

    def _phase2_generate_ontology(self, docs_state: dict[str, Any], blueprint: dict[str, Any]) -> str:
        path = self.state_dir / "assembled.yaml"
        if path.exists():
            log.info("Phase 2: using cached assembled.yaml")
            return path.read_text(encoding="utf-8")
        log.info("Phase 2: generating ontology.yaml")
        text = self.llm.call(
            ONTOLOGY_SYSTEM.format(metamodel_spec=read_metamodel_spec()),
            ONTOLOGY_USER.format(
                blueprint=json.dumps(blueprint, ensure_ascii=False, indent=2),
                document_summaries=_summaries(docs_state),
            ),
            temperature=0.05,
        )
        yaml_text = _strip_fences(text)
        _load_yaml(yaml_text)
        path.write_text(yaml_text, encoding="utf-8")
        return yaml_text

    def _phase3_review(self, ontology_yaml: str) -> dict[str, Any]:
        path = self.state_dir / "review.json"
        if path.exists():
            log.info("Phase 3: using cached review.json")
            return read_json(path)
        log.info("Phase 3: reviewing generated ontology")
        review = self.llm.call_json(
            REVIEW_SYSTEM,
            REVIEW_USER.format(ontology_yaml=ontology_yaml),
            temperature=0,
        )
        write_json(path, review)
        return review

    def _phase4_fix_and_validate(self, ontology_yaml: str, review: dict[str, Any]) -> str:
        log.info("Phase 4: fixing and validating ontology")
        issues = review.get("issues") or []
        errors = [issue for issue in issues if issue.get("severity") == "error"]
        warnings = [issue for issue in issues if issue.get("severity") == "warning"]

        if errors or warnings:
            fixed = self.llm.call(
                FIX_SYSTEM,
                FIX_USER.format(
                    ontology_yaml=ontology_yaml,
                    issues=json.dumps(issues, ensure_ascii=False, indent=2),
                ),
                temperature=0,
            )
            ontology_yaml = _strip_fences(fixed)

        raw = _load_yaml(ontology_yaml)
        raw.setdefault("links", {})
        raw.setdefault("functions", {})
        raw.setdefault("rules", {})
        raw.setdefault("workflows", {})
        raw["name"] = _snake(str(raw.get("name") or "new_domain"))
        raw.setdefault("description", "")
        if not raw.get("objects"):
            raise ValueError("Generated ontology has no objects")

        normalized = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
        Ontology.model_validate(yaml.safe_load(normalized))
        reviewed_path = self.state_dir / "reviewed.yaml"
        reviewed_path.write_text(normalized, encoding="utf-8")
        return normalized

    def status(self) -> str:
        lines = [f"State directory: {self.state_dir}", ""]
        checks = [
            ("Phase 0 documents", "documents.json"),
            ("Phase 1 blueprint", "blueprint.json"),
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
