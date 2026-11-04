// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useEffect, useRef, useState } from "react";

import {
  useContinueDirectorAutoRun,
  useDirectorAutoRun,
  type DirectorAutoRunPayload,
} from "@/lib/queries/director-auto";

/**
 * 工作室的「无人值守」控制器：一键成片启动后，客户端只需要回答**必须由人做主**的
 * 几个闸门，其余阶段全部自动推进。
 *
 * 服务端会在这些地方停下，我们**不再替客户答**，而是把对应卡片摆出来：
 * - 声线策略（`voice_setup`，没有 tag，靠 `last_error` 识别）→ 工作室声线选择卡；
 * - 角色形象图（`character_images`）→ 角色图片指定卡；
 * - 每集身份图（`identity_images`）→ 身份图指定卡；
 * - **成本闸门**（`cost_estimate`，分镜写完之后）→ 报价卡。
 *
 * 其余硬暂停（前置未满足、可重试失败、审核未过、跑飞被兜底）→ 自动 `/continue`：
 * 服务端已经改成「单镜做不出来就本地降级 + 跳过并继续」，所以每次续跑都真的会
 * 往前走，不会再出现「暂停↔续跑」空转。真正做不出来的镜头会被服务端登记成
 * `blocked_shots`，由工作台顶部错误条 + 逐镜标记醒目地交给客户。
 * `awaiting_confirmation`（Agent 提议改动 / 卡住，等用户点头）→ 不猜，交还给人。
 *
 * 自动恢复有次数上限，且**只有真正往前走（状态回到 running、或进入下一集）才重置
 * 计数**——否则一个必然失败的阶段会被无限重试，把额度烧光。
 */
export const MAX_AUTO_RECOVERIES = 8;

/** 分镜后「制作成本预估」闸门：不自动确认（见文件头注释）。 */
const COST_ESTIMATE_GATE = "cost_estimate";

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

export interface StudioRunController {
  run: DirectorAutoRunPayload | undefined;
  isLoading: boolean;
  /** 非空 = 自动恢复已耗尽或遇到需要人判断的状态，页面要显示提示。 */
  needsHuman: string | null;
  /** 自动恢复用掉的次数（用于展示与排障）。 */
  recoveries: number;
  /** 当前停下来等客户回答的闸门；null = 没停在闸门上。 */
  gateKind: StudioGateKind | null;
  /** 手动催一次 `/continue`（上限用尽后的兜底按钮）。 */
  resumeManually: () => void;
  resumePending: boolean;
}

export function useStudioRun(
  project: string,
  { auto }: { auto: boolean },
): StudioRunController {
  const runQuery = useDirectorAutoRun(project, { enabled: true, refetchIntervalMs: 3000 });
  const continueRun = useContinueDirectorAutoRun(project);

  const [needsHuman, setNeedsHuman] = useState<string | null>(null);
  const [recoveries, setRecoveries] = useState(0);
  const progressKeyRef = useRef<string>("");

  const run = runQuery.data?.data;
  const status = run?.status;
  const gate = run?.gate ?? null;
  const episode = run?.episode ?? 0;
  const runId = run?.run_id ?? "";
  const lastError = run?.last_error ?? "";

  const continueMutate = continueRun.mutate;

  // 走进度时重置自动恢复计数：只有「真的往前」才算重新获得额度。
  useEffect(() => {
    if (status !== "running" && status !== "completed") return;
    const key = `${runId}:${status}:${episode}`;
    if (progressKeyRef.current === key) return;
    progressKeyRef.current = key;
    setRecoveries(0);
    setNeedsHuman(null);
  }, [episode, runId, status]);

  useEffect(() => {
    if (status === "awaiting_confirmation") {
      setNeedsHuman(lastError.trim() || "制作流程需要确认后才能继续。");
    }
  }, [lastError, status]);

  useEffect(() => {
    if (!auto || status !== "paused") return;
    if (continueRun.isPending) return;

    // 阻塞型暂停（派发空转 / 连续几轮没有写任务）：自动续跑只会让服务端
    // 把重试预算清零后继续空转，直接停下并把原因交给客户判断。
    if (isBlockedPause(lastError)) {
      setNeedsHuman(
        `制作已停止自动重试（${lastError.trim()}）。请检查后点「继续制作」重试。`,
      );
      return;
    }

    // 审核未过 / 卡住 / 前置补不齐等硬暂停：服务端已改成「做不出来就本地降级 +
    // 跳过并继续」，每次续跑都真的会往前走，所以恢复自动续跑（见文件头注释）。
    // 声线策略暂停：等待客户在声线选择卡上选「系统 / 自定义」，不自动续跑
    // （自动续跑会用默认策略把客户的选择权绕过去）。
    if (isVoicePolicyPause(lastError)) return;

    // 图片闸门 / 成本闸门：一律交给对应卡片由客户点头，不再自动确认。
    if (gate && episode > 0) {
      if (gate === COST_ESTIMATE_GATE) {
        setNeedsHuman("已完成分镜与制作报价，请确认报价后再继续生成画面与视频。");
      }
      return;
    }

    // 非闸门暂停：硬暂停。自动续跑，但受上限保护。
    const timer = window.setTimeout(() => {
      if (recoveries >= MAX_AUTO_RECOVERIES) {
        setNeedsHuman(lastError.trim() || "自动恢复次数已用尽，需要人工检查后继续。");
        return;
      }
      setRecoveries((value) => value + 1);
      continueMutate(
        {},
        { onError: (error) => setNeedsHuman(`继续制作失败：${error.message}`) },
      );
    }, REACT_DELAY_MS);

    return () => window.clearTimeout(timer);
  }, [
    auto,
    continueRun.isPending,
    continueMutate,
    episode,
    gate,
    lastError,
    recoveries,
    runId,
    status,
  ]);

  const gateKind: StudioGateKind | null =
    status === "paused"
      ? isVoicePolicyPause(lastError)
        ? "voice_setup"
        : (gate as StudioGateKind | null)
      : null;

  return {
    run,
    isLoading: runQuery.isLoading,
    needsHuman,
    recoveries,
    gateKind,
    resumeManually: () => {
      setNeedsHuman(null);
      setRecoveries(0);
      continueMutate(
        {},
        { onError: (error) => setNeedsHuman(`继续制作失败：${error.message}`) },
      );
    },
    resumePending: continueRun.isPending,
  };
}
