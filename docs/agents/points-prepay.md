# 漫剧伴点预扣（一键成片）

给编码 agent 的速查。客户端在 [`src/novelvideo/points_client.py`](../../src/novelvideo/points_client.py)，
接入点是 [`src/novelvideo/api/routes/chat.py`](../../src/novelvideo/api/routes/chat.py) 的
`_charge_points_for_episode`（`POST /projects/{p}/chat/director-auto/gates/{gate}/confirm`，
`gate=cost_estimate`）。

## 它解决哪一道门

「一键成片」分镜写完后、第一批花钱的图/视频开跑**之前**会停在 `cost_estimate` 闸门，
客户在 `/studio` 看到「本集制作报价」并点「确认报价，继续制作」。**这一步要先把钱收了**：
按售价折算伴点调上游接口预扣，成功才放行。

- **扣费口径** = 客户看到的**售价**（成本 × 该客户倍率，`estimate.client_price_cny`），
  不是我们的成本。
- **比例** `amount = round(售价 ¥ × points_per_cny)`，默认 **100 伴点 = ¥1**。
- 上传后的**粗估不扣**（那时还没有分镜，数不准）。
- 内部端 / 没有手机号（owner=`local`）→ **跳过**，不扣。
- **同一集只扣一次**（台账 `points_charges`），**扣了不退**；对账/退款走人工
  （`PointsClient.refund` 走 `optype=2`，当前不自动调用）。

## 上游接口

`POST https://ai.nanie168.com/api/aistory/dopoints.php`（表单）：

| 字段 | 值 |
| --- | --- |
| `phone` | 11 位手机号（客户身份 `user["phone"]`） |
| `amount` | 整数正数（伴点） |
| `optype` | `1`=减少（扣），`2`=增加（冲正） |
| `projectsuuid` | 我们的项目 id（ULID） |
| `sign` | `md5(phone + 密钥)` 小写 hex（与 `studio_access.expected_code` 同算法） |

**响应恒为 HTTP 200，成败只看 body**：

```json
{"code": 0, "message": "操作成功", "data": []}
{"code": 1, "message": "伴点不足，请先充值", "data": null}
```

所以 `code != 0` 也必须当失败（`PointsNotEnough`），别只看状态码。

## 闸门行为

| 情形 | 结果 |
| --- | --- |
| 扣成功 | 记台账 → 写闸门答案 → `resume_paused` |
| `code != 0`（如余额不足） | **402** `{"detail": "<接口 message>"}`，闸门**不答**、不继续 |
| 超时 / 5xx / 坏 JSON / 非 200 | **502**「伴点服务暂时不可用，请稍后重试」（fail-closed） |
| 客户没配倍率（算不出售价） | **400**「该客户尚未配置制作报价，请联系客服后再继续生成」 |
| 折算为 0 / 同集已扣过 / 开关关 / 内部端 / 无手机号 | 直接放行（不调接口） |

前端 `useConfirmDirectorGate` / `confirmDirectorGate` 已经把后端 `detail` 弹成 toast
（`jsonWithBackendError`），卡片不收起 —— 客户充值后再点一次即可，不会重复扣。

## 配置（可热补，无环境变量）

`<DATA_ROOT>/dopoints.json`（本部署 = `/data/dopoints.json`），`DOPOINTS_CONFIG_PATH` 可覆盖；
按 mtime 缓存，改文件即时生效：

```json
{
  "enabled": true,
  "base_url": "https://ai.nanie168.com/api/aistory/dopoints.php",
  "sign_key": "<密钥；留空则回落到 studio 的 client_secret>",
  "points_per_cny": 100,
  "timeout_seconds": 15
}
```

- **缺省关闭**：文件不存在 / `enabled=false` → 整段预扣跳过（闸门照常放行）。
  这是有意的：不想让一个没配好的实例悄悄向客户收钱。
- `sign_key` 是密钥，**不进 git**，只在服务器 `/data/dopoints.json` 里。
- 台账写在项目配置 `project_config.json` 的 `points_charges`，形如
  `{"1": {"amount": 23081, "cny": 230.81, "phone": "138…", "at": "…"}}`。

## 成片后多退少补（结算）

预扣用**计划售价**；成片落盘后按**实际用量**重算售价，多退少补。核心模块
[`src/novelvideo/points_settlement.py`](../../src/novelvideo/points_settlement.py)，
调度在 [`chat/director_auto.py`](../../src/novelvideo/chat/director_auto.py) 的
`_settle_episode_points`（compose 完成分支），台账是项目配置 `project_config.json`
的 `points_settlements`（**键是集号**）。

### 口径

- **实际成本**只认能在**中转站账单对上的**行：该集四张用量表的 `quota` 之和
  （`gateway_cost_reconcile.episode_quota` + relay maps），换算
  `quota / QUOTA_PER_USD(500000) * USD_CNY_RATE(7.3)` —— 与
  `production_prices.calibrate_from_project` 同口径。**不**用网关侧
  `DC-*` / 媒体别名虚高的默认倍率（那会把客户的账算贵）。
- **售价** = 实际成本 × (1 + `contingency_pct`/100) × 客户倍率，再按
  `100 伴点 = ¥1` 折算，与报价卡一致。
- **差额** = 结算伴点 − 预扣伴点；差额 `< 1 伴点` 忽略；`settle_tolerance_pct`
  （% ）以内的差额也忽略，不补不退。
- 结算前先跑一次 relay 对账（`reconcile` 口径）并**回写** `cost_quota`，让运营
  报表与客户账单同口径。

### 状态机（`points_settlements[集号]`）

| status | 含义 | 成片（预览 + 下载） |
| --- | --- | --- |
| `settled` | 已结清（含退款成功、差额在容差内） | 放行 |
| `settled_manual` | 运营手动核销/放行 | 放行 |
| `refund_pending` | 退款失败（**不自动重退**） | 放行（不锁客户） |
| `pending` | 算不出实际用量（relay 账号缺失 / 超时 / 对不上 / 没客户倍率） | 放行 |
| `owed` | 待补缴（`reason=insufficient` 用接口原文；`unavailable` 用固定文案） | **拦** |
| （无台账但已有预扣） | 正在核算 | **短暂拦**，首次访问触发一次后台结算 |

- **补扣失败也拦**（fail-closed）：`code != 0` 与超时/5xx 都记 `owed`。
- **交付后成本再涨不再追扣**：终态（`settled` / `refund_pending` /
  `settled_manual`）一次写死，重复触发直接返回台账。
- **`owed` 重试只补「结算额 − 预扣 − 已扣」**，不重算实际用量（`force=True`）。
- 台账按 **`state_dir`** 读写（`*_in_state_dir`），避开 `run.context.project_name`
  与 `resolve_project_scope` 键不一致的问题。

### 交付闸门

`points_settlement.gate_delivery(user=…, state_dir=…, episode=…)` 按**身份 + 状态**
拦：客户身份（`credential_kind=studio_client`，或 `as_client=<手机号>` 预览）被拦，
内部端放行。落点：

- `/projects/{p}/episodes/{n}/final` → 200 但 `blocked/settle_status/owed_points/message`，
  **不给 `video_url`**；
- `/projects/{p}/episodes/{n}/export/video`、`/export/zip`、`/files/…`、`/media/…`、
  `/static/projects/<id>/videos/episodes/epNNN_final.mp4` → **402**；
- `/export/srt` **不拦**。
- 闸门在 OSS presign **之前**抛错；客户若已持有旧的 presigned URL，过期前仍可直取
  （与预扣同一已知风险）。

### 补缴 / 手动放行端点

- `POST /projects/{p}/episodes/{n}/points/settle` —— 客户补缴后重试（`force=True`），
  成功即解锁；返回 `{status, blocked, owed_points, message}`。
- `POST /projects/{p}/episodes/{n}/points/release` —— **仅内部端**（否则 403），
  手动核销/放行，记 `by`/`action=manual_release`。

### relay 账单账号（可热补，无环境变量）

结算要问中转站账单，账号**环境变量优先**，其次热更文件
`/data/relay-billing.json`（`NOVELVIDEO_RELAY_BILLING_CONFIG` 可覆盖路径）：

```json
{
  "base_urls": ["https://aicf.nanie168.com", "https://aigc.nanie168.com"],
  "username": "<中转站账号>",
  "password": "<口令>",
  "cache_path": "/data/runtime/relay_billing_cache.json"
}
```

- 文件 **0600**、**不进 git**；改文件即时生效。
- **取不到账号不锁客户**：无账号时照样命中缓存；完全对不上 → `pending` 放行。

### 结算开关与回滚

`dopoints.json` 里加 `settle`（缺省 **`off`**，只预扣不定数）：

```json
{
  "enabled": true,
  "settle": "compose",
  "settle_tolerance_pct": 0,
  "settle_budget_seconds": 20
}
```

- `settle="compose"` 才启用成片后结算；`settle_budget_seconds` 是 relay 对账预算，
  超时按「算不出来」记 `pending`。
- **回滚**：`settle` 改回 `off`（或 `enabled=false`）即停；已写的 `points_settlements`
  台账不清，重新打开即接着生效。

## 测试

- `tests/test_points_client.py` — 签名黄金向量、表单字段、成功/`code=1`/5xx/超时/坏 JSON/未配置、注入 transport。
- `tests/test_points_gate.py` — 闸门扣费各分支 + 端点接线（402 时闸门不答、重复确认不重复扣）。
- `tests/test_points_settlement.py` — 结算状态机（补扣/不足/传输失败/退款/退款失败/幂等/容差）、`episode_quota` 只认命中行、交付闸门（只拦客户、`inflight` 触发调度、无预扣放行、手动放行解锁）、结算/放行端点。
- `tests/test_project_config_image_gates.py` — `points_charges` round-trip。
- `frontend/.../director-cost-estimate-card.test.tsx` — 伴点提示 + 402 弹消息。
