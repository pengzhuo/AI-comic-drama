// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useEffect, useRef, useState } from "react";

import {
  useDirectorAutoRun,
  usePauseDirectorAutoRun,
  useResumeDirectorAutoRun,
  type DirectorAutoRunPayload,
  type DirectorPauseKind,
} from "@/lib/queries/director-auto";

/**
 * 工作室的「无人值守」控制器：一键成片启动后，客户端只需要回答**必须由人做主**的
 * 几个闸门，其余阶段全部自动推进。
 *
 * 服务端会在这些地方停下，我们**不再替客户答**，而是把对应卡片摆出来：
 * - 声线策略（`voice_setup`，没有 tag，靠 `last_error` 识别）→ 工作室声线选择卡；
 * - 角色形象图（`character_images`）→ 角色图片指定卡；
 * - 每集身份图（`identity_images`）→ 身份图指定卡（与角色形象图同一组件）；
 * - **成本闸门**（`cost_estimate`，分镜写完之后）→ 报价卡。
 *
 * 角色形象图与每集身份图都**停下来等客户**：客户可以上传自己的图，或点卡片上的
 * 「跳过，由 AI 生成」。两档都不再自动放行——身份图不再有 90 秒倒计时，服务端巡检
 * 也不再代为确认，客户按自己的节奏来。
 *
 * 其余硬暂停（前置未满足、可重试失败、审核未过、跑飞被兜底）→ 自动 `/continue`：
 * 服务端已经改成「单镜做不出来就本地降级 + 跳过并继续」，所以每次续跑都真的会
 * 往前走，不会再出现「暂停↔续跑」空转。真正做不出来的镜头会被服务端登记成
 * `blocked_shots`，由工作台顶部错误条 + 逐镜标记醒目地交给客户。
 * `awaiting_confirmation`（Agent 提议改动 / 卡住，等用户点头）→ 不猜，交还给人。
 *
 * 自动恢复的**预算由服务端持有**（`recovery_attempts`，见后端
 * `MAX_RECOVERY_ATTEMPTS`）：客户端自动派用 `auto=true` 续跑，不自己数次数，也就不
 * 再出现「resume 先把状态置回 running → 客户端把计数清零 → 无限空转」的老 bug。
 * 预算用尽时服务端把 `pause_kind` 置为 `needs_human`，客户端据此停下交人。
 */
const COST_ESTIMATE_GATE = "cost_estimate";

/** 每集身份图闸门：与角色形象图同一张卡片、同样是等客户上传或手动跳过。 */
const IDENTITY_IMAGES_GATE = "identity_images";

/**
 * 声线策略暂停没有 `[tag]`（后端 `VOICE_POLICY_REQUIRED_ERROR` 被系统声线准备任务按
 * 全等匹配复用，不能加 tag），所以这里按它的特征子串识别。命中即把声线选择卡交给客户。
 */
const VOICE_POLICY_PAUSE_MARKER = "未选择声线策略";

/** 客户端工作台里需要客户回答的闸门种类（含只有 `last_error` 标识的声线策略）。 */
export type StudioGateKind =
  | "voice_setup"
  | "character_images"
  | "identity_images"
  | "cost_estimate";

/** 看到暂停先等一会儿再动：服务端内部还会自己续跑，抢着调反而打断它。 */
const REACT_DELAY_MS = 2500;

/**
 * 服务端在「确定性派发找不到可做的 stage、agent 也没写出新的生成任务」时会暂停，
 * 并把原因写进 ``last_error``（``自动续跑未产生写任务`` / ``…未产生新的任务``）。
 *
 * 这类暂停**重试没有意义**：客户端再 ``/continue``，服务端只会把阶段重试预算清零后
 * 继续空转——现场 ``nn_1104_1`` 就是这样无限复活，客户只看到「制作中」而进度不动。
 * 判定为「阻塞型暂停」，不再自动续跑，把原因直接交给客户。
 */
const BLOCKED_PAUSE_MARKERS = ["未产生写任务", "未产生新的任务"] as const;

export function isBlockedPause(lastError: string | null | undefined): boolean {
  const text = (lastError ?? "").trim();
  if (!text) return false;
  return BLOCKED_PAUSE_MARKERS.some((marker) => text.includes(marker));
}

/**
 * 服务端停在「选声线策略」上（不是闸门，只有 `last_error` 一条线索）。
 */
export function isVoicePolicyPause(lastError: string | null | undefined): boolean {
  return (lastError ?? "").includes(VOICE_POLICY_PAUSE_MARKER);
}

/**
 * run 的暂停分类：优先用服务端下发的 `pause_kind`；老行没有这个字段时按
 * `last_error` 的稳定标记兜底（滚动升级期）。
 */
export function pauseKindOf(
  run: DirectorAutoRunPayload | undefined,
): DirectorPauseKind | null {
  if (!run) return null;
  if (run.pause_kind) return run.pause_kind;
  if (run.gate) return "gate";
  if (isVoicePolicyPause(run.last_error)) return "voice_setup";
  if (isBlockedPause(run.last_error)) return "blocked";
  if ((run.last_error ?? "").trim().startsWith("项目配置缺失")) return "needs_config";
  return null;
}

/**
 * `status=running` 但很久没有任何更新（服务端在真正推进时才会刷新 `updated_at`）：
 * 疑似卡死。阈值跟服务端 `STALL_PROGRESS_SECONDS` 对齐，让两端同一时刻都认为「卡住」。
 */
export const STUCK_PROGRESS_SECONDS = 900;

export function isStuckLikely(
  run: DirectorAutoRunPayload | undefined,
  now: number = Date.now(),
): boolean {
  if (!run || run.status !== "running") return false;
  const updatedAt = Date.parse(run.updated_at ?? "");
  if (!Number.isFinite(updatedAt)) return false;
  return now - updatedAt > STUCK_PROGRESS_SECONDS * 1000;
}

export interface StudioRunController {
  run: DirectorAutoRunPayload | undefined;
  isLoading: boolean;
  /** 非空 = 需要人判断的状态，页面要显示提示。 */
  needsHuman: string | null;
  /** 服务端自动干预用掉的次数（来自 `run.recovery_attempts`）。 */
  recoveries: number;
  /** 当前停下来等客户回答的闸门；null = 没停在闸门上。 */
  gateKind: StudioGateKind | null;
  /** `status=running` 但很久没有进展，疑似卡死。 */
  stuckLikely: boolean;
  /**
   * 按 run 状态选对端点继续：`paused`→`/continue`，`awaiting_confirmation`→`/resume`。
   * 人工点击（`auto=false`）会清零服务端预算。
   */
  resumeManually: () => void;
  /** 终止自动（切手动），把工作台从任何循环里救出来。 */
  stopManually: () => void;
  resumePending: boolean;
  stopPending: boolean;
}

export function useStudioRun(
  project: string,
  { auto }: { auto: boolean },
): StudioRunController {
  const runQuery = useDirectorAutoRun(project, { enabled: true, refetchIntervalMs: 3000 });
  const resumeRun = useResumeDirectorAutoRun(project);
  const stopRun = usePauseDirectorAutoRun(project);

  const [needsHuman, setNeedsHuman] = useState<string | null>(null);
  const progressKeyRef = useRef<string>("");

  const run = runQuery.data?.data;
  const status = run?.status;
  const gate = run?.gate ?? null;
  const episode = run?.episode ?? 0;
  const runId = run?.run_id ?? "";
  const lastError = run?.last_error ?? "";
  const kind = pauseKindOf(run);

  const resumeMutate = resumeRun.mutate;

  // 走进度时清掉上一次的提示（恢复预算由服务端在换 stage 时清零）。
  useEffect(() => {
    if (status !== "running" && status !== "completed") return;
    const key = `${runId}:${status}:${episode}`;
    if (progressKeyRef.current === key) return;
    progressKeyRef.current = key;
    setNeedsHuman(null);
  }, [episode, runId, status]);

  useEffect(() => {
    if (status === "awaiting_confirmation") {
      setNeedsHuman(lastError.trim() || "制作流程需要确认后才能继续。");
      return;
    }
    if (status !== "paused") return;
    if (kind === null) {
      // 认不出暂停原因也必须给出出口：否则页面只剩一句「已暂停」，客户没有任何可点
      // 的按钮（现场 t3 那类「打开就卡住」的观感就是这样来的）。老后端不下发
      // `pause_kind`，这条兜底就是它唯一的出路。
      setNeedsHuman(lastError.trim() || "制作已暂停，需要人工确认后继续。");
      return;
    }
    // 服务端给每类暂停打了结构化标记，这里不再靠子串猜。
    switch (kind) {
      case "blocked":
        setNeedsHuman(
          `制作已停止自动重试（${lastError.trim() || "派发空转"}）。请检查后点「继续制作」重试。`,
        );
        return;
      case "needs_config":
        setNeedsHuman("项目配置还没补全，请先到项目设置里完成配置，再继续制作。");
        return;
      case "fatal":
        setNeedsHuman(
          `${lastError.trim() || "上游拒绝"}。重试无法解决，请检查账号额度或权限。`,
        );
        return;
      case "needs_human":
        setNeedsHuman(
          lastError.trim() || "自动恢复次数已用尽，需要人工检查后继续。",
        );
        return;
      default:
        return;
    }
  }, [kind, lastError, status]);

  useEffect(() => {
    if (!auto || status !== "paused") return;
    if (resumeRun.isPending) return;

    // 阻塞型 / 配置缺失 / 致命 / 自动恢复已用尽 / 用户主动停：都不自动续跑。
    if (
      kind === "blocked" ||
      kind === "needs_config" ||
      kind === "fatal" ||
      kind === "needs_human" ||
      kind === "stopped"
    ) {
      return;
    }

    // 声线策略暂停：等客户在声线选择卡上选「系统 / 自定义」，不自动续跑
    // （自动续跑会用默认策略把客户的选择权绕过去）。
    if (kind === "voice_setup") return;

    // 图片闸门 / 成本闸门：一律交给对应卡片由客户点头，绝不自动确认。
    // 身份图闸门不再有 90 秒倒计时——客户可以按自己的节奏上传形象图，或手动跳过。
    if (kind === "gate" && gate && episode > 0) {
      setNeedsHuman(
        gate === COST_ESTIMATE_GATE
          ? "已完成分镜与制作报价，请确认报价后再继续生成画面与视频。"
          : gate === IDENTITY_IMAGES_GATE
            ? `第 ${episode} 集有新身份还没指定身份图：可以上传自己的图，或直接跳过由 AI 生成。`
            : "有角色形象图还没指定：可以上传自己的图，或直接跳过由 AI 生成。",
      );
      return;
    }

    // 可重试的硬暂停：自动派续跑（`auto:true`，预算由服务端持有，客户端不自己数）。
    const timer = window.setTimeout(() => {
      resumeMutate(
        { status: "paused", auto: true },
        { onError: (error) => setNeedsHuman(`继续制作失败：${error.message}`) },
      );
    }, REACT_DELAY_MS);

    return () => window.clearTimeout(timer);
  }, [
    auto,
    episode,
    gate,
    kind,
    resumeMutate,
    resumeRun.isPending,
    status,
  ]);

  const gateKind: StudioGateKind | null =
    status === "paused"
      ? kind === "voice_setup"
        ? "voice_setup"
        : kind === "gate"
          ? (gate as StudioGateKind | null)
          : null
      : null;

  return {
    run,
    isLoading: runQuery.isLoading,
    needsHuman,
    recoveries: run?.recovery_attempts ?? 0,
    gateKind,
    stuckLikely: isStuckLikely(run),
    resumeManually: () => {
      setNeedsHuman(null);
      resumeMutate(
        { status: status ?? "paused", auto: false },
        { onError: (error) => setNeedsHuman(`继续制作失败：${error.message}`) },
      );
    },
    stopManually: () => {
      stopRun.mutate();
    },
    resumePending: resumeRun.isPending,
    stopPending: stopRun.isPending,
  };
}
