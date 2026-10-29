// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { AlertTriangle, CheckCircle2, CircleDashed, Loader2, Play } from "lucide-react";

import { Button } from "@/components/ui/button";
import { isImportSettled } from "@/features/studio/studio-api";
import type { DirectorAutoRunPayload } from "@/lib/queries/director-auto";
import type { PipelineStatus, PipelineStepStatus } from "@/types/episode";
import { cn } from "@/lib/utils";

/**
 * 制作进度（客户端工作室）：把一个 run 的状态、全局阶段与当前集阶段翻译成
 * 「用户看得懂的一句话 + 一串打勾的清单」，不暴露内部任务名。
 */

const GLOBAL_STAGES: { key: string; label: string; done: (s: PipelineStatus) => boolean }[] = [
  { key: "ingested", label: "导入剧本", done: (s) => Boolean(s.global.ingested) },
  // 「剧本处理」的完成判定要和一键成片的前置条件用同一个口径（见 studio-api）：
  // 预处理/剧本处理还在队列里就不算完成。
  { key: "script_process", label: "剧本处理", done: (s) => isImportSettled(s.global) },
  { key: "characters", label: "角色设定", done: (s) => (s.global.characters ?? 0) > 0 },
  { key: "episodes", label: "拆分剧集", done: (s) => (s.global.episodes ?? 0) > 0 },
  {
    key: "portraits",
    label: "角色形象",
    done: (s) => Boolean(s.global.portraits_done),
  },
];

const EPISODE_STAGES: { key: string; label: string; done: (s: PipelineStepStatus) => boolean }[] = [
  { key: "identity_plan", label: "角色定妆", done: (s) => Boolean(s.identity_plan) },
  { key: "identity_images", label: "形象出图", done: (s) => Boolean(s.identity_images) },
  { key: "script", label: "分集剧本", done: (s) => Boolean(s.script) },
  { key: "sketches", label: "分镜草图", done: (s) => Boolean(s.sketches) },
  { key: "coloring", label: "上色", done: (s) => Boolean(s.coloring) },
  { key: "global_optimize", label: "画面优化", done: (s) => Boolean(s.global_optimize) },
  { key: "first_frames", label: "镜头首帧", done: (s) => Boolean(s.first_frames) },
  { key: "tts", label: "配音", done: (s) => Boolean(s.tts) },
  { key: "video", label: "镜头视频", done: (s) => Boolean(s.video) },
];

function StageRow({ label, done, active }: { label: string; done: boolean; active?: boolean }) {
  return (
    <li className="flex items-center gap-2 text-sm">
      {done ? (
        <CheckCircle2 className="size-4 shrink-0 text-emerald-500" />
      ) : active ? (
        <Loader2 className="size-4 shrink-0 animate-spin text-primary" />
      ) : (
        <CircleDashed className="size-4 shrink-0 text-muted-foreground/50" />
      )}
      <span className={cn(done ? "text-foreground" : "text-muted-foreground")}>{label}</span>
    </li>
  );
}

export function StudioStageChecklist({ pipeline }: { pipeline: PipelineStatus | undefined }) {
  if (!pipeline) {
    return <div className="text-sm text-muted-foreground">正在读取制作进度…</div>;
  }
  const status = pipeline.episode_status ?? {};
  const currentIndex = pipeline.next_step;
  return (
    <div className="space-y-4">
      <div>
        <div className="mb-2 text-xs font-medium text-muted-foreground">整体</div>
        <ul className="grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-3">
          {GLOBAL_STAGES.map((stage) => (
            <StageRow
              key={stage.key}
              label={stage.label}
              done={stage.done(pipeline)}
              active={!stage.done(pipeline) && stage.key === currentIndex}
            />
          ))}
        </ul>
      </div>
      <div>
        <div className="mb-2 text-xs font-medium text-muted-foreground">
          第 {pipeline.current_episode ?? Math.max(1, pipeline.global.episodes)} 集
        </div>
        <ul className="grid grid-cols-2 gap-x-4 gap-y-1.5 sm:grid-cols-3">
          {EPISODE_STAGES.map((stage) => (
            <StageRow
              key={stage.key}
              label={stage.label}
              done={stage.done(status)}
              active={!stage.done(status) && !stage.done(status) && currentIndex === stage.key}
            />
          ))}
        </ul>
      </div>
    </div>
  );
}

const RUN_LABEL: Record<DirectorAutoRunPayload["status"], string> = {
  manual: "待开始",
  running: "制作中",
  awaiting_confirmation: "等待确认",
  paused: "已暂停",
  completed: "全部完成",
};

export function StudioRunBanner({
  run,
  needsHuman,
  recoveries,
  onResume,
  resumePending,
}: {
  run: DirectorAutoRunPayload | undefined;
  needsHuman: string | null;
  recoveries: number;
  onResume: () => void;
  resumePending: boolean;
}) {
  const status = run?.status ?? "manual";
  const tone =
    status === "completed"
      ? "text-emerald-600 dark:text-emerald-400"
      : status === "paused" || needsHuman
        ? "text-amber-600 dark:text-amber-500"
        : "text-foreground";

  return (
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex items-center gap-2">
        {status === "running" ? (
          <Loader2 className="size-4 animate-spin text-primary" />
        ) : status === "completed" ? (
          <CheckCircle2 className="size-4 text-emerald-500" />
        ) : null}
        <span className={cn("text-sm font-semibold", tone)}>
          {needsHuman ? "需要人工介入" : RUN_LABEL[status]}
        </span>
        {run?.episode ? (
          <span className="text-xs text-muted-foreground">第 {run.episode} 集</span>
        ) : null}
        {recoveries > 0 ? (
          <span className="text-xs text-muted-foreground">（已自动恢复 {recoveries} 次）</span>
        ) : null}
      </div>
      {needsHuman ? (
        <Button type="button" size="sm" onClick={onResume} disabled={resumePending}>
          <Play className="size-3.5" />
          继续制作
        </Button>
      ) : null}
    </div>
  );
}

export function StudioHumanNotice({ message }: { message: string }) {
  return (
    <div className="flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm">
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-500" />
      <span className="text-amber-700 dark:text-amber-300">{message}</span>
    </div>
  );
}
