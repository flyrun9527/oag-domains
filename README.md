# OAG Domains

`domains` 是 MyPalantir 的领域库子项目。这里保存所有可被 OAG runtime 加载的业务领域：
本体定义、初始化数据、业务函数实现和示例问题。

如果你要理解 `ontology.yaml` 的完整字段、语义和运行时行为，请先看
[metamodel-spec.md](metamodel-spec.md)。本 README 只说明领域仓库如何组织、如何维护、
如何在根项目中加载验证。

## 当前领域

| 领域 | 说明 |
|---|---|
| `drone` | 公路交通应急处置与无人机侦测 |
| `fee` | 高速公路费率管理与路径计费 |
| `hv_access` | 10kV 高压接入方案设计 |
| `icf` | GXLF 激光发射集中管控系统 |

## 领域目录约定

```text
domains/{domain}/
  ontology.yaml          # 领域本体，必需
  data/                  # 初始化数据，按需
  functions/             # Python 业务函数，必需
    __init__.py          # DATA_FILES / FIELD_MAPPINGS / register()
    *.py                 # 具体函数实现
  prompts.json           # 示例问题，按需
  *.md                   # 领域资料或说明，按需
```

领域维护工具放在：

```text
domains/tools/ontology_builder/
```

它不是业务领域，不包含 `ontology.yaml`，不会被根项目挂载为 `/d/{domain}`。

OAG 通过 `oag.ontology.loader.load_domain(domain_dir)` 加载领域：

1. 读取 `ontology.yaml`。
2. 根据 `objects` 创建 SQLite 表。
3. 导入 `functions/__init__.py`。
4. 按 `DATA_FILES` 加载 `data/` 下的 JSON。
5. 调用 `register(registry, store, ontology)` 注册业务函数。

## ontology.yaml 速览

`ontology.yaml` 的一级结构按 OAG 元模型组织：

```yaml
name: fee
description: 高速公路费率管理

objects: {}
links: {}
functions: {}
rules: {}
workflows: {}
```

各层职责：

- `objects`：领域对象、属性、数据来源、可变性和状态约束。
- `links`：对象间关系，用于 `query_links`。
- `functions`：Agent 可调用的业务函数及参数 schema。
- `rules`：确定性规则，由 `apply_rule` 执行。
- `workflows`：多步骤业务流程，由 `start_workflow` 推进。

完整字段说明见 [metamodel-spec.md](metamodel-spec.md)。

## functions/__init__.py

领域函数包至少需要提供 `register()`。如果领域有初始化数据，可以提供
`DATA_FILES` 和 `FIELD_MAPPINGS`。

```python
DATA_FILES = {
    "TollStation": "toll_station.json",
}

FIELD_MAPPINGS = {
    "TollStation": {
        "id": "station_id",
    },
}


def register(registry, store, ontology):
    from .find_path import find_path

    registry.register(
        "find_path",
        lambda en_station_id, ex_station_id, vehicle_type: find_path(
            store,
            en_station_id=en_station_id,
            ex_station_id=ex_station_id,
            vehicle_type=int(vehicle_type),
        ),
        ontology.functions["find_path"],
    )
```

约定：

- 注册名必须和 `ontology.yaml` 的 `functions` key 一致。
- Python 函数返回 dict/list 时会被序列化为 JSON。
- 需要访问数据时优先通过 `store`。
- 写入或有副作用的函数应在 ontology 中声明 `writes_to` 或 `function_type: business`。
- 复杂函数应补充 `usage_prompt`，说明何时调用、调用前置条件和副作用。

## data/

`data/` 下的 JSON 文件由 `DATA_FILES` 引用。文件内容通常是对象数组：

```json
[
  {
    "station_id": "S001",
    "name": "乐陵南",
    "use_status": 2
  }
]
```

加载规则：

- 只会导入 ontology 中声明过的字段。
- 如果表里已经有数据，默认不会重复导入。
- 字段名不一致时使用 `FIELD_MAPPINGS` 映射。
- 对象的第一个 `required: true` 字段会被视为业务主键。

## prompts.json

`prompts.json` 用于 Web 页面展示示例问题。最简单格式是字符串数组：

```json
[
  "查询从乐陵南到乐陵北的收费路径",
  "帮我计算一型客车 MTC 通行费"
]
```

也可以用更结构化的对象数组，前端会按实际字段展示。

## Ontology Builder

`tools/ontology_builder` 是从业务文档生成领域 ontology 的辅助工具。根项目 CLI 保留
`oag distill` 入口：

```bash
uv run oag distill run ./raw_docs --output domains/new_domain --phase 3
uv run oag distill status domains/new_domain/state
```

这个工具依赖 OAG 的元模型 schema 做校验，但不属于在线 Agent runtime。

## 本地检查

在根项目中运行：

```bash
DOMAIN=domains/fee uv run oag info
DOMAIN=domains/fee uv run oag chat
DOMAIN=domains/fee uv run oag serve --port 18000
```

多领域启动：

```bash
uv run oag serve --port 18000
```

根项目测试会加载领域并验证关键函数。如果当前在 `domains/` 目录，先回到根项目：

```bash
cd ..
uv run pytest
```

## 新增领域流程

1. 新建 `domains/{name}/ontology.yaml`。
2. 按 [metamodel-spec.md](metamodel-spec.md) 定义 `objects / links / functions / rules / workflows`。
3. 先确保主键字段有 `required: true`。
4. 如需初始化数据，放入 `data/` 并在 `functions/__init__.py` 声明 `DATA_FILES`。
5. 在 `functions/__init__.py` 实现并注册业务函数。
6. 为复杂函数补充 `usage_prompt`。
7. 运行 `DOMAIN=domains/{name} uv run oag info` 检查加载结果。
8. 添加至少一个根项目测试，覆盖关键查询或业务函数。

## 建模建议

- 给每个对象写清楚 `summary`，这是常驻 prompt 的主要信息。
- 把完整细节放在 `description`、`usage_prompt`、约束和字段说明里，让 Agent 通过
  `inspect` 按需获取。
- 规则尽量写成 `rules`，不要依赖 LLM 自行推理。
- 有副作用的函数必须显式声明 `writes_to` 或 `function_type: business`。
- 外部接口对象建议标记 `data_source: external_api` 和 `mutability: read_only`。
- Agent 生成或用户确认的数据建议标记 `agent_generated` 或 `human_confirmed`。
- workflow 步骤要保持可执行，每一步最好对应一个明确函数或人工决策点。
