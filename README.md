# 仪器校准漂移补偿 API

基于 **FastAPI + SQLite** 的仪器校准服务：客户端在各时点提交参考值与同点多次读数，服务标记异常读数、计算校准偏差，并在相邻时点间线性插值偏差，对待校正读数施加漂移补偿。

## 快速开始（单条命令）

```bash
docker compose up --build
```

- **API 地址：http://localhost:8000**
- 交互式文档（Swagger UI）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

SQLite 数据落在命名卷 `calibration-data`（容器内 `/data/calibration.db`），容器重启数据不丢。

## 校准规则

1. **观测登记**：每次提交参考值 `reference`、容差 `tolerance`、同一时点 `timestamp` 的至少 3 次读数。
2. **异常判定**：读数偏离该组**中位数**超过 `tolerance` 即标记为异常（原始读数仍完整保留）。
3. **校准偏差**：`bias = reference − 有效读数（剔除异常后）的中位数`。
4. **漂移补偿**：在相邻观测时点间对 `bias` 做**线性插值**，`校正读数 = 原始读数 + 插值偏差`。
5. **区间重算**：新增/修订观测只重算受影响的相邻区间（原位置缺口重新接通、新位置区间被时点拆分），其余历史区间与原始数据不变。
6. **覆盖范围**：查询时点超出 `[最早观测, 最晚观测]` 时拒绝（HTTP 400）；恰落在观测点上时直接使用该点偏差。
7. **修订与版本**：观测带单调递增的 `revision`（新建为 1）。`PUT` 修订须携带当前 `revision`，版本过期或目标时点已被占用均返回 409；成功后观测 ID 不变、版本 +1，并保存一份修订前快照（旧输入与旧偏差），可通过修订历史接口按版本倒序查询。
8. **累计漂移量**：`GET /drift-integral?from=&to=` 在覆盖范围内对当前分段线性 `bias` 做**解析积分**（精确梯形面积；段内 `bias` 过零时按解析交点拆分绝对值面积，不做固定步长采样），返回 `signed_bias_seconds`、`absolute_bias_seconds` 及按插值区间裁剪后的分段 `contributions`。窗口边界可落在区间内部；`from` 必须小于 `to`，任一边界越界返回 400。
9. **漂移超限窗口分析**：`GET /drift-exceedances?from=&to=&limit=` 在覆盖范围内**解析求解**分段线性 `bias` 与 `±limit` 的交点（不做固定步长采样），返回 `|bias| > limit` 的连续区间，每段含方向（`high`/`low`）、持续秒数与区间内峰值绝对偏差；跨观测点仍连续且同向的片段合并为一段，恰等于阈值的瞬时不计入超限（观测点恰好触及阈值时两侧不合并）。`from` 必须小于 `to`、`limit` 必须为正数，任一边界越界返回 400。
10. **漂移预算耗尽分析**：`GET /drift-budget-depletion?from=&budget=` 从 `from` 起沿当前分段线性 `bias` 累积 `|bias|·秒`，**解析求出**预算首次耗尽的精确时点：段内 `bias` 过零时先在解析零点拆分（`|bias|` 仅在拆分后的子段上才是线性函数），目标面积落入某子段时反解其累积二次式，不做固定步长采样。返回 `depleted_at`（覆盖范围内未耗尽为 `null`）、截至耗尽点（未耗尽时为覆盖终点）的 `consumed`/`remaining`，以及按插值区间裁剪的分段 `contributions`。`budget` 必须为正数，`from` 越出覆盖范围返回 400。
11. **最长稳定窗口分析**：`GET /drift-stability-window?from=&to=&max_spread=` 在查询范围内**解析求出**持续时间最长、且窗口内 `max(bias)−min(bias) ≤ max_spread` 的连续子区间。最优边界可落在插值段内部：单段窗口按线性斜率直接截取（`时长 = min(段长, max_spread/|斜率|)`）；跨段窗口把段内可行 `(u, v)` 参数集写成一组线性不等式交出的凸多边形，线性目标（窗口时长）的最大值必在多边形顶点，由约束直线的**解析交点精确定位**，全程无固定步长采样。多个窗口并列最长时返回起点最早者。响应含起止时刻（Unix 秒与 ISO 8601）、`duration_seconds`、`min_bias`、`max_bias`。`from` 必须小于 `to`、`max_spread` 必须为正数，任一边界越出覆盖范围返回 400。
12. **历史时点漂移补偿**：`GET /historical-correction?effective_at=&as_of=&reading=` 按 `as_of` 系统时点重建校准状态后，对目标观测时点 `effective_at` 的读数做漂移补偿。重建依据 `observations.created_at`、当前版本与 `revisions` 快照：`created_at > as_of` 的观测尚未创建、不参与；修订自 `changed_at` 起生效（恰等于 `changed_at` 时使用新版本），`changed_at > as_of` 的修订一律回滚到快照旧值。随后按重建出的观测时点排序，沿用与 `/corrections` 相同的覆盖范围与线性插值规则。查询**只读**，不改写当前观测、修订历史或区间表；响应附带 `as_of` 及所用端点的观测 ID 与版本。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/observations` | 登记校准观测，返回偏差与异常明细（201） |
| GET | `/observations` | 列出全部观测（含原始读数与异常标记） |
| PUT | `/observations/{id}` | 修订观测（乐观锁版本校验，409 冲突；ID 不变，版本 +1） |
| GET | `/observations/{id}/revisions` | 按版本倒序列出修订前快照（旧输入与旧偏差） |
| GET | `/intervals` | 列出当前插值区间 |
| POST | `/corrections` | 对指定时点的读数做漂移补偿 |
| GET | `/historical-correction` | 按 `as_of` 系统时点重建校准状态，对目标时点读数做漂移补偿（只读） |
| GET | `/drift-integral` | 对覆盖范围内分段线性偏差做精确积分（累计漂移量） |
| GET | `/drift-exceedances` | 解析求解偏差越出 ±limit 的连续区间（方向/持续秒数/峰值） |
| GET | `/drift-budget-depletion` | 解析求解漂移预算（\|bias\|·秒）首次耗尽的精确时点 |
| GET | `/drift-stability-window` | 解析求解偏差极差 ≤ max_spread 的最长连续子区间（起止时刻/持续秒数/min/max bias） |
| GET | `/health` | 健康检查 |

时间戳支持 Unix 秒（数字）或 ISO 8601 字符串（如 `2026-09-18T08:00:00Z`），缺省为服务器当前时间。

### 登记观测

```bash
curl -X POST http://localhost:8000/observations \
  -H 'Content-Type: application/json' \
  -d '{"timestamp": "2026-09-18T08:00:00Z", "reference": 100.0,
       "tolerance": 0.5, "readings": [99.0, 99.2, 98.8, 150.0]}'
```

响应（`150.0` 偏离组中位数 `99.1` 超差，判为异常；`bias = 100.0 − 99.0 = 1.0`）：

```json
{
  "id": 1, "revision": 1, "timestamp": 1789718400.0, "timestamp_iso": "2026-09-18T08:00:00+00:00",
  "reference": 100.0, "tolerance": 0.5, "group_median": 99.1, "bias": 1.0,
  "readings": [
    {"value": 99.0, "is_outlier": false}, {"value": 99.2, "is_outlier": false},
    {"value": 98.8, "is_outlier": false}, {"value": 150.0, "is_outlier": true}
  ],
  "outliers": [150.0]
}
```

### 修订观测

修订请求体与登记一致（`reference`、`tolerance`、`readings`、`timestamp`），额外携带客户端持有的当前 `revision`。服务在单个事务内重新判定异常、重算偏差，并重建**原位置与新位置**受影响的相邻插值区间；观测 ID 保持不变。版本不匹配（已被他人修订）或目标时间戳已被其它观测占用时返回 409，数据不变。

```bash
curl -X PUT http://localhost:8000/observations/1 \
  -H 'Content-Type: application/json' \
  -d '{"timestamp": "2026-09-18T08:00:00Z", "reference": 100.0,
       "tolerance": 0.5, "readings": [99.1, 99.0, 99.2], "revision": 1}'
```

响应与登记观测相同，`revision` 推进为 `2`：

```json
{
  "id": 1, "revision": 2, "timestamp": 1789718400.0,
  "timestamp_iso": "2026-09-18T08:00:00+00:00",
  "reference": 100.0, "tolerance": 0.5, "group_median": 99.1, "bias": 0.9,
  "readings": [
    {"value": 99.1, "is_outlier": false}, {"value": 99.0, "is_outlier": false},
    {"value": 99.2, "is_outlier": false}
  ],
  "outliers": []
}
```

版本冲突（或时间戳冲突）响应：

```json
{"detail": "observation 1 is at revision 2, not 1; fetch the latest observation and retry"}
```

### 查询修订历史

每次成功修订都会先保存一份修订前快照。按版本倒序返回变更时间、旧输入（旧时点、旧参考值、旧容差、旧读数）与旧偏差：

```bash
curl http://localhost:8000/observations/1/revisions
```

```json
[
  {
    "revision": 1,
    "changed_at": 1789720200.0, "changed_at_iso": "2026-09-18T08:30:00+00:00",
    "old_timestamp": 1789718400.0, "old_timestamp_iso": "2026-09-18T08:00:00+00:00",
    "old_reference": 100.0, "old_tolerance": 0.5, "old_bias": 1.0,
    "old_readings": [99.0, 99.2, 98.8, 150.0]
  }
]
```

从未修订过的观测返回空数组 `[]`；观测 ID 不存在时两个接口均返回 404。

### 漂移补偿

```bash
curl -X POST http://localhost:8000/corrections \
  -H 'Content-Type: application/json' \
  -d '{"timestamp": "2026-09-18T09:00:00Z", "reading": 98.0}'
```

响应包含校正值、所用区间及两端点观测的异常明细：

```json
{
  "timestamp": 1789722000.0, "timestamp_iso": "2026-09-18T09:00:00+00:00",
  "reading": 98.0, "bias": 2.0, "corrected": 100.0,
  "interval": {"t_start": 1789718400.0, "t_end": 1789725600.0,
               "start_iso": "2026-09-18T08:00:00+00:00", "end_iso": "2026-09-18T10:00:00+00:00",
               "bias_start": 1.0, "bias_end": 3.0},
  "endpoints": [
    {"observation_id": 1, "timestamp": 1789718400.0, "timestamp_iso": "2026-09-18T08:00:00+00:00",
     "bias": 1.0, "outliers": [150.0]},
    {"observation_id": 2, "timestamp": 1789725600.0, "timestamp_iso": "2026-09-18T10:00:00+00:00",
     "bias": 3.0, "outliers": []}
  ]
}
```

### 历史时点漂移补偿（as_of 状态重建）

`GET /historical-correction?effective_at=&as_of=&reading=` 回答“以 `as_of` 系统时点所能看到的校准状态，如何校正 `effective_at` 时点的读数”。服务用 `observations.created_at`、当前版本与 `revisions` 快照重建 `as_of` 状态：尚未创建的观测不参与；修订自 `changed_at` 起生效（恰等于变更时刻用新版本），晚于 `as_of` 的修订回滚到快照旧值。随后按重建出的观测时点排序，套用与 `/corrections` 完全相同的覆盖范围与线性插值规则。查询只读，不改写当前观测、修订历史或区间表。

沿用上文场景：两条观测（08:00 `bias 1.0`、10:00 `bias 3.0`）均已在 08:15 前登记，观测 1 的修订（`bias 1.0 → 0.9`，版本 1 → 2）于 08:30 生效。查询 09:00 的读数 `98.0`：

```bash
# as_of 在修订之前：看到旧版本（revision 1，bias 1.0）
curl "http://localhost:8000/historical-correction?effective_at=2026-09-18T09:00:00Z&as_of=2026-09-18T08:15:00Z&reading=98.0"
```

```json
{
  "effective_at": 1789722000.0, "effective_at_iso": "2026-09-18T09:00:00+00:00",
  "as_of": 1789719300.0, "as_of_iso": "2026-09-18T08:15:00+00:00",
  "reading": 98.0, "bias": 2.0, "corrected": 100.0,
  "interval": {"t_start": 1789718400.0, "t_end": 1789725600.0,
               "start_iso": "2026-09-18T08:00:00+00:00", "end_iso": "2026-09-18T10:00:00+00:00",
               "bias_start": 1.0, "bias_end": 3.0},
  "endpoints": [
    {"observation_id": 1, "revision": 1, "timestamp": 1789718400.0,
     "timestamp_iso": "2026-09-18T08:00:00+00:00", "bias": 1.0},
    {"observation_id": 2, "revision": 1, "timestamp": 1789725600.0,
     "timestamp_iso": "2026-09-18T10:00:00+00:00", "bias": 3.0}
  ]
}
```

```bash
# as_of 在修订之后：看到新版本（revision 2，bias 0.9），中点插值变为 1.95
curl "http://localhost:8000/historical-correction?effective_at=2026-09-18T09:00:00Z&as_of=2026-09-18T08:45:00Z&reading=98.0"
```

```json
{
  "effective_at": 1789722000.0, "effective_at_iso": "2026-09-18T09:00:00+00:00",
  "as_of": 1789721100.0, "as_of_iso": "2026-09-18T08:45:00+00:00",
  "reading": 98.0, "bias": 1.95, "corrected": 99.95,
  "interval": {"t_start": 1789718400.0, "t_end": 1789725600.0,
               "start_iso": "2026-09-18T08:00:00+00:00", "end_iso": "2026-09-18T10:00:00+00:00",
               "bias_start": 0.9, "bias_end": 3.0},
  "endpoints": [
    {"observation_id": 1, "revision": 2, "timestamp": 1789718400.0,
     "timestamp_iso": "2026-09-18T08:00:00+00:00", "bias": 0.9},
    {"observation_id": 2, "revision": 1, "timestamp": 1789725600.0,
     "timestamp_iso": "2026-09-18T10:00:00+00:00", "bias": 3.0}
  ]
}
```

`effective_at` 越出重建后的覆盖范围（含 `as_of` 时无任何观测）时返回 400；恰落在重建后的观测点上时直接使用该点偏差（`interval` 为 `null`）。

### 累计漂移量积分

对 `[from, to]` 窗口内的分段线性 `bias` 做解析积分。窗口边界可落在区间内部（按插值裁剪）；段内 `bias` 过零时，绝对值面积在解析零点处拆成两个三角形求和，全程无固定步长采样。各分段贡献按时间排序，其 `signed`/`absolute` 面积之和分别等于汇总值。

```bash
curl "http://localhost:8000/drift-integral?from=2026-09-18T08:00:00Z&to=2026-09-18T10:00:00Z"
```

响应（沿用上文 08:00 `bias 1.0` → 10:00 `bias 3.0` 的区间，梯形面积 `(1+3)/2 × 7200s`）：

```json
{
  "from": 1789718400.0, "from_iso": "2026-09-18T08:00:00+00:00",
  "to": 1789725600.0, "to_iso": "2026-09-18T10:00:00+00:00",
  "signed_bias_seconds": 14400.0,
  "absolute_bias_seconds": 14400.0,
  "contributions": [
    {"t_start": 1789718400.0, "t_end": 1789725600.0,
     "start_iso": "2026-09-18T08:00:00+00:00", "end_iso": "2026-09-18T10:00:00+00:00",
     "bias_start": 1.0, "bias_end": 3.0,
     "signed_bias_seconds": 14400.0, "absolute_bias_seconds": 14400.0}
  ]
}
```

若段内 `bias` 变号（如 `2.0 → −2.0`），`signed_bias_seconds` 中正负面积相互抵消（此处为 0），`absolute_bias_seconds` 则为零点两侧两个三角形面积之和（此处为 `7200.0`）。

### 漂移超限窗口分析

对 `[from, to]` 窗口内的分段线性 `bias` 解析求解与 `±limit` 的交点，返回 `|bias| > limit` 的连续区间。每段给出方向（`high`：`bias > limit`；`low`：`bias < −limit`）、持续秒数与区间内峰值绝对偏差；跨观测点仍连续且同向的片段合并为一段，恰等于阈值的瞬时不计入超限（观测点恰好触及阈值时，两侧超限不合并）。全程解析解，不做固定步长采样。

沿用上文观测，再补一条 12:00 的观测（`bias = −3.0`）：

```bash
curl -X POST http://localhost:8000/observations \
  -H 'Content-Type: application/json' \
  -d '{"timestamp": "2026-09-18T12:00:00Z", "reference": 100.0,
       "tolerance": 0.5, "readings": [103.0, 103.1, 102.9]}'

curl "http://localhost:8000/drift-exceedances?from=2026-09-18T08:00:00Z&to=2026-09-18T12:00:00Z&limit=2.0"
```

响应（`bias` 1.0 → 3.0 → −3.0：09:00 越上阈、10:20 回落，两段跨 10:00 观测点合并；11:40 跌破下阈）：

```json
{
  "from": 1789718400.0, "from_iso": "2026-09-18T08:00:00+00:00",
  "to": 1789732800.0, "to_iso": "2026-09-18T12:00:00+00:00",
  "limit": 2.0,
  "exceedances": [
    {"t_start": 1789722000.0, "t_end": 1789726800.0,
     "start_iso": "2026-09-18T09:00:00+00:00", "end_iso": "2026-09-18T10:20:00+00:00",
     "direction": "high", "duration_seconds": 4800.0, "peak_abs_deviation": 3.0},
    {"t_start": 1789731600.0, "t_end": 1789732800.0,
     "start_iso": "2026-09-18T11:40:00+00:00", "end_iso": "2026-09-18T12:00:00+00:00",
     "direction": "low", "duration_seconds": 1200.0, "peak_abs_deviation": 3.0}
  ]
}
```

窗口内从未超限时 `exceedances` 为空数组；`from ≥ to`、`limit ≤ 0` 或窗口边界越出覆盖范围均返回 400。

### 漂移预算耗尽分析

`GET /drift-budget-depletion?from=&budget=` 从 `from` 起沿当前分段线性 `bias` 累积 `|bias|·秒`，解析求出预算首次耗尽的精确时点。逐插值区间推进：段内 `bias` 过零时先在解析零点拆分（`|bias|` 仅在拆分后的子段上才是线性函数，其累积面积是时长的二次函数），目标面积落入某子段时反解该二次式得到精确耗尽时点，全程无固定步长采样。

沿用上文 08:00 `bias 1.0` → 10:00 `bias 3.0` 的区间（`|bias|` 从 1.0 线性升到 3.0，预算 5400 恰在 09:00 耗尽：`(1+2)/2 × 3600s`）：

```bash
curl "http://localhost:8000/drift-budget-depletion?from=2026-09-18T08:00:00Z&budget=5400"
```

```json
{
  "from": 1789718400.0, "from_iso": "2026-09-18T08:00:00+00:00",
  "budget": 5400.0,
  "depleted_at": 1789722000.0, "depleted_at_iso": "2026-09-18T09:00:00+00:00",
  "consumed": 5400.0, "remaining": 0.0,
  "coverage_end": 1789725600.0, "coverage_end_iso": "2026-09-18T10:00:00+00:00",
  "contributions": [
    {"t_start": 1789718400.0, "t_end": 1789722000.0,
     "start_iso": "2026-09-18T08:00:00+00:00", "end_iso": "2026-09-18T09:00:00+00:00",
     "bias_start": 1.0, "bias_end": 2.0, "absolute_bias_seconds": 5400.0}
  ]
}
```

预算未在覆盖范围内耗尽时 `depleted_at` 为 `null`，`consumed` 为到覆盖终点的全部累计量、`remaining` 为剩余预算；`contributions` 按插值区间裁剪到 `[from, 耗尽点或覆盖终点]`，各段 `absolute_bias_seconds` 之和即 `consumed`。`budget ≤ 0` 或 `from` 越出覆盖范围返回 400。

### 最长稳定窗口分析

`GET /drift-stability-window?from=&to=&max_spread=` 在查询窗口内解析求持续时间最长、且 `max(bias)−min(bias) ≤ max_spread` 的连续子区间。最优边界可落在插值段内部：完全落在单个线性段内的窗口，其偏差极差即 `|斜率|·时长`，直接截取 `min(段长, max_spread/|斜率|)`；跨过一个或多个观测点的窗口，以入段/出段内的 `(u, v)` 参数表示，端点偏差与内部观测点最值之差全部不超过 `max_spread` 构成一组线性不等式，可行域是凸多边形，线性目标（窗口时长）的最大值必在多边形顶点，由约束直线解析交点精确定位，全程无固定步长采样。多个窗口并列最长时返回起点最早者。

沿用 08:00 `bias 1.0` → 10:00 `bias 3.0` 的单段场景，`max_spread = 1.0` 时最长稳定窗口恰为一小时（bias 从 1.0 升到 2.0，斜率 `2/7200/秒 → 3600 秒`），并列的 `[08:30, 09:30]` 等窗口中起点最早：

```bash
curl "http://localhost:8000/drift-stability-window?from=2026-09-18T08:00:00Z&to=2026-09-18T10:00:00Z&max_spread=1.0"
```

```json
{
  "from": 1789718400.0, "from_iso": "2026-09-18T08:00:00+00:00",
  "to": 1789725600.0, "to_iso": "2026-09-18T10:00:00+00:00",
  "max_spread": 1.0,
  "window_start": 1789718400.0, "window_start_iso": "2026-09-18T08:00:00+00:00",
  "window_end": 1789722000.0, "window_end_iso": "2026-09-18T09:00:00+00:00",
  "duration_seconds": 3600.0,
  "min_bias": 1.0, "max_bias": 2.0
}
```

跨观测点窗口同理：内部观测点作为固定极值参与线性约束，两端边界由解析交点定位。`from ≥ to`、`max_spread ≤ 0` 或任一边界越出覆盖范围均返回 400。

### 错误码

| 状态码 | 场景 |
| --- | --- |
| 400 | 查询时点超出校准覆盖范围（响应含可用区间；历史补偿按重建后的覆盖范围判定）；积分/超限/稳定窗口 `from ≥ to` 或任一边界越界；超限阈值 `limit ≤ 0`；稳定窗口 `max_spread ≤ 0`；预算耗尽起点 `from` 越界或 `budget ≤ 0` |
| 404 | 修订/查询修订历史时观测 ID 不存在 |
| 409 | 同一时点重复登记观测；修订时版本过期或目标时点已被占用 |
| 422 | 读数不足 3 次、全部读数被判异常、参数非法 |

## 本地开发

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q          # 运行测试
uvicorn app.main:app --reload       # 本地启动（默认 ./calibration.db）
```

环境变量 `CALIB_DB_PATH` 可覆盖 SQLite 文件路径（容器内默认 `/data/calibration.db`）。

## 项目结构

```
app/
  main.py         # FastAPI 路由
  service.py      # 观测入库、相邻区间重算、补偿查询、as_of 历史状态重建、漂移积分、超限窗口分析、预算耗尽分析、最长稳定窗口分析
  calibration.py  # 纯计算：异常判定、偏差、线性插值、分段积分、超限交点求解、预算累积反解、最长稳定窗口凸多边形求解
  models.py       # Pydantic 请求/响应模型
  db.py           # SQLite 连接与建表
tests/test_api.py # 端到端 API 测试
Dockerfile / docker-compose.yml
```
