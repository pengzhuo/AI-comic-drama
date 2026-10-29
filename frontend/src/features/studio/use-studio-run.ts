// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useEffect, useRef, useState } from "react";

import {
  useConfirmDirectorGate,
  useContinueDirectorAutoRun,
  useDirectorAutoRun,
  type DirectorAutoRunPayload,
} from "@/lib/queries/director-auto";

/**
 * 工作室的「无人值守」控制器：一键成片启动后，客户端不应该再回答任何问题。
 *
 * 服务端会在这些地方停下：
 * - 内联图片闸门（角色形象图 / 身份图）→ 自动确认，用生成图继续；
 * - 其它硬暂停（前置未满足、可重试失败、跑飞被兜底）→ 自动 `/continue`；
 * - `awaiting_confirmation`（Agent 提议改动，等用户点头）→ 不猜，交还给人。
 *
 * 自动恢复有次数上限，且**只有真正往前走（状态回到 running、或进入下一集）才重置
 * 计数**——否则一个必然失败的阶段会被无限重试，把额度烧光。
 */
export const MAX_AUTO_RECOVERIES = 8;

/** 看到暂停先等一会儿再动：服务端内部还会自己续跑，抢着调反而打断它。 */
const REACT_DELAY_MS = 2500;

export interface StudioRunController {
  run: DirectorAutoRunPayload | undefined;
  isLoading: boolean;
  /** 非空 = 自动恢复已耗尽或遇到需要人判断的状态，页面要显示提示。 */
  needsHuman: string | null;
  /** 自动恢复用掉的次数（用于展示与排障）。 */
  recoveries: number;
  /** 手动催一次 `/continue`（上限用尽后的兜底按钮）。 */
  resumeManually: () => void;
  resumePending: boolean;
}

export function useStudioRun(
  project: string,
  { auto }: { auto: boolean },
): StudioRunController {
  const runQuery = useDirectorAutoRun(project, { enabled: true, refetchIntervalMs: 3000 });
  const confirmGate = useConfirmDirectorGate(project);
  const continueRun = useContinueDirectorAutoRun(project);

  const [needsHuman, setNeedsHuman] = useState<string | null>(null);
  const [recoveries, setRecoveries] = useState(0);
  const handledGateKeys = useRef<Set<string>>(new Set());
  const progressKeyRef = useRef<string>("");

  const run = runQuery.data?.data;
  const status = run?.status;
  const gate = run?.gate ?? null;
  const episode = run?.episode ?? 0;
  const runId = run?.run_id ?? "";
  const lastError = run?.last_error ?? "";

  const confirmMutate = confirmGate.mutate;
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
    if (confirmGate.isPending || continueRun.isPending) return;

    const timer = window.setTimeout(() => {
      if (gate && episode > 0) {
        const gateKey = `${runId}:${gate}:${episode}`;
        if (handledGateKeys.current.has(gateKey)) return;
        handledGateKeys.current.add(gateKey);
        confirmMutate(
          { gate, episode },
          { onError: (error) => setNeedsHuman(`确认制作闸门失败：${error.message}`) },
        );
        return;
      }
      // 非闸门暂停：硬暂停。自动续跑，但受上限保护。
      if (recoveries >= MAX_AUTO_RECOVERIES) {
        setNeedsHuman(lastError.trim() || "自动恢复次数已用尽，需要人工检查后继续。");
        return;
      }
      setRecoveries((value) => value + 1);
      continueMutate(
        { voicePolicy: "system" },
        { onError: (error) => setNeedsHuman(`继续制作失败：${error.message}`) },
      );
    }, REACT_DELAY_MS);

    return () => window.clearTimeout(timer);
  }, [
    auto,
    confirmGate.isPending,
    confirmMutate,
    continueRun.isPending,
    continueMutate,
    episode,
    gate,
    lastError,
    recoveries,
    runId,
    status,
  ]);

  return {
    run,
    isLoading: runQuery.isLoading,
    needsHuman,
    recoveries,
    resumeManually: () => {
      setNeedsHuman(null);
      setRecoveries(0);
      continueMutate(
        { voicePolicy: "system" },
        { onError: (error) => setNeedsHuman(`继续制作失败：${error.message}`) },
      );
    },
    resumePending: continueRun.isPending,
  };
}
