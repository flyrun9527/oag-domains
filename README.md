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
    __init__.py          # register()，按需注册 resolver / adapter / 业务函数
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
2. 注册内置 `json_file` 和 `sqlite_table` adapter。
3. 创建 `ObjectRepository` 作为统一对象数据入口。
4. 导入 `functions/__init__.py`。
5. 调用 `register(registry, repository, ontology)` 注册 resolver、adapter 和业务函数。

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

对象实例数据不再默认来自 OAG runtime 创建的本地表。每个对象应通过
`objects.<name>.source` 声明数据来源。`ObjectRepository` 会根据 `source.type` 把
`query`、`count`、`query_links`、`search` 和 `mutate` 等工具路由到对应 adapter 或
resolver。

若希望不导入 SQLite、每次查询直接读 JSON 文件，可以声明内置 `json_file` adapter：

```yaml
Substation:
  source:
    type: json_file
    id_field: substation_id
    config:
      path: data/substation.json
  mutability: read_only
  properties:
    substation_id: {type: str, required: true}
    name: {type: str}
```

若数据已经在 SQLite 数据库中，可以声明内置 `sqlite_table` adapter。它只访问已有表或
视图，不建表、不导入 JSON：

```yaml
AccountBalance:
  source:
    type: sqlite_table
    id_field: account_id
    table: account_balance_view
    config:
      db_path: data/accounting.db
  mutability: read_only
  properties:
    account_id: {type: str, required: true}
    customer_id: {type: str}
    balance: {type: float}
```

复杂对象可以声明 resolver，让 `query` / `count` / `query_links` 等工具背后走开发者
实现的数据访问逻辑：

```yaml
AssetView:
  source:
    type: resolver
    resolver: asset_view
    id_field: asset_id
  mutability: read_only
  properties:
    asset_id: {type: str, required: true}
    status: {type: str}
```

resolver 适合对象数据并不直接对应一张表的场景，例如手写多表 SQL、聚合 HTTP API、
图算法结果或多个系统组合出的视图。

开发者也可以注册自定义 adapter。比如 `hv_access` 领域为运行期生成的 `AccessPlan`、
`FeederLoadTransfer` 等对象注册了 `runtime_memory` adapter；静态电网和规则数据则用
`json_file` 直接读取 `data/*.json`。

## functions/__init__.py

领域函数包至少需要提供 `register()`。`register()` 接收 `registry`、`repository` 和
`ontology`。其中 `repository` 是对象数据访问入口，不是旧的本地兼容 Store。

```python
class AssetViewResolver:
    def query(self, filters=None, limit=None, **kw):
        # 可以在这里手写 SQL、调用 HTTP API，或组合多个对象来源。
        rows = [{"asset_id": "A1", "status": "ok"}]
        return rows[:limit] if limit else rows


def register(registry, repository, ontology):
    registry.register_resolver("asset_view", AssetViewResolver())

    registry.register(
        "lookup_asset",
        lambda asset_id: repository.query_by_id("Asset", asset_id) or {"error": "not found"},
        ontology.functions["lookup_asset"],
    )
```

约定：

- 注册名必须和 `ontology.yaml` 的 `functions` key 一致。
- Python 函数返回 dict/list 时会被序列化为 JSON。
- 需要访问对象数据时优先通过 `repository`。
- 需要为一类数据源提供复用实现时，用 `registry.register_adapter(source_type, factory)`
  注册 adapter factory。
- 需要为对象提供复杂数据源时，用 `registry.register_resolver(name, resolver)` 注册
  resolver；resolver 至少实现 `query(...)`，可选实现 `count`、`query_by_id`、
  `search_text`、`insert_record`、`update_record`、`delete_record`。
- 写入或有副作用的函数应在 ontology 中声明 `writes_to` 或 `function_type: business`；
  runtime 会结合写入目标对象的 `data_source` 和 `mutability` 判断是否需要用户确认。
- 复杂函数应补充 `usage_prompt`，说明何时调用、调用前置条件和副作用。

## data/

`data/` 下可以存放 JSON、SQLite 数据库或其他领域私有数据文件。JSON 文件通常是对象数组：

```json
[
  {
    "station_id": "S001",
    "name": "乐陵南",
    "use_status": 2
  }
]
```

如果对象声明 `source.type: json_file`，runtime 会在每次查询时读取对应 JSON 文件，并只通过
ontology 中声明的字段向上暴露对象语义。对象的 `source.id_field` 优先作为业务主键；未声明
时，第一个 `required: true` 字段会被视为业务主键。

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
DOMAIN=domains/hv_access uv run oag info
DOMAIN=domains/hv_access uv run oag chat
DOMAIN=domains/hv_access uv run oag serve --port 18000
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
4. 为每个对象选择合适的 `source.type`：`json_file`、`sqlite_table`、`resolver` 或自定义 adapter。
5. 如需本地 JSON 数据，放入 `data/` 并在对象的 `source.config.path` 中引用。
6. 在 `functions/__init__.py` 注册 resolver、adapter 和业务函数。
7. 为复杂函数补充 `usage_prompt`。
8. 运行 `DOMAIN=domains/{name} uv run oag info` 检查加载结果。
9. 添加至少一个根项目测试，覆盖关键查询或业务函数。

## 建模建议

- 给每个对象写清楚 `summary`，这是常驻 prompt 的主要信息。
- 把完整细节放在 `description`、`usage_prompt`、约束和字段说明里，让 Agent 通过
  `inspect` 按需获取。
- 规则尽量写成 `rules`，不要依赖 LLM 自行推理。
- 有副作用的函数必须显式声明 `writes_to` 或 `function_type: business`。
- 外部接口对象建议标记 `data_source: external_api` 和 `mutability: read_only`。
- JSON 快照、规则表和示例数据建议用 `source.type: json_file`。
- 已有 SQLite 表或视图建议用 `source.type: sqlite_table`，数据库 schema 由业务系统或迁移脚本维护。
- 多表视图、外部 API 或算法生成对象建议用 `source.type: resolver`，把数据获取细节封装在
  resolver 内，不要暴露成 LLM 需要理解的多步查询过程。
- Agent 生成或用户确认的数据建议标记 `agent_generated` 或 `human_confirmed`。
- `data_source: agent_generated` 且 `mutability: append_only` 适合作为 Agent 生成的中间产物
  或候选结果，新增写入可由 runtime 直接放行；需要人工确认边界、可修改结果或外部系统写入
  时，应使用更严格的来源和可变性建模。
- workflow 步骤要保持可执行，每一步最好对应一个明确函数或人工决策点。
