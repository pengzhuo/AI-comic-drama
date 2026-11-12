# 按集前置链的预取（identity_planner → script_writer）

剧本写作过去是**逐集串行**：调度器一轮只派 `current_episode` 一集，等它写完才轮到下一集。
单集剧本是一次「若干分钟~数小时」的 LLM 往返（护花铃_2 单集 1656 行：剧本阶段
2026-11-09 19:55 → 11-10 01:28，≈5.5h 墙钟，中间派发 4 次），多集项目串起来就是十小时级。
各集剧本彼此独立（只吃本集的身份 / 场景 / 道具菜单），没有先后依赖，所以允许同时在飞几集。

但「本集身份还没规划」正是剧本的前置闸门：`pipeline_status` 的 `has_identity_plan` 看的是
本集 `identity_ids`，`enqueue_script_writer_task` 缺它时直接返回 `identity_plan_required`，
而每集身份规划（`identity_planner`）本身也只按 `current_episode` 派发。所以一起预取：

实现：`pipeline_auto.dispatch_next_stage` 每轮先跑 `_prefetch_episode_chain`——
- 候选集的剧本派得动 → 派它的剧本（签名 `dispatch:script_writer:{ep}`）；
- 派不动（`identity_plan_required`）→ 先派它的身份规划（签名 `dispatch:identity_planner:{ep}`，
  与逐阶段阶梯用的是同一个签名，天然不会重复派）；
- 身份规划回来之后，下一轮调度接力派它的剧本。

只在「本集剧本已经轮到过」（已派发或已写好）之后才开始预取，所以调度主路径永远先照顾本集；
游标取**剧本**已派签名里的最大集号，一集剧本只派一次、按集号往前推，身份规划也不发散到窗口
之外。身份规划只做规划、不生成图片（图片仍按集在成片流程里生成），所以不抢图片 lane。

## 热调参

| 文件 | 键 | 默认 | 说明 |
| --- | --- | --- | --- |
| `/data/runtime/script_writer.json` | `concurrency` | 3 | 同时在飞的身份规划 + 剧本任务数（共用同一上限，扇出因此有界） |
| `/data/runtime/script_writer.json` | `process_digest` | `true` | 是否把 `script_process` 的「理解账」注入逐行 prompt（env `SCRIPT_PROCESS_DIGEST` 优先；项目级 `project_config.json` 的 `script_process_digest` 覆盖） |

环境变量 `SCRIPT_WRITER_CONCURRENCY` 优先于热调参文件（`stage_int` 语义）。

「理解账注入」是把 `script_process` 阶段产出的 `script_process/result.json` 摘要
（`script_process.build_downstream_digest`：理解账 + 人物表 + 未决项）追加到逐行标注的
系统提示里，让每行标注不越过流程包锁定的角色/揭示边界；提示里同时明确「仍只标注当前
这一行、不得据此脑补」。这是一次**逐行 prompt 变更**，所以
`SCRIPT_CHECKPOINT_VERSION` 已随之 +1（旧断点失效、最坏整集重算一次）；不想要就置
`false`。

## 断点续跑（逐行主循环）

逐行剧本一集要几百次模型往返，但只在最后 `persist_narration_script` 落库；任务失败、进程重启
或用户中途停下，过去整集重算。现在逐行循环每 `SCRIPT_CHECKPOINT_EVERY`（默认 5）行把进度
原子写到 `<项目输出目录>/runtime/script_writer/ep{N}.json`（目录优先级：workflow 构造参数
`output_dir` → `store.output_dir` → `store.project_dir`；生产 `CogneeStore` 只有
`project_dir`，所以实际就是 `/data/output/<owner>/<项目>/runtime/script_writer/`）：

- 内容：已生成 beats + 循环状态（sticky 身份、`safe_prompt_history`、当前场次块、降级行号）
  + 指纹（原文 sha256 + 行数 + 影响逐行 prompt 的配置与菜单/场次块）。
- 复用：同一集再次 `run()` 时指纹一致才复用；指纹不同、文件损坏、进度与 beats 条数不自洽
  都当没有断点，从零重算。
- 删档：`persist_narration_script` 成功后才删；删除失败也不影响主流程。
- 不落 DB，不参与阶段间共享写入；失败/取消最坏重算 4 行（落点是每 5 行一次）。
- `runtime/script_writer/` 下的 `ep*.json` 是运行态缓存，不是产物，可随时删。

改逐行 prompt / 循环语义时，必须把 `literal_script_writing.SCRIPT_CHECKPOINT_VERSION` +1，
否则旧断点会被继续复用（拿到旧 prompt 的结果）。

## 预取的闸门

一集要满足下面几条才会被预取：

1. **还没剧本**——与 `pipeline_status` 同口径（`_all_or_empty`）：beats 的 `narration` /
   `visual_description` 全部非空才算「已写好」。**没有分镜也算还没剧本**：剧本阶段本身会把
   原文切成行、建成 beats（护花铃_2 的 1656 条就是它建的），所以「beats 还空着」不是
   「无事可做」。反过来，已写好的集绝不能因为阶段重试清了签名就整集重写。
2. **本集存在**（`store.get_episode(ep)` 非空）——不存在就没得派。
3. **本集身份**：已规划 → 派剧本；未规划 → 派身份规划（不越过这一集去抢后面的剧本，
   菜单不会被写坏）。

## 观测

- `runtime/pipeline_timing.jsonl`：`dispatch` 事件 `stage=script_writer` /
  `stage=identity_planner`，`signature` 带集号，重叠派发 `overlapped=true`。
- 在手集数：项目 `data.db` 的 `task_states`（`task_type in ('script_writer','identity_planner')`
  且状态仍在飞）。

## 已知取舍

- 预取会把后续最多 `concurrency` 集的 LLM 花费提前花掉；用户中途停下，这部分就白花（敞口小）。
- 预取派出去的任务失败时，走的是同一套阶段重试/暂停逻辑（失败预算按任务类型计），
  不会因为「不是本集」而静默吞掉。

## 回滚

`concurrency` 设 `1`（或 `SCRIPT_WRITER_CONCURRENCY=1`）即回到逐集串行的老行为，不必回滚代码。
