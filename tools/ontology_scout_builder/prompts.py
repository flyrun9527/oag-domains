from __future__ import annotations

from pathlib import Path


def read_modeling_guide() -> str:
    path = Path(__file__).resolve().parents[2] / "modeling-guide.md"
    return path.read_text(encoding="utf-8")[:20_000]


def read_metamodel_spec() -> str:
    path = Path(__file__).resolve().parents[2] / "metamodel-spec.md"
    return path.read_text(encoding="utf-8")[:24_000]


SCOUT_SYSTEM = """\
你是 OAG 文档侦察器。你不直接建完整本体，只根据本地索引找值得深入取证的主任务闭环。

要求:
- 输出 JSON，不要 Markdown。
- 只输出用户会让 Agent 端到端完成的主业务闭环，不输出普通概念/术语。
- 不要把“配置约束、搜索、筛选、评分、输出”等流程阶段升格为并列 task_loop；这些应放入 parent loop 的 sub_processes。
- 主闭环应按业务场景、入口或终态分支拆分，例如新装、增容、临时用电、无方案终态；同一入口和同一终态的阶段不要拆成多个 loop。
- 每个主闭环必须有入口、关键子过程、决策点和终态输出线索。
- evidence_section_ids 必须来自索引中的 section_id。
- task_loops 最多 5 个；每个 loop 的 sub_processes 最多 8 个。
"""


SCOUT_USER = """\
本地文档索引:
{document_index}

请输出闭环线索:
{{
  "domain_hint": "snake_case_domain_hint",
  "description_hint": "一句领域说明",
  "task_loops": [
    {{
      "name": "闭环名称",
      "loop_type": "scenario|terminal|exception",
      "trigger": "触发条件",
      "entry": "入口信息/请求/事件",
      "sub_processes": [
        {{"name": "子过程名称", "purpose": "作用", "stage": "validation|decision|search|screening|composition|scoring|output|exception"}}
      ],
      "decision_points": ["关键关口，最多8项"],
      "final_outputs": ["终态输出"],
      "key_terms": ["用于回查证据的关键词"],
      "evidence_section_ids": ["最相关的 section_id，1-4个"],
      "basis": "简短依据"
    }}
  ],
  "open_questions": ["影响识别的问题"]
}}
"""


LOOP_SYSTEM = """\
你是 OAG 闭环建模种子提取器。你只围绕一个主任务闭环和精选证据包提取短建模种子。

建模方法论:
{modeling_guide}

要求:
- 输出 JSON，不要 Markdown。
- 只输出短种子，不输出完整 ontology，不写长 description，不写 basis。
- 每个数组最多 10 项；summary/why/condition 每项最多 40 字。
- 只提取服务于该主闭环执行的对象、函数、规则和 workflow steps。
- 优先识别稳定业务实体作为 fact；不要把搜索结果、校验记录、候选方案、评分结果、推荐结果当成 fact。
- fact 应优先保留业务资源实体、入口实体、配置实体和规则表实体；不要过早压缩成“能力快照、综合视图、搜索批次、筛选结果”。
- 如果一个 fact 概念同时包含多个可独立识别、独立关联、独立约束的稳定资源，应拆成多个 fact 并用 link_seeds 连接。
- 如果一个概念的字段会随某次任务、某个请求或某轮推理上下文变化，它不是稳定 fact 字段；应放入 process_output/final_output。
- 一次执行产生的视图、校验、候选、评分、推荐应放到 process_output 或 final_output，并通过 depends_on 关联 fact。
- 不要生成纯 query(Object,id) getter。
- 业务规则和关口必须能落到 rule、precondition/effect、usage_prompt 或业务函数。
"""


LOOP_USER = """\
任务闭环:
{loop_seed}

精选证据包:
{evidence}

请输出闭环建模种子:
{{
  "loop_name": "闭环名称",
  "object_seeds": [
    {{
      "name": "PascalCase",
      "layer": "fact|rule|entry|process_output|final_output",
      "entity_role": "stable_resource|request|configuration|rule_table|process_artifact|delivery_artifact",
      "summary": "摘要",
      "key_fields": ["字段"],
      "data_source": "external_api|human_confirmed|agent_generated",
      "mutability": "read_only|mutable|append_only",
      "stability_reason": "fact 对象填写稳定实体依据；其他层可为空",
      "depends_on": ["事实对象"]
    }}
  ],
  "function_seeds": [
    {{
      "name": "snake_case",
      "function_type": "get|lookup|business",
      "summary": "摘要",
      "why": "为何需要专用函数",
      "inputs": ["参数"],
      "writes_to": ["对象"],
      "involves_objects": ["对象"]
    }}
  ],
  "rule_seeds": [
    {{
      "name": "snake_case",
      "summary": "摘要",
      "applies_to": ["对象"],
      "result_field": "字段",
      "condition": "条件到结果"
    }}
  ],
  "link_seeds": [
    {{
      "name": "snake_case",
      "source": "Object",
      "target": "Object",
      "relation": "关系说明"
    }}
  ],
  "workflow_seed": {{
    "name": "snake_case",
    "trigger": "触发条件",
    "steps": ["步骤，最多10项"]
  }},
  "open_questions": ["影响该闭环建模的问题"]
}}
"""


BLUEPRINT_SYSTEM = """\
你是 OAG canonical blueprint 归并器。你把多个主闭环的短建模种子合并为紧凑领域蓝图。

要求:
- 输出 JSON，不要 Markdown。
- 合并同义对象、函数、规则和 links。
- 保留主任务闭环，不要把子过程变成 workflow。
- fact 对象必须是稳定业务实体；把搜索结果、校验结果、候选、评分、推荐降级为 process_output/final_output。
- 不要把多个稳定资源合并成一个综合 fact；当资源有独立标识、独立关系或独立约束时，应拆分并补 link。
- 不要把上下文相关字段放到 stable_resource fact；这些字段应进入 process_artifact。
- 明确保留规则表/配置表对象，不要只把所有规则压到 rules section。
- 过程产物和终态输出应保留 depends_on，明确依附哪些 fact 对象。
- 删除纯 query(Object,id) getter 噪音。
- 每项 summary 最多 40 字。
"""


BLUEPRINT_USER = """\
闭环线索:
{loop_seeds}

短建模种子:
{loop_models}

请输出 canonical blueprint:
{{
  "domain_name": "snake_case_name",
  "domain_description": "一句领域说明",
  "task_loops": [
    {{"name": "主闭环", "loop_type": "scenario|terminal|exception", "trigger": "入口", "sub_processes": ["子过程"], "final_outputs": ["输出"]}}
  ],
  "objects": [
    {{"name": "PascalCase", "layer": "fact|rule|entry|process_output|final_output", "entity_role": "stable_resource|request|configuration|rule_table|process_artifact|delivery_artifact", "summary": "摘要", "key_fields": ["字段"], "data_source": "external_api|human_confirmed|agent_generated", "mutability": "read_only|mutable|append_only", "stability_reason": "fact 对象填写稳定实体依据；其他层可为空", "depends_on": ["事实对象"]}}
  ],
  "functions": [
    {{"name": "snake_case", "function_type": "get|lookup|business", "summary": "摘要", "inputs": ["参数"], "writes_to": ["对象"], "involves_objects": ["对象"]}}
  ],
  "rules": [
    {{"name": "snake_case", "summary": "摘要", "applies_to": ["对象"], "result_field": "字段", "condition": "条件到结果"}}
  ],
  "links": [
    {{"name": "snake_case", "source": "Object", "target": "Object", "relation": "关系"}}
  ],
  "workflows": [
    {{"name": "snake_case", "trigger": "触发条件", "steps": ["步骤"]}}
  ],
  "open_questions": ["需要人工确认的问题"]
}}
"""


SECTION_SYSTEM = """\
你是 OAG ontology.yaml 分层生成器。你一次只生成一个 ontology section。

OAG 元模型规范摘录:
{metamodel_spec}

要求:
- 只输出 YAML，不要 Markdown。
- 输出必须是 YAML mapping。
- 对象名 PascalCase；字段、函数、规则、关系、workflow 名 snake_case。
- 不要生成纯 query(Object,id) getter。
"""


OBJECTS_USER = """\
领域提示:
- name: {domain_hint}
- description: {description_hint}

Canonical blueprint:
{blueprint}

请生成 YAML，且只包含顶层 name、description、objects。
要求:
- 对象 properties 要包含关键字段，至少一个 required: true 主键或标识字段。
- fact 对象必须优先表示稳定业务实体；不要把搜索结果、校验记录、候选方案、评分结果、推荐结果写成 fact。
- stable_resource/request 使用 kind: entity；configuration 使用 kind: config；rule_table 使用 kind: rule_table 或 lookup_table。
- process_artifact/delivery_artifact 使用 data_source: agent_generated、mutability: append_only；kind 仍使用元模型允许的 entity，不要创造 process_output、artifact 等非法 kind。
- 若 blueprint 中某个 fact 是多个稳定资源的综合视图，应在 objects 阶段拆分成多个 entity，并在 links 阶段建立关系。
- 稳定资源对象不得包含依赖单次任务上下文的字段；这类字段应移入 agent_generated 过程产物。
- 本阶段可给 source 填保守占位，后续开发人员会根据实际系统补充或调整数据源。
- agent_generated 默认 source.type=runtime_memory。
- process_output/final_output 应在 properties 或 description 中说明依附的 fact 对象。
"""


SECTION_USER = """\
当前已生成的对象摘要:
{object_summary}

当前已生成的函数摘要:
{function_summary}

领域提示:
- name: {domain_hint}
- description: {description_hint}

Canonical blueprint:
{blueprint}

请生成 YAML，且只包含顶层 {section}。
要求:
- links 必须引用已生成 objects。
- functions 必须引用已生成 objects，纯 query(Object,id) 薄包装不要生成。
- rules 必须落到对象字段或明确业务条件。
- workflows 必须引用已生成 functions，步骤应体现主闭环和关键分支。
- review/fix 时若发现 stable_resource 被压缩、上下文字段放错对象、规则表缺失，应优先通过对象/link/rule 调整修复。
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

审查重点:
- objects: stable_resource 是否被综合视图吞并；agent_generated 过程产物是否误作 external_api；规则表/配置表是否缺失。
- links: 被拆分的稳定资源之间是否有清晰关系；过程产物是否通过 links 依附事实对象。
- functions: 是否只暴露承载业务语义的函数；函数读写对象是否完整。
- rules: 规则是否引用存在的对象字段；规则表对象和 rules section 是否互相支持。
- workflows: 主闭环是否引用已生成函数；异常/终态闭环是否避免重复写入已有过程产物。
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
