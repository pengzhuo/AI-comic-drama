// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useEffect, useRef, useState } from "react";
import { Link, createFileRoute, useNavigate } from "@tanstack/react-router";
import { ArrowLeft, Loader2 } from "lucide-react";

import { StudioCostOverview } from "@/features/studio/studio-cost";
import { StudioEpisodeList } from "@/features/studio/studio-episode-list";
import {
  StudioHumanNotice,
  StudioRunBanner,
  StudioStageChecklist,
} from "@/features/studio/studio-run";
import { STUDIO_RUN_CONFIG, isImportSettled } from "@/features/studio/studio-api";
import { useStudioPolling } from "@/features/studio/use-studio-polling";
import { useStudioRun } from "@/features/studio/use-studio-run";
import { useStartProjectAutoRun } from "@/lib/queries/director-auto";
import { useEpisodes, usePipelineStatus } from "@/lib/queries/episodes";
import { useAllProjectSummaries } from "@/lib/queries/projects";
import { useTasks } from "@/lib/queries/tasks";

export const Route = createFileRoute("/studio/$project")({
  // `?autostart=1` 由上传页带过来：导入一完成就自动开跑「一键成片」。
  validateSearch: (search: Record<string, unknown>): { autostart?: boolean } => ({
    autostart:
      search.autostart === true ||
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
  const { data: summaries } = useAllProjectSummaries();
  const startRun = useStartProjectAutoRun(project);
  const run = useStudioRun(project, { auto: true });

  const [startError, setStartError] = useState<string | null>(null);

  const pipeline = pipelineQuery.data?.data;
  const importSettled = isImportSettled(pipeline?.global);
  const status = run.run?.status ?? "manual";
  const runActive =
    status === "running" || status === "awaiting_confirmation" || status === "paused";

  useStudioPolling(project, { enabled: !importSettled || runActive });

  // 导入完成后自动开跑；跑起来就把 URL 参数清掉，刷新不会重复启动。
  const autostartFired = useRef(false);
  const startRunMutation = startRun.mutate;
  useEffect(() => {
    if (!autostart || autostartFired.current) return;
    if (run.isLoading || pipelineQuery.isLoading) return;
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
      { voicePolicy: "system", config: { ...STUDIO_RUN_CONFIG } },
      { onError: (error) => setStartError(error.message) },
    );
  }, [
    autostart,
    importSettled,
    navigate,
    pipelineQuery.isLoading,
    project,
    run.isLoading,
    startRunMutation,
    status,
  ]);

  const projectName =
    summaries?.find((item) => item.id === project)?.name ?? "制作中";
  const currentEpisode = pipeline?.current_episode ?? null;
  const episodes = (episodesQuery.data?.data ?? []).map((episode) => ({
    number: episode.number,
    title: episode.title,
  }));
  const failedTask = (tasksQuery.data?.data ?? []).find(
    (task) => task.status === "failed",
  );

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
      {run.needsHuman ? <StudioHumanNotice message={run.needsHuman} /> : null}
      {!run.needsHuman && failedTask?.error ? (
        <StudioHumanNotice message={`有任务失败：${failedTask.error}`} />
      ) : null}

      <section className="space-y-3 rounded-2xl border border-border/60 bg-card p-4">
        <StudioRunBanner
          run={run.run}
          needsHuman={run.needsHuman}
          recoveries={run.recoveries}
          onResume={run.resumeManually}
          resumePending={run.resumePending}
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
