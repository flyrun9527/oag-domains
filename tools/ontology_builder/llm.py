from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from openai import OpenAI

log = logging.getLogger(__name__)


def _builder_env_path() -> Path:
    return Path(__file__).resolve().parents[1] / ".env"


def load_builder_config(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load LLM config from domains/tools/.env, then environment, then overrides."""

    file_values = dotenv_values(_builder_env_path())
    config = {
        "api_key": file_values.get("LLM_API_KEY") or os.getenv("LLM_API_KEY") or "sk-placeholder",
        "api_url": file_values.get("LLM_API_URL") or os.getenv("LLM_API_URL") or "http://localhost:8090/v1",
        "model": file_values.get("LLM_MODEL") or os.getenv("LLM_MODEL") or "qwen3.5-plus",
    }
    if overrides:
        for key, value in overrides.items():
            if value:
                config[key] = value
    return config


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


def _extract_json_text(text: str) -> str:
    text = _strip_fences(text)
    if text.startswith("{") and text.endswith("}"):
        return text
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return text


class DistillerLLM:
    """OpenAI-compatible chat client with config from domains/tools/.env."""

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = load_builder_config(config)
        self.client = OpenAI(
            api_key=self.config["api_key"],
            base_url=self.config["api_url"],
        )
        self.model = self.config["model"]
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
        self._call_log: list[dict[str, Any]] = []

    def call(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        max_retries: int = 3,
        temperature: float = 0.1,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for attempt in range(max_retries):
            try:
                started = time.time()
                response = self.client.chat.completions.create(**payload)
                elapsed = time.time() - started
                text = response.choices[0].message.content or ""

                usage = response.usage
                prompt_tokens = usage.prompt_tokens if usage else 0
                completion_tokens = usage.completion_tokens if usage else 0
                self.usage["prompt_tokens"] += prompt_tokens
                self.usage["completion_tokens"] += completion_tokens
                self.usage["calls"] += 1
                self._call_log.append({
                    "prompt_chars": len(system) + len(user),
                    "completion_chars": len(text),
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "elapsed_seconds": round(elapsed, 2),
                })
                log.info(
                    "LLM call #%d finished: %d chars -> %d chars in %.1fs",
                    self.usage["calls"],
                    len(system) + len(user),
                    len(text),
                    elapsed,
                )
                return text
            except Exception as exc:  # pragma: no cover - network behavior
                last_error = exc
                if attempt >= max_retries - 1:
                    break
                wait = 2 ** attempt
                log.warning("LLM call failed: %s; retrying in %ss", exc, wait)
                time.sleep(wait)
        raise RuntimeError(f"LLM call failed after {max_retries} attempts: {last_error}")

    def call_json(self, system: str, user: str, **kwargs: Any) -> dict[str, Any]:
        text = self.call(system, user, json_mode=True, **kwargs)
        try:
            return json.loads(_extract_json_text(text))
        except json.JSONDecodeError as exc:
            raise ValueError(f"LLM did not return valid JSON: {exc}\n{text[:1000]}") from exc

    def usage_summary(self) -> str:
        return (
            f"LLM usage: {self.usage['calls']} calls, "
            f"{self.usage['prompt_tokens']:,} prompt tokens, "
            f"{self.usage['completion_tokens']:,} completion tokens"
        )

    @property
    def call_log(self) -> list[dict[str, Any]]:
        return list(self._call_log)
