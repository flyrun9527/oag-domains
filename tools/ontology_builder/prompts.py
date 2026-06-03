from __future__ import annotations

from pathlib import Path


def read_modeling_guide() -> str:
    path = Path(__file__).resolve().parents[2] / "modeling-guide.md"
    return path.read_text(encoding="utf-8")


def read_metamodel_spec() -> str:
    path = Path(__file__).resolve().parents[2] / "metamodel-spec.md"
    text = path.read_text(encoding="utf-8")
    return text[:24_000]


LOOP_DISCOVERY_SYSTEM = """\
你是 OAG 任务闭环发现器。你的唯一任务是从业务文档中找出用户会让 Agent 完成的业务闭环。

不要输出对象清单、函数清单、规则清单或术语表。此阶段只找闭环。

一个任务闭环必须包含:
- trigger: 用户或外部事件如何启动任务
- entry: 任务入口信息/请求/事件
- steps: 从入口到输出的关键步骤
- decision_points: 必须/不得/通过后/不通过则/审批后/复核后等决策关口
- final_outputs: 任务结束时交付什么结果

输出 JSON，不要 Markdown。
"""


LOOP_DISCOVERY_USER = """\
文档地图:
{document_map}

当前 chunk:
- chunk_id: {chunk_id}
- document: {doc_path}
- title: {title}

内容:
{content}

请只输出当前 chunk 明确支持的任务闭环:
{{
  "chunk_id": "{chunk_id}",
  "task_loops": [
    {{
      "name": "闭环名称",
      "trigger": "触发条件",
      "entry": "入口信息/请求/事件",
      "steps": ["关键步骤，5-9项以内"],
      "decision_points": ["关键关口，最多8项"],
      "final_outputs": ["终态输出"],
      "basis": "简短依据，最多80字"
    }}
  ],
  "open_questions": ["仅记录影响闭环识别的问题"]
}}
"""


LOOP_MODEL_SYSTEM = """\
你是 OAG 闭环建模器。你只围绕一个已识别的任务闭环建模。

建模方法论:
{modeling_guide}

要求:
- 只提取服务于该闭环执行的对象、函数、规则和 workflow 步骤。
- 优先识别稳定业务实体作为 fact_objects；不要把搜索结果、校验结果、候选方案、评分结果当成事实对象。
- 过程视图或一次执行产生的记录应放入 process_outputs/final_outputs，并说明其依附的事实对象。
- 不要输出普通术语表。
- 不要输出纯 query(Object,id) getter，除非它封装跨对象、空间、状态过滤或业务口径。
- 每类候选保持精简，优先业务关口和过程产物。

输出 JSON，不要 Markdown。
"""


LOOP_MODEL_USER = """\
任务闭环:
{task_loop}

相关文档片段:
{evidence}

请为这个闭环输出建模材料:
{{
  "loop_name": "闭环名称",
  "entry_objects": [
    {{"name": "PascalCase", "summary": "摘要", "key_fields": ["字段"], "basis": "依据"}}
  ],
  "fact_objects": [
    {{"name": "PascalCase", "summary": "稳定业务实体摘要", "key_fields": ["稳定标识/状态/所属关系字段"], "stability_reason": "为什么它是稳定业务实体", "basis": "依据"}}
  ],
  "rule_objects": [
    {{"name": "PascalCase", "summary": "摘要", "key_fields": ["字段"], "basis": "依据"}}
  ],
  "process_outputs": [
    {{"name": "PascalCase", "summary": "一次执行产生的中间产物", "key_fields": ["字段"], "depends_on": ["事实对象"], "basis": "依据"}}
  ],
  "final_outputs": [
    {{"name": "PascalCase", "summary": "可交付终态输出", "key_fields": ["字段"], "depends_on": ["事实对象/过程产物"], "basis": "依据"}}
  ],
  "gateways": [
    {{"name": "关口名称", "kind": "validation|approval|review|threshold|state_transition|sla", "deterministic_boundary": "应下沉到哪里", "basis": "依据"}}
  ],
  "functions": [
    {{"name": "snake_case", "function_type": "get|lookup|business", "why_exposed": "为何需要专用函数", "inputs": ["参数"], "writes_to": ["对象"], "basis": "依据"}}
  ],
  "rules": [
    {{"name": "snake_case", "applies_to": ["对象"], "result_field": "字段", "conditions_summary": "条件到结果", "basis": "依据"}}
  ],
  "workflow": {{
    "name": "snake_case",
    "trigger": "触发条件",
    "steps": ["步骤"]
  }},
  "open_questions": ["影响该闭环建模的问题"]
}}
"""


BLUEPRINT_SYSTEM = """\
你是 OAG 领域蓝图综合器。你要把多个闭环建模结果综合成一份领域级 blueprint。

你必须:
- 合并多个闭环共享的事实对象和规则对象。
- fact 对象必须优先表示稳定业务实体；搜索结果、校验结果、候选、评分、推荐不得合并成 fact。
- 对不能作为稳定业务实体但仍有执行价值的概念，降级为 process_output 或 final_output。
- 保留每个闭环独有的过程产物、终态输出和 workflow。
- 合并同义函数，删除纯 query(Object,id) getter 噪音。
- 让关口能够下沉到对象、函数、规则、preconditions/effects 或 usage_prompt。
- 输出紧凑 JSON，不要复述长依据。

输出 JSON，不要 Markdown。
"""


BLUEPRINT_USER = """\
文档地图:
{document_map}

任务闭环:
{task_loops}

逐闭环建模结果:
{loop_models}

请综合为领域建模蓝图 JSON:
{{
  "domain_name": "snake_case_name",
  "domain_description": "一句领域说明",
  "task_loops": [
    {{"name": "闭环", "trigger": "入口", "steps": ["步骤"], "final_outputs": ["输出"]}}
  ],
  "object_candidates": [
    {{"name": "PascalCase", "layer": "fact|rule|entry|process_output|final_output", "summary": "一行摘要", "key_fields": ["字段"], "stability_reason": "fact 对象填写稳定实体依据；其他层可为空", "depends_on": ["事实对象"]}}
  ],
  "gateways": [
    {{"name": "关口", "kind": "validation|approval|review|threshold|state_transition|sla", "suggested_object": "对象", "suggested_function": "函数", "deterministic_boundary": "落地点"}}
  ],
  "function_candidates": [
    {{"name": "snake_case", "function_type": "get|lookup|business", "why_exposed": "原因", "inputs": ["参数"], "writes_to": ["对象"]}}
  ],
  "rule_candidates": [
    {{"name": "snake_case", "applies_to": ["对象"], "result_field": "字段", "conditions_summary": "条件到结果"}}
  ],
  "workflow_candidates": [
    {{"name": "snake_case", "trigger": "触发条件", "steps": ["步骤"]}}
  ],
  "open_questions": ["需要人工确认的问题"]
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
- fact 对象必须优先是稳定业务实体；不要把搜索结果、校验记录、候选方案、评分结果、推荐结果写成 fact。
- 本阶段可给 source 填保守占位，后续开发人员会根据实际系统补充或调整数据源。
- rule/entry 种子数据可使用保守占位。
- agent_generated 过程产物/终态输出默认 source.type=runtime_memory。
- process_output/final_output 应通过 properties 或 description 说明依附哪些 fact 对象。
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
