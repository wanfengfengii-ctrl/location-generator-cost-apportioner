# 影视外景发电机燃料费用分摊服务

纯后端 HTTP JSON 服务（Python 3.12 + FastAPI）。外景拍摄结束后，把同一台移动
发电机的燃料发票总额（整数分）按各摄制组的 `watts × minutes` 整数权重，用
**最大余数法（Hamilton 分摊）**拆到每个摄制组，保证分摊明细合计**始终严格等于**
发票总额，财务可直接对平。合同另定每组保底/封顶时，`POST /allocate-bounded`
在多轮封顶重算中直接纳入这些约束。读数事后更正时，`POST /adjustments` 对同一发票
总额各跑一次原始版与更正版分摊，直接返回每组可正可负的调账差额（合计恒为零）。

## 分摊算法与不变量

1. 每组权重 `weight = watts × minutes`（整数）；所有组权重之和必须大于零，否则整批拒绝。
2. 每组精确份额为 `total_cents × weight / total_weight`，先**向下取整**得到 `floor_cents`。
3. 剩余分 `leftover = total_cents − Σfloor_cents`（恒满足 `0 ≤ leftover < 组数`），
   逐个分给小数余数较大的组，每组至多 1 分。
4. 余数完全相同时，按 `unit_id` 的 **UTF-8 字节字典序升序**决定先后。
5. 成功响应中每组都带 `weight`、`floor_cents`、`remainder_awarded`（是否获得余分）、
   `final_cents`，且恒有 `Σfinal_cents == total_cents`（响应里 `allocated_cents`
   字段即合计，供会计核对）。

任一非法项（负数、非整数、重复 `unit_id`、权重和为零……）都会**整批拒绝**，不产生
任何部分分摊结果。

## 目录结构

```
app/
  main.py         # FastAPI 应用与路由（POST /allocate、POST /allocate-bounded、POST /adjustments、GET /health）
  allocator.py    # 最大余数法核心逻辑（纯 Python，无框架依赖）
  bounded.py      # 保底/封顶约束下的水填充分摊（多轮锁定封顶 + 最大余数收尾，纯 Python）
  adjustments.py  # 调账编排：两版读数各跑一次分摊，按 unit_id 合并出差额
  schemas.py      # 请求/响应模型（严格非负整数校验）
  errors.py       # 统一错误信封，所有错误返回可定位字段
tests/
  test_api.py          # HTTP 验收测试（可打真实服务或进程内 TestClient）
  test_allocator.py    # 分摊逻辑单元测试（含随机化不变量校验）
  test_bounded.py      # 保底/封顶分摊单元测试（含随机化不变量与独立参照实现）
  test_adjustments.py  # 调账编排单元测试（含集合并发、整批拒绝校验）
Dockerfile           # python:3.12-slim 单镜像
compose.yaml         # api 服务 + 一次性 verify 验收服务
requirements.txt / requirements-dev.txt
pytest.ini
```

## 运行方式（Docker Compose）

`docker compose up` 只运行 API 服务；宿主端口由环境变量 `API_PORT` 覆盖（默认 8000）。

```bash
# 构建并后台启动 API（默认 http://localhost:8000）
docker compose up -d --build api

# 用 API_PORT 覆盖宿主端口
API_PORT=9000 docker compose up -d --build api

# 运行一次性 pytest 验收服务 verify（打真实 API 容器，跑完即退出，
# 退出码即测试结果；会自动先等 api 健康检查通过）
docker compose --profile verify run --rm verify

# 清理
docker compose down
```

## 本地开发

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

uvicorn app.main:app --reload --port 8000   # 交互文档: http://localhost:8000/docs

pytest -v                                        # 进程内 TestClient 模式
API_BASE_URL=http://localhost:8000 pytest -v     # 打真实运行的服务
```

## API 说明

### `POST /allocate`

请求体：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `total_cents` | int | 非负整数（严格校验：字符串、布尔、浮点一律拒绝） |
| `units` | array | 至少 1 个元素 |
| `units[].unit_id` | string | 非空，批内唯一 |
| `units[].watts` | int | 非负整数 |
| `units[].minutes` | int | 非负整数 |

成功响应 `200`：

| 字段 | 含义 |
| --- | --- |
| `total_cents` / `total_weight` | 发票总额（分）/ 权重总和 |
| `allocated_cents` | 分摊合计，恒等于 `total_cents` |
| `remainder_cents_distributed` | 本轮发出去的余分总数 |
| `allocations[].weight` | 该组权重 `watts × minutes` |
| `allocations[].floor_cents` | 精确份额向下取整值 |
| `allocations[].remainder_awarded` | 是否获得 1 分余分 |
| `allocations[].final_cents` | 最终金额（整数分） |

示例：

```bash
curl -s -X POST http://localhost:${API_PORT:-8000}/allocate \
  -H 'Content-Type: application/json' \
  -d '{
        "total_cents": 10000,
        "units": [
          {"unit_id": "lighting", "watts": 2000, "minutes": 180},
          {"unit_id": "camera",   "watts": 800,  "minutes": 150},
          {"unit_id": "vfx",      "watts": 500,  "minutes": 96}
        ]
      }'
```

```json
{
  "total_cents": 10000,
  "total_weight": 528000,
  "allocated_cents": 10000,
  "remainder_cents_distributed": 1,
  "allocations": [
    {"unit_id": "lighting", "watts": 2000, "minutes": 180, "weight": 360000,
     "floor_cents": 6818, "remainder_awarded": false, "final_cents": 6818},
    {"unit_id": "camera", "watts": 800, "minutes": 150, "weight": 120000,
     "floor_cents": 2272, "remainder_awarded": true, "final_cents": 2273},
    {"unit_id": "vfx", "watts": 500, "minutes": 96, "weight": 48000,
     "floor_cents": 909, "remainder_awarded": false, "final_cents": 909}
  ]
}
```

上例中三组精确份额为 6818.18…、2272.72…、909.09… 分；向下取整后剩 1 分，
camera 组小数余数最大，获得该余分。合计 6818 + 2273 + 909 = 10000，与发票对平。

### `POST /allocate-bounded`

摄制组合同可能约定每组的燃料费**保底额**（`minimum_cents`）与**封顶额**
（`maximum_cents`）。本端点在用量分摊中直接纳入这些约束：

1. 每组先拿到保底额，可分配余额 = `total_cents − Σminimum_cents`；
2. 余额只在**权重为正且未封顶**的组间按当前权重计算精确增量
   （`余额 × 权重 / 当前权重和`，全程整数交叉相乘，无浮点误差）；
3. 每轮把所有「增量 ≥ 自身剩余容量」的组一起锁定到封顶额，扣除这些容量后
   用剩余组重算权重，循环直至某轮没有新锁定项；
4. 最后在幸存组间沿用 `/allocate` 的**最大余数法**分完整数分，余数同分时按
   `unit_id` 的 UTF-8 字节序升序决胜。

零权重组不参与余额分配，只能取得保底额（其 `maximum_cents` 不抬高可分配上限）。
响应保持输入顺序，逐组返回 `weight`、`minimum_cents`、`maximum_cents`、
`final_cents` 与 `amount_basis`（`minimum` / `weighted` / `maximum`，标明最终金额
由保底、按权重还是封顶决定）。恒有每组 `minimum_cents ≤ final_cents ≤ maximum_cents`
且 `Σfinal_cents == total_cents`。

请求体：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `total_cents` | int | 非负整数（与 `/allocate` 相同的严格整数校验） |
| `units` | array | 至少 1 个元素 |
| `units[].unit_id` | string | 非空，批内唯一（重复时沿用 `/allocate` 的 `DUPLICATE_UNIT_ID` 定位错误） |
| `units[].watts` / `units[].minutes` | int | 非负整数 |
| `units[].minimum_cents` / `units[].maximum_cents` | int | 非负整数，且每组必须满足 `minimum_cents ≤ maximum_cents` |

整批拒绝规则（校验按序分阶段，任何不可行请求均不返回部分结果）：

- `unit_id` 重复 → `400 DUPLICATE_UNIT_ID`；
- 存在上下限倒置 → `400 BOUNDS_INVERTED`，**一次返回全部**倒置项，按输入位置
  排序定位；
- `total_cents < Σminimum_cents` 或
  `total_cents > Σ(正权重组 maximum_cents) + Σ(零权重组 minimum_cents)`
  → `400 INFEASIBLE_BOUNDS`。

成功响应 `200`：

| 字段 | 含义 |
| --- | --- |
| `total_cents` / `total_weight` | 发票总额（分）/ 权重总和 |
| `allocated_cents` | 分摊合计，恒等于 `total_cents` |
| `remainder_cents_distributed` | 最后幸存组阶段发出的余分总数 |
| `allocations[].weight` | 该组权重 `watts × minutes` |
| `allocations[].minimum_cents` / `maximum_cents` | 合同保底额 / 封顶额 |
| `allocations[].final_cents` | 最终金额（整数分，落在区间内） |
| `allocations[].amount_basis` | `minimum` / `weighted` / `maximum` |

示例（等额权重、封顶 20/40/100，总额 100：前两组在两轮重算中先后锁定封顶，
余额 40 全归第三组）：

```bash
curl -s -X POST http://localhost:${API_PORT:-8000}/allocate-bounded \
  -H 'Content-Type: application/json' \
  -d '{
        "total_cents": 100,
        "units": [
          {"unit_id": "a", "watts": 1, "minutes": 1, "minimum_cents": 0, "maximum_cents": 20},
          {"unit_id": "b", "watts": 1, "minutes": 1, "minimum_cents": 0, "maximum_cents": 40},
          {"unit_id": "c", "watts": 1, "minutes": 1, "minimum_cents": 0, "maximum_cents": 100}
        ]
      }'
```

```json
{
  "total_cents": 100,
  "total_weight": 3,
  "allocated_cents": 100,
  "remainder_cents_distributed": 0,
  "allocations": [
    {"unit_id": "a", "weight": 1, "minimum_cents": 0, "maximum_cents": 20,
     "final_cents": 20, "amount_basis": "maximum"},
    {"unit_id": "b", "weight": 1, "minimum_cents": 0, "maximum_cents": 40,
     "final_cents": 40, "amount_basis": "maximum"},
    {"unit_id": "c", "weight": 1, "minimum_cents": 0, "maximum_cents": 100,
     "final_cents": 40, "amount_basis": "weighted"}
  ]
}
```

### `POST /adjustments`

拍摄结束后若摄制组更正了功率或使用时长，会计直接取得两版分摊的**调账差额**，
无需手工比对两份 `/allocate` 结果。服务对同一发票总额分别用原始读数
（`original_units`）和更正读数（`corrected_units`）各跑一次相同的最大余数法
分摊，再按 `unit_id` 合并：

- `adjustment_cents = corrected − original`，**可正、可负、可为零**；
- 两版拆分的都是同一张发票，所以 `Σadjustment_cents` **恒为 0**，响应中的
  `total_adjustment_cents` 即供核对的合计值；
- `adjustments` 顺序与 `original_units` 一致（忽略更正数组自身的排列）。

请求体：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `total_cents` | int | 非负整数（与 `/allocate` 相同的严格整数校验） |
| `original_units` / `corrected_units` | array | 各至少 1 个元素，元素结构与 `/allocate` 的 `units[]` 相同 |
| 两组读数的 `unit_id` | — | 两组必须包含**完全相同**的组号集合，且组内各自唯一 |

整批拒绝规则（不生成任何部分调账单）：

- 任一数组内 `unit_id` 重复 → `400 DUPLICATE_UNIT_ID`，定位到重复出现的元素；
- 两组组号集合不一致（缺失或额外）→ `400 UNIT_SET_MISMATCH`，对每个失配元素
  分别给出定位（缺失的一方标在原数组，额外的一方标在更正数组）；
- 任一版本权重总和为零 → `400 ZERO_TOTAL_WEIGHT`，`fields[].loc` 指向对应的
  `original_units` 或 `corrected_units`。

成功响应 `200`：

| 字段 | 含义 |
| --- | --- |
| `total_cents` | 两版共用的发票总额（分） |
| `original_total_weight` / `corrected_total_weight` | 两版权重总和 |
| `total_adjustment_cents` | 调账合计，恒为 `0`（核对值） |
| `adjustments[].original_cents` | 原始读数下该组分摊金额 |
| `adjustments[].corrected_cents` | 更正读数下该组分摊金额 |
| `adjustments[].adjustment_cents` | 调账差额（更正 − 原始，可正可负） |

示例：

```bash
curl -s -X POST http://localhost:${API_PORT:-8000}/adjustments \
  -H 'Content-Type: application/json' \
  -d '{
        "total_cents": 10,
        "original_units": [
          {"unit_id": "a", "watts": 1, "minutes": 1},
          {"unit_id": "b", "watts": 1, "minutes": 1}
        ],
        "corrected_units": [
          {"unit_id": "a", "watts": 2, "minutes": 1},
          {"unit_id": "b", "watts": 1, "minutes": 1}
        ]
      }'
```

```json
{
  "total_cents": 10,
  "original_total_weight": 2,
  "corrected_total_weight": 3,
  "total_adjustment_cents": 0,
  "adjustments": [
    {"unit_id": "a", "original_cents": 5, "corrected_cents": 7, "adjustment_cents": 2},
    {"unit_id": "b", "original_cents": 5, "corrected_cents": 3, "adjustment_cents": -2}
  ]
}
```

读数完全未变化时，每个 `adjustment_cents` 均为 `0`，合计仍为 `0`。

### 错误响应

所有错误（参数校验、业务规则、404/405、500）都是同一信封，`detail.fields[].loc`
按 JSON 路径定位到出错字段（数组下标为整数）：

```json
{
  "detail": {
    "code": "DUPLICATE_UNIT_ID",
    "message": "unit_id values must be unique; repeated: 'a'",
    "fields": [
      {"loc": ["body", "units", 2, "unit_id"],
       "message": "duplicate unit_id 'a'; first occurrence is units[0].unit_id"}
    ]
  }
}
```

| HTTP | code | 触发条件 |
| --- | --- | --- |
| 422 | `VALIDATION_ERROR` | 缺字段、负数、浮点/字符串/布尔冒充整数、空 `unit_id`、空 `units`/读数数组、多余字段、JSON 语法错误等（`/adjustments` 同样适用） |
| 400 | `DUPLICATE_UNIT_ID` | `unit_id` 重复（每个重复出现的位置都会列出；`/adjustments` 两个数组内各自检查） |
| 400 | `ZERO_TOTAL_WEIGHT` | `/allocate` 的 `units`，或 `/adjustments` 任一版本读数的权重和为零 |
| 400 | `BOUNDS_INVERTED` | 仅 `/allocate-bounded`：至少一组 `minimum_cents > maximum_cents`（一次返回全部倒置项，按输入位置排序） |
| 400 | `INFEASIBLE_BOUNDS` | 仅 `/allocate-bounded`：`total_cents` 低于保底合计或高于可分配上限（零权重组只能取得保底额） |
| 400 | `UNIT_SET_MISMATCH` | 仅 `/adjustments`：两版读数的 `unit_id` 集合不完全相同（缺失或额外，定位到具体数组元素） |
| 404 / 405 | `NOT_FOUND` / `METHOD_NOT_ALLOWED` | 路径或方法不存在 |
| 500 | `INTERNAL_ERROR` | 未预期异常 |

### `GET /health`

存活探针，返回 `{"status": "ok"}`，供 compose 健康检查与 verify 服务等待使用。
