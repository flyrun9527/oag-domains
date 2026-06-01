from __future__ import annotations

from pathlib import Path


def read_modeling_guide() -> str:
    path = Path(__file__).resolve().parents[2] / "modeling-guide.md"
    return path.read_text(encoding="utf-8")


def read_metamodel_spec() -> str:
    path = Path(__file__).resolve().parents[2] / "metamodel-spec.md"
    text = path.read_text(encoding="utf-8")
    return text[:24_000]


CHUNK_EXTRACT_SYSTEM = """\
你是 OAG 本体建模素材抽取器。你只处理一个文档 chunk，不做最终设计。

建模方法论:
{modeling_guide}

输出必须是 JSON。不要输出 Markdown。
"""


CHUNK_EXTRACT_USER = """\
全局文档地图:
{document_map}

当前 chunk:
- chunk_id: {chunk_id}
- document: {doc_path}
- title: {title}

内容:
{content}

请只基于当前 chunk 抽取结构化候选材料。不要补全文档没有写的内容。

JSON 结构:
{{
  "chunk_id": "{chunk_id}",
  "task_loops": [
    {{"name": "任务闭环", "trigger": "入口", "steps": ["步骤"], "final_outputs": ["输出"], "basis": "原文依据"}}
  ],
  "object_candidates": [
    {{"name": "PascalCase", "layer": "fact|rule|entry|process_output|final_output", "summary": "摘要", "key_fields": ["字段"], "basis": "原文依据"}}
  ],
  "gateways": [
    {{"name": "关口", "kind": "validation|approval|review|threshold|state_transition|sla", "must_be_deterministic": true, "suggested_object": "对象", "suggested_function": "函数", "basis": "原文依据"}}
  ],
  "function_candidates": [
    {{"name": "snake_case", "kind": "query|lookup|business|gateway", "why_exposed": "为何需要专用函数", "inputs": ["参数"], "writes_to": ["对象"], "basis": "原文依据"}}
  ],
  "rule_candidates": [
    {{"name": "snake_case", "applies_to": ["对象"], "result_field": "字段", "conditions_summary": "条件到结果", "basis": "原文依据"}}
  ],
  "workflow_candidates": [
    {{"name": "snake_case", "trigger": "触发", "steps": ["步骤"], "basis": "原文依据"}}
  ],
  "terms": [
    {{"name": "术语", "meaning": "含义", "basis": "原文依据"}}
  ],
  "open_questions": ["当前 chunk 暴露但无法确定的问题"]
}}
"""


BLUEPRINT_SYSTEM = """\
你是 OAG 本体建模架构师。你要把多个 chunk 的抽取结果综合成领域建模蓝图。

你必须:
- 从任务闭环开始综合，而不是简单合并名词。
- 合并同义/近义对象和函数。
- 找出跨 chunk 的关口依赖和流程顺序。
- 过滤纯 query(Object,id) getter 噪音。
- 保留 open_questions，标注需要人工确认的缺口。

输出必须是 JSON。不要输出 Markdown。
"""


BLUEPRINT_USER = """\
文档地图:
{document_map}

chunk 抽取结果:
{extractions}

请综合为领域建模蓝图 JSON:
{{
  "domain_name": "snake_case_name",
  "domain_description": "一句领域说明",
  "task_loops": [
    {{"name": "任务闭环名称", "trigger": "入口/触发条件", "steps": ["步骤"], "final_outputs": ["输出"], "basis": ["依据"]}}
  ],
  "object_candidates": [
    {{"name": "PascalCase", "layer": "fact|rule|entry|process_output|final_output", "summary": "一行摘要", "key_fields": ["建议字段"], "basis": ["依据"]}}
  ],
  "gateways": [
    {{"name": "关口名称", "kind": "validation|approval|review|threshold|state_transition|sla", "must_be_deterministic": true, "suggested_object": "可选对象", "suggested_function": "可选函数", "basis": ["依据"]}}
  ],
  "function_candidates": [
    {{"name": "snake_case", "kind": "query|lookup|business|gateway", "why_exposed": "为什么不是通用 query", "inputs": ["参数"], "writes_to": ["对象名"], "basis": ["依据"]}}
  ],
  "rule_candidates": [
    {{"name": "snake_case", "applies_to": ["对象名"], "result_field": "字段", "conditions_summary": "条件到结果摘要", "basis": ["依据"]}}
  ],
  "workflow_candidates": [
    {{"name": "snake_case", "trigger": "触发条件", "steps": ["步骤"], "basis": ["依据"]}}
  ],
  "open_questions": ["文档不足或需要人工确认的问题"]
}}
"""


SECTION_SYSTEM = """\
你是 OAG ontology.yaml 分层生成器。你一次只生成一个 ontology section。

OAG 元模型规范摘录:
{metamodel_spec}

通用要求:
- 只输出 YAML，不要 Markdown。
- 输出必须是 YAML mapping。
- 对象名 PascalCase；字段、函数、规则、关系、workflow 名 snake_case。
- 不要生成纯 query(Object,id) 薄包装函数，除非它有跨对象、空间、状态过滤或业务口径。
"""


OBJECTS_USER = """\
建模蓝图:
{blueprint}

请生成 YAML，且只包含顶层 name、description、objects。
要求:
- 每个 object 有 kind、data_source、mutability、source、summary、description、properties。
- 每个 object 至少一个 required: true 的主键字段。
- 事实/规则/入口种子数据默认 source.type=json_file。
- agent_generated 过程产物/终态输出默认 source.type=runtime_memory。
"""


SECTION_USER = """\
当前已生成的对象摘要:
{object_summary}

当前已生成的函数摘要:
{function_summary}

建模蓝图:
{blueprint}

请生成 YAML，且只包含顶层 {section}。
"""


REVIEW_SECTION_SYSTEM = """\
你是 OAG ontology section 审查器。你只审查一个 section。
输出 JSON，不要 Markdown。
"""


REVIEW_SECTION_USER = """\
section: {section}

相关上下文:
{context}

YAML:
{yaml_text}

请输出 JSON:
{{
  "issues": [
    {{"severity": "error|warning", "path": "{section}.xxx", "message": "问题", "suggestion": "建议"}}
  ],
  "summary": "简短总结"
}}
"""


FIX_SECTION_SYSTEM = """\
你是 OAG ontology section 修复器。你只修复一个 section。
只输出修复后的 YAML mapping，不要 Markdown。
"""


FIX_SECTION_USER = """\
section: {section}

相关上下文:
{context}

原始 YAML:
{yaml_text}

需要修复的问题:
{issues}

请只输出包含顶层 {section} 的 YAML。
"""
