# 捏账费用口径：以上游中转站账单为准

「捏账」（项目 Reports 页 / `GET /api/v1/projects/{id}/cost-report`）的费用来自
项目 `data.db` 四张用量表的 `cost_quota`，由 `novelvideo cost-reconcile` 回填。
默认口径已切到 **上游中转站真实账单**（`--source relay`）。

## 为什么要绕开本地捏驿网关

`dramaclaw-newapi-1` 是我们自己的 NewAPI 中继，它的 `options.ModelRatio` 里没有
`DC-*-LLM` / `seedance-2.0-fast` / `LingShan-G2` 这些别名，于是落到默认倍率
（`37.5`）计费，算出来的 `quota` 比上游实收高 **35～212 倍**。真实收费方是
`aicf.nanie168.com`（LLM/图像/视频，channel 1）与 `aigc.nanie168.com`（音频，
channel 7），同一账号可登录两站，账单要两边合并。

## 单号桥接（本地单号 → 上游单号）

中转站只认它自己发出的单号，桥接全部取自捏驿库（只读）：

| 应用侧 | 捏驿库 | 上游列 |
| --- | --- | --- |
| `video_request_usage.request_id` | `tasks.task_id` | `tasks.private_data`（BLOB JSON）里的 `upstream_task_id` |
| `image/audio_request_usage.gateway_request_id`、`llm_request_usage.provider_request_id` | `logs.request_id` | `logs.upstream_request_id` |

## 取数与限制

- 上游接口：`GET /api/task/self`（分页，`p` 从 0 起、`page_size` 上限 100）、
  `GET /api/log/self?request_id=<上游单号>`（精确 1 条）、`GET /api/status`（免鉴权）。
- access token **15 分钟**过期，客户端遇 401 自动重登一次再重试。
- 查询范围会**收窄到本项目用量行真正引用到的 id**，不会去拉全站 3 万条日志。
- 结果缓存在 `RELAY_BILLING_CACHE_PATH`（默认 `/data/runtime/relay_billing_cache.json`）。
- 匹配不上的行留 `cost_quota = NULL`，报表里计进 `unattributed`，不会静默计 0。

## 运行方式（凭据只走命令行，不落库、不进 git）

```bash
# 预演
docker exec -w /app dramaclaw-api-1 /app/.venv/bin/novelvideo cost-reconcile \
  --project <project> --source relay --relay-username <u> --relay-password <p>

# 回写（必须显式 --apply --yes）；--all 对 state 下全部项目
... --apply --yes
```

`--source gateway` 保留旧行为（读本地捏驿库），仅用于排查与回归。

## 币种

中转站 `quota_per_unit = 500000`（与 `QUOTA_PER_USD` 一致）、`usd_exchange_rate`
≈ 7.3、`quota_display_type = CNY`，所以报表按 ¥ 展示：
`cny = usd * usd_cny_rate = quota / 500000 * 7.3`。后端在 payload 里给出
`currency` / `usd_cny_rate` / 每行的 `cny`；前端在字段缺失（api 进程未重启）时按
`usd_cny_rate`、兜底常数 `7.3` 自行折算。

## 已知不计入的部分

- `audio_request_usage` 表从来没有 `gateway_request_id` 列 → 音频一律无法桥接，全部
  落在 `unattributed`（量级约 100 quota/条，可忽略）。
- 更早的项目连 `image_request_usage.gateway_request_id` 列都没有（该列是后加的），
  这些图片行也一律无法桥接。按 pd_1025_1 实测（约 11,000 quota/张）估算，通常只占
  视频费用的几个百分点。
- 有列但没拿到网关响应头（失败 / 校验前中止）的图片行同样无法桥接。
- 聊天 / Agent 的 LLM 消耗记在全局用量表，不在项目 `llm_request_usage` 里，因此
  **不**出现在项目捏账页。
- 上游账单里查不到的上游单号（中转站已清理该任务，或当时走的是别的上游账号）会让
  该项目无法定价：`apply` **只覆盖匹配到的行**，匹配不到的保留库里旧值，不会清零。
  实测中转站只认识网关记录过的 2067 个上游单号里的 1533 个，约 10 个老项目属于这种
  情况（nn1 / nn_1018_1 / new1 / new2 / cs_nn / chongsheng_test* / nanie_cs /
  shenye_bianlidian / wenlu）。
