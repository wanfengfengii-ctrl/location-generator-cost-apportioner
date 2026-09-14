# 影视外景发电机燃料费用分摊服务

纯后端 HTTP JSON 服务（Python 3.12 + FastAPI）。外景拍摄结束后，把同一台移动
发电机的燃料发票总额（整数分）按各摄制组的 `watts × minutes` 整数权重，用
**最大余数法（Hamilton 分摊）**拆到每个摄制组，保证分摊明细合计**始终严格等于**
发票总额，财务可直接对平。

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
  main.py        # FastAPI 应用与路由（POST /allocate、GET /health）
  allocator.py   # 最大余数法核心逻辑（纯 Python，无框架依赖）
  schemas.py     # 请求/响应模型（严格非负整数校验）
  errors.py      # 统一错误信封，所有错误返回可定位字段
tests/
  test_api.py        # HTTP 验收测试（可打真实服务或进程内 TestClient）
  test_allocator.py  # 分摊逻辑单元测试（含随机化不变量校验）
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
| 422 | `VALIDATION_ERROR` | 缺字段、负数、浮点/字符串/布尔冒充整数、空 `unit_id`、空 `units`、多余字段、JSON 语法错误等 |
| 400 | `DUPLICATE_UNIT_ID` | `unit_id` 重复（每个重复出现的位置都会列出） |
| 400 | `ZERO_TOTAL_WEIGHT` | 所有组 `watts × minutes` 之和为零 |
| 404 / 405 | `NOT_FOUND` / `METHOD_NOT_ALLOWED` | 路径或方法不存在 |
| 500 | `INTERNAL_ERROR` | 未预期异常 |

### `GET /health`

存活探针，返回 `{"status": "ok"}`，供 compose 健康检查与 verify 服务等待使用。
