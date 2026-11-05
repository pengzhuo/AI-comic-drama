// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useEffect, useRef, useState } from "react";
import { Link, createFileRoute, useNavigate } from "@tanstack/react-router";
import { ArrowLeft, Loader2 } from "lucide-react";

import { DirectorCharacterImagesCard } from "@/components/director/director-character-images-card";
import { StudioCostOverview } from "@/features/studio/studio-cost";
import { StudioEpisodeList } from "@/features/studio/studio-episode-list";
import { StudioQuoteCard } from "@/features/studio/studio-quote-card";
import {
  StudioBlockedShots,
  StudioHumanNotice,
  StudioRunBanner,
  StudioStageChecklist,
} from "@/features/studio/studio-run";
import { StudioVoiceGateCard } from "@/features/studio/studio-voice-gate-card";
import {
  isImportSettled,
  regenerateStudioBeatVideo,
  studioRunConfig,
  waitForStudioBeatVideo,
} from "@/features/studio/studio-api";
import { useStudioPolling } from "@/features/studio/use-studio-polling";
import {
  IDENTITY_GATE_GRACE_SECONDS,
  useStudioRun,
} from "@/features/studio/use-studio-run";
import {
  useClearBlockedShot,
  useStartProjectAutoRun,
  type DirectorBlockedShot,
} from "@/lib/queries/director-auto";
import { useEpisodes, usePipelineStatus } from "@/lib/queries/episodes";
import { useAllProjectSummaries, useProject } from "@/lib/queries/projects";
import { useTasks } from "@/lib/queries/tasks";

export const Route = createFileRoute("/studio/$project")({
  // `?autostart=1` 由上传页带过来：导入一完成就自动开跑「一键成片」。
  validateSearch: (search: Record<string, unknown>): { autostart?: boolean } => ({
    // 路由的默认 `parseSearch` 会对值做 `JSON.parse`，所以 `?autostart=1` 到这里是
    // number 1 而不是字符串 "1"：两种都要认，否则「上传完自动开跑」会静默失效。
    autostart:
      search.autostart === true ||
      search.autostart === 1 ||
      search.autostart === "1" ||
      search.autostart === "true",
  }),
  component: StudioProjectPage,
});

function StudioProjectPage() {
  const { project } = Route.useParams();
  const { autostart } = Route.useSearch();
  const navigate = useNavigate();

  const pipelineQuery = usePipelineStatus(project);
  const episodesQuery = useEpisodes(project);
  const tasksQuery = useTasks({ project });
  const summariesQuery = useAllProjectSummaries();
  const projectQuery = useProject(project);
  const summaries = summariesQuery.data;
  const startRun = useStartProjectAutoRun(project);
  const run = useStudioRun(project, { auto: true });
  const clearBlockedShot = useClearBlockedShot(project);

  const [startError, setStartError] = useState<string | null>(null);
  const [redoError, setRedoError] = useState<string | null>(null);
  const [redoPendingBeat, setRedoPendingBeat] = useState<number | null>(null);
  const [quoteDismissed, setQuoteDismissed] = useState(false);
  // 换集或过闸门后重新给一次机会：上次「先不生成」只针对那一集的报价。
  const gateEpisode =
    run.run?.gate === "cost_estimate" ? (run.run?.episode ?? 0) : 0;
  useEffect(() => {
    setQuoteDismissed(false);
  }, [gateEpisode]);

  const pipeline = pipelineQuery.data?.data;
  const importSettled = isImportSettled(pipeline?.global);
  const status = run.run?.status ?? "manual";
  const runActive =
    status === "running" || status === "awaiting_confirmation" || status === "paused";

  // 真隔离：别人的项目在这里既不在自己的项目列表里，单项目接口也是 404。
  // 两个信号同时成立才判定「不存在或不属于你」，避免把刚创建、列表还没刷新的
  // 新项目误判成不可访问。
  const projectUnavailable =
    summaries !== undefined &&
    !summariesQuery.isLoading &&
    !summaries.some((item) => item.id === project) &&
    (pipelineQuery.isError || episodesQuery.isError);

  useStudioPolling(project, { enabled: !importSettled || runActive });

  // 导入完成后自动开跑；跑起来就把 URL 参数清掉，刷新不会重复启动。
  const autostartFired = useRef(false);
  const startRunMutation = startRun.mutate;
  useEffect(() => {
    if (!autostart || autostartFired.current || projectUnavailable) return;
    if (run.isLoading || pipelineQuery.isLoading || projectQuery.isLoading) return;
    const clearParam = () =>
      void navigate({
        to: "/studio/$project",
        params: { project },
        search: {},
        replace: true,
      });
    if (status !== "manual") {
      autostartFired.current = true;
      clearParam();
      return;
    }
    if (!importSettled) return;
    autostartFired.current = true;
    clearParam();
    startRunMutation(
      {
        // 一键成片读项目配置：用户在上传页选的风格/人群/项目类型在这里生效，而不是
        // 覆盖成写死的默认值。
        config: studioRunConfig(projectQuery.data?.data),
      },
      { onError: (error) => setStartError(error.message) },
    );
  }, [
    autostart,
    importSettled,
    navigate,
    pipelineQuery.isLoading,
    project,
    projectQuery.data,
    projectQuery.isLoading,
    projectUnavailable,
    run.isLoading,
    startRunMutation,
    status,
  ]);

  if (projectUnavailable) {
    return (
      <div className="space-y-4">
        <Link
          to="/studio"
          className="inline-flex items-center gap-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft className="size-3.5" />
          返回我的剧
        </Link>
        <div className="rounded-2xl border border-dashed border-border/60 bg-card px-4 py-10 text-center">
          <h1 className="text-base font-semibold tracking-tight">项目不存在或不属于你</h1>
          <p className="mt-2 text-sm text-muted-foreground">
            这个链接可能已经失效。请从「我的剧」里打开自己的项目。
          </p>
        </div>
      </div>
    );
  }

  const projectName =
    summaries?.find((item) => item.id === project)?.name ?? "制作台";
  const currentEpisode = pipeline?.current_episode ?? null;
  const episodes = (episodesQuery.data?.data ?? []).map((episode) => ({
    number: episode.number,
    title: episode.title,
  }));
  const failedTask = (tasksQuery.data?.data ?? []).find(
    (task) => task.status === "failed",
  );
  // 分镜后的成本闸门：客户工作台停在这里等客户点头，而不是自动确认（见 use-studio-run）。
  const costGateEpisode =
    run.run?.status === "paused" && run.run?.gate === "cost_estimate"
      ? (run.run.episode ?? currentEpisode ?? 1)
      : null;
  // 客户工作台停在的闸门（含没有 tag 的声线策略）：都要客户自己点，见 use-studio-run。
  const gateKind = run.gateKind;
  const activeGateEpisode = run.run?.episode ?? currentEpisode ?? 1;
  // 服务端因审核/重试耗尽而本地降级、跳过的镜头：顶部错误条 + 逐镜标记（不假死）。
  const blockedShots = run.run?.blocked_shots ?? [];

  /**
   * 「重做该镜」：先重跑这一镜，**等它真的成功**再清掉降级标记。
   *
   * 以前是「触发后就立刻清标记」的乐观清理——重做再次失败时标记已经没了，客户会
   * 以为那镜好了。现在只在该镜任务 `completed` 后清；失败 / 超时都保留标记并把
   * 原因摆给客户看。
   */
  const handleRedoBlockedShot = async (shot: DirectorBlockedShot) => {
    setRedoError(null);
    setRedoPendingBeat(shot.beat);
    try {
      const { taskId, previousTaskId } = await regenerateStudioBeatVideo(
        project,
        shot.episode,
        shot.beat,
      );
      const result = await waitForStudioBeatVideo(project, shot.episode, shot.beat, {
        taskId,
        previousTaskId,
      });
      if (!result.ok) {
        setRedoError(result.error);
        return;
      }
      await clearBlockedShot.mutateAsync({ episode: shot.episode, beat: shot.beat });
    } catch (error) {
      setRedoError(error instanceof Error ? error.message : String(error));
    } finally {
      setRedoPendingBeat(null);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Link
          to="/studio"
          className="inline-flex items-center gap-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft className="size-3.5" />
          返回
        </Link>
        <h1 className="min-w-0 flex-1 truncate text-lg font-semibold tracking-tight">
          {projectName}
        </h1>
      </div>

      {!importSettled ? (
        <div className="flex items-center gap-2 rounded-xl border border-border/60 bg-card px-4 py-3 text-sm">
          <Loader2 className="size-4 animate-spin text-primary" />
          <span>
            正在导入剧本
            {pipeline?.next_step_name ? ` · ${pipeline.next_step_name}` : "…"}
          </span>
        </div>
      ) : null}

      {startError ? (
        <StudioHumanNotice message={`启动制作失败：${startError}`} />
      ) : null}
      {run.needsHuman && gateKind !== "cost_estimate" && gateKind !== "voice_setup" ? (
        // 闸门暂停也要有横幅：只有一张卡片时，客户很容易以为工作台没反应。
        <StudioHumanNotice message={run.needsHuman} />
      ) : null}
      {gateKind === "cost_estimate" && !quoteDismissed ? (
        <StudioQuoteCard
          project={project}
          episode={costGateEpisode ?? activeGateEpisode}
          onDismiss={() => setQuoteDismissed(true)}
        />
      ) : gateKind === "character_images" || gateKind === "identity_images" ? (
        <DirectorCharacterImagesCard
          project={project}
          gate={gateKind}
          episode={activeGateEpisode}
          variant="card"
          autoSkipSeconds={
            gateKind === "identity_images" ? IDENTITY_GATE_GRACE_SECONDS : undefined
          }
          onResolved={() => undefined}
        />
      ) : gateKind === "voice_setup" ? (
        <StudioVoiceGateCard project={project} episode={activeGateEpisode} />
      ) : null}
      {failedTask?.error ? (
        // 与闸门横幅并列，不互相顶掉：一边是「在等你决定」，一边是「有任务真失败了」。
        <StudioHumanNotice message={`有任务失败：${failedTask.error}`} />
      ) : null}
      {run.stuckLikely ? (
        <StudioHumanNotice message="制作已经有一段时间没有进展，可能在跑一个很长的步骤；如果一直不动，可以点「继续制作」重试当前步骤，或「终止自动」。" />
      ) : null}
      <StudioBlockedShots
        shots={blockedShots}
        onRedo={handleRedoBlockedShot}
        pendingBeat={redoPendingBeat}
      />
      {redoError ? <StudioHumanNotice message={`重做该镜未成功：${redoError}`} /> : null}

      <section className="space-y-3 rounded-2xl border border-border/60 bg-card p-4">
        <StudioRunBanner
          run={run.run}
          needsHuman={run.needsHuman}
          recoveries={run.recoveries}
          onResume={run.resumeManually}
          resumePending={run.resumePending}
          resumeBlocked={gateKind !== null}
          onStop={run.stopManually}
          stopPending={run.stopPending}
          stuckLikely={run.stuckLikely}
        />
        <StudioStageChecklist pipeline={pipeline} />
      </section>

      <section className="space-y-3">
        <h2 className="text-sm font-semibold text-muted-foreground">成片与费用</h2>
        <StudioEpisodeList
          project={project}
          episodes={episodes}
          currentEpisode={currentEpisode}
          runActive={runActive || !importSettled}
        />
      </section>

      <section className="space-y-3 rounded-2xl border border-border/60 bg-card p-4">
        <h2 className="text-sm font-semibold text-muted-foreground">费用总览</h2>
        <StudioCostOverview project={project} />
      </section>
    </div>
  );
}
