# AICF 素材库（真人输入图先登记再引用）

给编码 agent 的速查。代码在 [`src/novelvideo/storage/asset_library.py`](../../src/novelvideo/storage/asset_library.py)，
接入点在 [`src/novelvideo/generators/video_generator.py`](../../src/novelvideo/generators/video_generator.py)
的 `_asset_library_first_frame_candidate_factory`。

## 它解决哪一道门

上游 Ark 对 i2v 的**首帧位**（`content[0]`）单独做真人隐私审核，参考图槽位不查：

```
InputImageSensitiveContentDetected.PrivacyInformation
the input image 'content[0]' may contain real person
```

2026-10-28 **直连 aicf 的 A/B 脚本**实测（`nn_1027_1` / 第 1 集 Beat 45，同一张首帧 render、
同一提示词、`doubao-seedance-2.0-fast`、720p/9:16/5s，前后相差几分钟）：

| `first_frame` 引用方式 | 结果 |
| --- | --- |
| 媒体中继直链 | **400** `InputImageSensitiveContentDetected.PrivacyInformation`「may contain real person」，不建任务、不计费 |
| `asset://asset-20261028122009-ntgtc` | **200** 通过，严格首帧锁定、画质零损失 |

⚠️ 这次 A/B 走的是**直连 aicf**，不是应用链路。2026-10-28 22:33 用应用链路复测 Beat 44
（素材 `Active`、首帧同源）时 `asset://` 提交仍被判输入档敏感（见「素材库救不了画面内容敏感」
一节），所以「经 newapi 时 `asset://` 是否照旧生效」**仍未解**，别把上表当成应用链路的结论。

**它不解决输出档。** 同一张图用 `asset://` 提交后，输出仍被
`OutputVideoSensitiveContentDetected` 拦下。输出档是画面内容问题（例如提示词把旗帜列为
落幅必现），只能改画面/提示词。

## 首帧审核在线上长什么样（2026-10-28 实测）

1. 应用通路（经本地 newapi）看到的首帧拒绝**全是光杆形态**：`InputImageSensitiveContentDetected`
   + `the input image 'content[0]' may contain sensitive information`。`nn_1027_1` 事故里共
   44 行 = 22 次尝试，`may contain real person` **0 行**。
2. 这 22 次是 Beat 42/43/44 的**画面内容敏感**帧（不是隐私帧）：同一批帧拿去登记素材
   （**直连 aicf、不经网关**）也回同样的光杆文案。⇒ 光杆文案**不是**网关抹掉子类的产物，
   而是 Ark 对「画面内容敏感」本来就回光杆，`.PrivacyInformation`「may contain real person」
   只在隐私分类器命中时出现。
3. 旧判定只认隐私子类 ⇒ 在线上恒为假 ⇒ 素材库档与整条降质阶梯都是**死代码**：Beat 42
   在 13:0x 重试当场 400，台账 0 条、无任何 `asset://` 日志，每次都是直链原样重投。

判定改为按**结构**识别（`anchor_moderation.is_input_image_sensitive_rejection`）：
`InputImageSensitiveContentDetected` 出现即算首帧图审核（隐私子类是其真子集），显式排除
含 `output video` 的成片审核文案（code 是 `upstream_task_failed`，语义完全不同）。

首帧图审核随后分两支：

- **真人隐私子类**（`is_privacy_information_rejection`）：行为不变，逐档降质 + 多参考兜底
  （素材库档仍插在降质阶梯之前）。
- **光杆/其它子类**（画面内容敏感）：**只允许前进到素材库 `asset://` 档**，不做降质 ——
  糊一版并不能让「内容本身敏感」变得不敏感，却可能真的过审、拿画质换一次侥幸。素材库档
  自己也被判输入图敏感时**直接失败**并给出「请调整本 beat 的画面或首帧」的可读原因。

## 素材库救不了「画面内容敏感」（2026-10-28 实测）

`nn_1027_1` 第 1 集 Beat 42–45 的首帧逐张送进素材库（`POST /v1/assets`，直连 aicf）：

| Beat | 素材库结果 | 视频任务输入档 |
| --- | --- | --- |
| 42 | **Failed** `InputImageSensitiveContentDetected`（间隔重试 3 次全 Failed） | 400 同码 |
| 43 | **Failed** 同码 | 400 同码 |
| 44 | `Active` | 400 同码（改用 `asset://` 提交同样 400） |
| 45 | `Active` | **通过**，卡在输出档 |

同一张 Beat 42 首帧（1010×1558，村委会会议室 + 墙面党旗/红横幅 + 半圈乡亲）的裁剪与缩放实验：

| 变体 | 素材库 |
| --- | --- |
| 完整帧（原图 / 880 / 720 / 640 / 512 宽，JPEG q92） | **全部 Failed** |
| 只留上半：党旗 + 红横幅，无人物 | **Active** |
| 只留中段：人物上半身 + 桌面（含旗子下缘） | **Active** |
| 只留下半：人物 + 募捐箱 + 半圈乡亲 | **Active** |

⇒ 闸门是**整幅画面的场景语义**（会议室 + 党旗 + 人群的「政治活动」构图），不是尺寸，
也不是「画面里有没有党旗」——单看旗子或单看人群都能过，合起来就过不了。**缩小、模糊、
裁切都救不了这一镜**，`nn_1027_1` 这 4 镜的唯一出路是改画面 / 换首帧（去掉墙面党旗与
红横幅，或换场景），以及改提示词（Beat 45 的落幅必现旗帜要求）。

素材库档的价值因此收窄成一条：**它只为「真人隐私」这一类免审核**（它的设计目标）。
对**画面内容敏感**类无效 —— 同一套内容审核在登记素材时就会先跑一遍。

## 上游接口

| 接口 | 用途 |
| --- | --- |
| `POST /v1/assets` | `{url: 公网 HTTP(S) 地址, name, asset_type}` → 返回 `id` + `asset://…` + `group_id`，状态 `Processing` → 轮询 `GET /v1/assets/{id}` 到 `Active` |
| `GET /v1/assets/{id}` | 查状态 |
| `DELETE /v1/assets/{id}` | 删除（台账里的素材要清理时用） |
| `POST /api/assets/groups` | 逻辑素材组（含 `is_real_person` 标记），同名幂等 |

引用方式：把 `asset://…` 放进首帧槽位（`metadata.image_url` → 网关转成
`metadata.content[].image_url.url`）。

实测：登记到 `Active` 约 **2 秒**，没有人工审核中间态，返回的 `url` 会被改写成 Ark 侧
`ark-media-asset.tos-cn-beijing…` 副本。素材归组由上游自动决定（登记接口不接受 `group_id`）。

已知上游问题：`GET /v1/assets`（列表）会 502 `MissingParameter.Filter.GroupType` —— 文档
没写这个必填参数，缺了就报错。登记/单条查询/删除不受影响，故运维清单以**本地台账**为准。

## 硬约束：必须是同一个 AICF 账号

`asset://` 是**账号级**素材。登记用的 token 必须与"真正生成视频那条 channel 的上游 key"
属于同一个 AICF 账号，否则登记出来的素材在生成请求里不可见（白登记）。

本部署：视频走 `dramaclaw-newapi-1` 的 channel 1（`DC-openai`，
`base_url=https://aicf.nanie168.com`），所以 `AICF_ASSET_TOKEN` 必须是**那个 aicf 账号**
签发的 token，而不是本地 newapi 的运行 token（后者打 aicf 是 401 `Invalid token`）。

## 配置

环境变量（`config.AICF_ASSET_*`）优先，其次是 JSON 配置文件；两者都没有 = 功能静默关闭。

```jsonc
// <STATE_DIR>/aicf_asset_library.json   （本部署 = /data/state/aicf_asset_library.json）
{
  "enabled": true,
  "base_url": "https://aicf.nanie168.com",
  "token": "sk-...",          // 与生成 channel 同账号
  "timeout_seconds": 60       // 轮询 Active 上限
}
```

- `enabled` 留空 = "有 token 就启用"；显式 `false` / `AICF_ASSET_LIBRARY_ENABLED=0` 可关掉。
- 用配置文件是为了**不重建容器**就能开关与换 token（`STATE_DIR` 是挂载卷）。

## 接入行为

`NewApiVideoGenerator` 的首帧候选顺序（`_submit_video_candidates`）：

```
首帧原图（直链）
  → 素材库：登记 → asset://（本模块，惰性，仅在原图被首帧图审核拒绝时才登记）
  → 首帧降质阶梯（模糊/下采样，anchor_moderation）  ← 仅真人隐私子类会走到这里
  → 多参考兜底（丢首帧锁定）
```

- **惰性**：只有前档真的被拒才登记 —— 登记会在账号里留下持久素材，不该每镜都做。
- **回退**：登记失败（未配置 / 401 / 503 / 超时）只记一条日志，原样退回直链候选与降质阶梯，
  不会让整镜失败。
- **锦上添花档**：素材库档被包成 `OptionalVideoCandidate`，它**自己在提交时被拒也一律
  继续降级**（网关不认 `asset://` 给出别的 4xx、上游 5xx、文本审核都算），保证「靠降质
  本来能过」的一镜不会因为这一档而失败。普通档位不受影响：非隐私类 4xx 仍然直接抛出。
- **台账**：`<STATE_DIR>/asset_library.jsonl`（只追加），`源图 sha256 → asset://`，
  同一张图重投直接复用，不重复登记。源图重渲（内容变了）自然生成新行。

## 运维

```bash
# 当前配置（token 只显示前后几位）
python -m novelvideo.storage.asset_library --status
# 台账里的素材（上游列表接口不可用，以台账为准）
python -m novelvideo.storage.asset_library --list
# 删掉一个素材
python -m novelvideo.storage.asset_library --delete asset-20261028122009-ntgtc
```

素材会长期留在 AICF 账号里，需要定期核对台账、清理不再使用的素材。

## 测试

- `tests/test_asset_library.py`：配置解析、错误分类（401/503 → 回退；400 → 登记失败）、
  台账复用、候选构造与回退、档位顺序（素材库在降质阶梯之前）。
- `tests/test_human_review_media_url.py`：`human_review` 的媒体 URL 供给（OSS 预签名
  不可用时回退媒体中继）—— 同一个"真人素材"话题下的姊妹修复。
