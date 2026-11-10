# 剧本处理（script_process）运行期调参

「处理剧本」= 后端 `script_process` 阶段（`workflows/script_processing.py`）。
它是长稿热点：原文按字符/上下文预算切成 N 段逐段调用，最后把各段理解账归并成
全剧一份。本页说明不改代码就能调它的开关，以及怎么验证。

## 热调参文件

进程启动后每次构造工作流都会读一次这个文件（缺失/损坏都当空字典，绝不抛错）：

```
${NOVELVIDEO_RUNTIME_DIR:-<DATA_ROOT>/runtime}/script_process.json
```

现网即 `/data/runtime/script_process.json`（宿主机与 api 容器共用 `/data`）。
路径可用 `SCRIPT_PROCESS_TUNING_FILE` 覆盖。

```json
{
  "concurrency": 12,
  "chunk_max_chars": 3000,
  "skill_injection": "full",
  "parallel_digest": true,
  "digest_segment_chars": 2000,
  "digest_facts_max_chars": 0
}
```

| 键 | 含义 | 代码兜底 |
| --- | --- | --- |
| `concurrency` | 分段并发（同时最多几段在飞） | `SCRIPT_PROCESS_DEFAULT_CONCURRENCY` = 12 |
| `chunk_max_chars` | 单段字符预算；超上下文预算会自动再切 | 3000 |
| `skill_injection` | 分段注入形态：`full` 整份第 02 份 / `condensed` 随包精简块 | `full` |
| `parallel_digest` | 归并分批是否并发（`false` 退回旧的深度优先串行语义） | `true` |
| `digest_segment_chars` | 每段摘要在归并输入里的截断长度（字符） | 2000 |
| `digest_facts_max_chars` | 每段事实 JSON 的字节上限；调小可减少归并批次数/输入 token（保结构收敛） | 0（不限） |

**优先级**：环境变量（`SCRIPT_PROCESS_*`）> 该文件 > 代码常量。
改文件**无需重启**（下一次构造工作流即生效）。归并（`digest` 作用域）没有精简块，
`condensed` 时会自动回落整份第 01 份——不会空注入。

## 相关环境变量

`SCRIPT_PROCESS_CONCURRENCY`、`SCRIPT_PROCESS_CHUNK_MAX_CHARS`、
`SCRIPT_PROCESS_SKILL_INJECTION`、`SCRIPT_PROCESS_PARALLEL_DIGEST`、
`SCRIPT_PROCESS_DIGEST_SEGMENT_CHARS`、`SCRIPT_PROCESS_DIGEST_FACTS_MAX_CHARS`、
`SCRIPT_PROCESS_TUNING_FILE`；过载判定沿用
`SCRIPT_PROCESS_OVERLOAD_STREAK` / `SCRIPT_PROCESS_OVERLOAD_COOLDOWN_SECONDS`
（连续瞬时失败进入冷却，冷却期间并发降为 1）。见 `.env.example` 第 13 节。

## 观测

每次分段/归并调用都会往项目的 `runtime/pipeline_timing.jsonl` 追一条
`script_process_call`：

```json
{"ts": 1794289..., "event": "script_process_call", "phase": "segment",
 "index": 12, "total": 65, "ok": true, "concurrency": 6,
 "injection": "full", "duration_seconds": 21.4}
```

- `phase` = `segment` / `digest`；`index`/`total` 只对分段有值。
- 该文件是**滚动**的（只留最近约 25 KB），跑完尽快取数。
- 过载/重试告警同时写任务日志与 `novelvideo.script_process` logger。
- 每次归并前打一条 `归并输入 N 段 → M 批（预算 …/摘要截断 …/事实上限 …）`，
  用来判断「归并输入瘦身」是否把批次数压下来。

快速汇总（容器内）：

```bash
docker exec dramaclaw-api-1 /app/.venv/bin/python - <<'PY'
import json, pathlib
p = pathlib.Path("/data/output/<owner>/<project>/runtime/pipeline_timing.jsonl")
ev = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
ev = [e for e in ev if e.get("event") == "script_process_call"]
seg = [e for e in ev if e["phase"] == "segment"]
print("segments", len(seg), "ok", sum(e["ok"] for e in seg),
      "span", round(max(e["ts"] for e in ev) - min(e["ts"] for e in ev), 1))
PY
```

## 部署与回滚

代码烤在镜像里，热补后必须重启才生效：

```bash
docker cp <新文件> dramaclaw-api-1:/app/src/novelvideo/workflows/script_processing.py
docker restart dramaclaw-api-1
```

重启会重载进程（容器 FS 保留，热补不丢）；`docker compose up -d` 会重建容器并
还原镜像代码，**不要**用它来应用热补。回滚就是把备份的旧文件 `docker cp` 回去再重启。
重启会打断其它项目在飞的任务，挑空窗执行。

## 验证口径

强制重跑一个存量项目（`force=true`），对比墙钟与产物：

```bash
POST /api/v1/projects/{project}/script-process/start   {"force": true}
```

- 墙钟：`pipeline_timing.jsonl` 首末 `script_process_call` 的 `ts` 差，或
  `screenplay/preprocess.json` → `script_process/result.json` 的落盘时间差。
- 产物：`script_process/result.json` 的 `chunks` / `degraded_chunks`，
  以及 `production_script.md` 抽检。
- A/B`skill_injection`：同一项目分别用 `full` / `condensed` 各重跑一次，
  抽检 `production_script.md` 的事实边界与逐场完整性，确认精简块不降质再切。

## 实测提速（护花铃_2，191,255 字符 / 65 计划段，2026-11-10）

同一项目强制重跑，隔离变量（均为 `parallel_digest=true`）：

| 运行 | 并发 | 注入 | 分块 | 墙钟 | span(s) | 段中位(s) | degraded | 场次 | 理解账字符 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 基线 | 2 | full | 3000 | ~72 min | — | — | 0 | — | — |
| A | 4 | full | 3000 | 32.8 min | 1838.7 | 87.9 | 0 | 150 | 62.7k |
| B | 4 | condensed | 3000 | 25.6 min | 1435.0 | 60.5 | 0 | 155 | 64.6k |
| C | 6 | condensed | 3000 | 16.4 min | 917.2 | 63.3 | 0 | 150 | 63.1k |
| D | 6 | condensed | 6000 | 12.3 min | 618.2 | 77.2 | 0 | 111 | 41.1k |
| E | 8 | condensed | 3000 | 11.8 min | 653.5 | 58.4 | 0 | 159 | 66.3k |
| F | 8 | condensed | 4000 | 10.1 min | 552.9 | 67.3 | 0 | 122 | 48.8k |
| G | 12 | condensed | 3000 | 7.5 min | 448.9 | 62.3 | 0 | 169 | 61.1k |
| H | 8 | condensed | 3000 + 瘦身 | 11.0 min | 656.0 | 60.1 | 0 | 147 | 46.8k |

- **并发 4→6→8→12 近线性**（B→C→E→G：span 1435→917→654→449，单次中位基本不变）——瓶颈是并发度
  而非单次时延。故代码默认改为 **12**（G 相对 E 干净 −31%，`degraded=0`）。12 会吃满
  全局闸门 `LLM_GLOBAL_CONCURRENCY`（默认 12），但**不增加网关总负载**——闸门本就限制
  全进程在飞总量；再往上要同步抬高闸门，会波及所有阶段，未采纳。
- **`condensed` 有效且不降覆盖**（A→B 单次中位 −31%，场次/理解账同档）；产物体量约减半，
  下游若缺料可 `set_injection.sh full` 一键切回。
- **分块 4000/6000 掉质量**（D：场次 111/理解账 41k；F：场次 122/理解账 49k，均低于
  3000 的 150+/63k+）——大段让每段概括更粗，在没有同步加大输出预算前**不要**启用，保持 3000。
- **归并输入瘦身不采纳**（H：`digest_segment_chars=1000` + `digest_facts_max_chars=2500`）：
  digest 批次数 37→27、sum −21%，但 digest 阶段并发且占比小，**span 持平**（653.5→656.0），
  而理解账 −29%、角色 −19%、gaps −25%、并出现 1 次归并失败。旋钮代码保留（默认 2000/0 = 关闭），
  仅在确有输入预算压力时才单独用于减 token。

