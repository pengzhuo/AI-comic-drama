# 捏账费用口径：本地按用量 × 单价（实测优先）

「捏账」（项目 Reports 页 / `GET /api/v1/projects/{id}/cost-report`）的费用来自
项目 `data.db` 四张用量表的 `cost_quota`，由 `novelvideo cost-reconcile` 回填。
默认口径是 **本地计价**（`--source local`）：按「用量 × 单价」算，**不登录任何上游**，
不受中转站 429/409 影响（上游对并发会话有上限，登录后不退出会持续撞 409）。
单价里 `actual_rules`（由真账反推）优先，因此算出来的钱与真账同口径。

## 三种口径

| `--source` | 含义 | 用途 |
| --- | --- | --- |
| `local`（默认） | 读 `/data/production-prices.json`：实测反推价 `actual_rules` > 手工折扣价 `rules`（列表价 × 折扣，如 `aiqy-5zhe`）> 上游同步价 `synced`。不触网。 | 日常结算（报表「重新结算」、成片后结算钩子、伴点结算） |
| `relay` | 向中转站取真实 `quota`。登录后 `finally` 强制退出会话。 | 一次性标定真账 |
| `gateway` | 读本地捏驿库 `one-api.db`，别名落默认倍率、偏高 35～212 倍。 | 仅排查/回归 |

自动入口全部走 `local`；`relay` 只保留手动 CLI。

## 为什么要绕开本地捏驿网关

`dramaclaw-newapi-1` 是我们自己的 NewAPI 中继，它的 `options.ModelRatio` 里没有
`DC-*-LLM` / `seedance-2.0-fast` / `LingShan-G2` 这些别名，于是落到默认倍率
（`37.5`）计费，算出来的 `quota` 比上游实收高 **35～212 倍**。真实收费方是
`aicf.nanie168.com`（LLM/图像，channel 1）、`aigc.nanie168.com`（音频，channel 7）
与 `aiqy.nanie168.com`（视频，channel 8 `DC-doubao_video`）。三站共用同一账号
（`hiaihub`）与令牌池，账单要三边合并。

## 单号桥接（本地单号 → 上游单号）

中转站只认它自己发出的单号，桥接全部取自捏驿库（只读）：

| 应用侧 | 捏驿库 | 上游列 |
| --- | --- | --- |
| `video_request_usage.request_id` | `tasks.task_id` | `tasks.private_data`（BLOB JSON）里的 `upstream_task_id` |
| `image/audio_request_usage.gateway_request_id`、`llm_request_usage.provider_request_id` | `logs.request_id` | `logs.upstream_request_id` |

## relay 取数与限制（仅标定用）

- 上游接口：`GET /api/task/self`（分页，`p` 从 0 起、`page_size` 上限 100）、
  `GET /api/log/self?request_id=<上游单号>`（精确 1 条）、`GET /api/status`（免鉴权）。
- access token **15 分钟**过期，客户端遇 401 自动重登一次再重试。
- 上游对**并发会话数**有上限：登录不退出会一直撞 `409 AUTH_SESSION_LIMIT`。所以
  `collect_quotas` 在 `finally` 里主动 `POST /api/user/auth/logout` + `Origin` 头退出。
- `GET /api/log/self` 逐单号查询容易触发 `429`；客户端默认在账单请求之间留最小间隔
  （`RELAY_MIN_REQUEST_INTERVAL_SECONDS`，默认 0.25s），并对 429 做指数退避重试
  （`RELAY_MAX_RATE_RETRIES` 次，退避基数 `RELAY_RATE_BACKOFF_BASE_SECONDS`）；退出
  会话被 429 时同样重试，避免会话泄漏挤占登录额度。`GET /api/task/self` 分页可靠，
  视频（含 `seedance-2.5`）优先走它。
- 查询范围会**收窄到本项目用量行真正引用到的 id**，不会去拉全站 3 万条日志。
- 结果缓存在 `RELAY_BILLING_CACHE_PATH`（默认 `/data/runtime/relay_billing_cache.json`）。

## 运行方式

```bash
# 本地计价（默认，不触网）；--apply 才回写 cost_quota，--yes 免二次确认
docker exec -w /app dramaclaw-api-1 /app/.venv/bin/novelvideo cost-reconcile \
  --project <project> --apply --yes
# --all 对 state 下全部项目

# 一次性标定：取上游真账单（凭据只走命令行，不落库、不进 git；查完自动退出会话）
docker exec -w /app dramaclaw-api-1 /app/.venv/bin/novelvideo cost-reconcile \
  --project <project> --source relay --relay-username <u> --relay-password <p> \
  --apply --yes
```

标定出的真账再用
`POST /api/v1/settings/production-prices/calibrate?project=<id>&apply=true`
反推成 `actual_rules`（该接口只读项目库里已有的 `cost_quota`，所以 `seedance-2.5`
这类还没回写过真账的行要单独从 task 账单补）。

## 币种

中转站 `quota_per_unit = 500000`（与 `QUOTA_PER_USD` 一致）、`usd_exchange_rate`
≈ 7.3、`quota_display_type = CNY`，所以报表按 ¥ 展示：
`cny = usd * usd_cny_rate = quota / 500000 * 7.3`。后端在 payload 里给出
`currency` / `usd_cny_rate` / 每行的 `cny`；前端在字段缺失（api 进程未重启）时按
`usd_cny_rate`、兜底常数 `7.3` 自行折算。

## 失败任务口径（A 口径）

**失败任务不计成本**：`failed` / `error` / `cancelled`（含 `canceled`）/ `timeout` 等
非成功状态的用量行一律不计费，与线上既有口径一致（下称 **A 口径**）。

- 本地口径：`_reconcile_table_local` 直接跳过这些状态的行，回填时置
  `cost_quota = NULL`、`cost_source = 'unmatched'`，报表里落进 `unattributed`。
- relay 口径：逐单号取真账，上游若对某条失败请求仍收过费（例如视频超时但上游已计费），
  该行会带上真账 `quota`。
- 因此「本地估价 vs 中转站真账」要**同口径**比较：成功行的单价与真账精确一致，差额
  来自失败行（A 口径少计）与单号未桥接的行，不是单价算错。实测 t3：视频 12 条 failed
  未计，本地比 relay 少 ¥6.79。

## 已知限制

- **本地口径不受单号桥接限制**：只要价目表里有对应 `kind/model` 的单价，就按用量计入，
  不依赖 `gateway_request_id` / 上游单号。
- `relay` 口径才依赖桥接：`audio_request_usage` 曾经没有 `gateway_request_id` 列，更早的
  项目连 `image_request_usage.gateway_request_id` 也没有，这些行在 relay 下无法桥接、
  落到 `unattributed`；有列但没拿到网关响应头（失败 / 校验前中止）的图片行同样如此。
- 角色基准图（`nanobanana_character` 的 portrait / identity）过去记录用量后**从不回写**
  `gateway_request_id`，留下一批无单号的图片行；现已透传 `trace` 并在生成成功后回写，
  新行能正常桥接，**历史行仍无法补齐**。
- 上游账单里查不到的上游单号（中转站已清理该任务，或当时走的是别的上游账号）会让
  relay 定不了价；`apply` **只覆盖匹配到的行**，匹配不到的保留库里旧值、不清零。
  实测中转站只认识网关记录过的 2067 个上游单号里的 1533 个，约 10 个老项目属于这种
  情况（nn1 / nn_1018_1 / new1 / new2 / cs_nn / chongsheng_test* / nanie_cs /
  shenye_bianlidian / wenlu）。
- 价目表里没有对应 `kind/model` 的行落 `cost_source = 'unmatched'`、`cost_quota = NULL`，
  报表里计进 `unattributed`，不会静默计 0。
- 聊天 / Agent 的 LLM 消耗记在全局用量表，不在项目 `llm_request_usage` 里，因此
  **不**出现在项目捏账页。
