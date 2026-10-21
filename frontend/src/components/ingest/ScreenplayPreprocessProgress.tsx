// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader2, Wand2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import {
  isScreenplayPreprocessRunning,
  useScreenplayPreprocess,
  useStartScreenplayPreprocess,
} from "@/lib/queries/ingest";
import { cn } from "@/lib/utils";

/** How often the bar re-reads the status while the automatic stage is pending. */
const WAIT_POLL_MS = 3000;

interface ScreenplayPreprocessProgressProps {
  project: string;
  /** Whether to read the stage status at all. */
  enabled?: boolean;
  /**
   * How long to keep asking for the automatic stage after the import finished.
   *
   * The import chains the deterministic preprocessing stage, so a short window
   * where nothing exists yet is normal. Only the session that ran the import
   * passes this (`0` = read once and poll only while the stage is live), so
   * returning to the page later never pokes the endpoint forever.
   */
  watchMs?: number;
  className?: string;
}

/**
 * Status line for 剧本预处理 — the deterministic stage that runs between 导入
 * 剧本 and 剧本处理: it normalizes the imported scene headings into the single
 * line shape every later stage consumes. Automatic, so this reports progress,
 * and offers a manual (re)run for projects imported before the stage existed.
 */
export function ScreenplayPreprocessProgress({
  project,
  enabled = true,
  watchMs = 0,
  className,
}: ScreenplayPreprocessProgressProps) {
  const { t } = useTranslation();
  const [watching, setWatching] = useState(watchMs > 0);

  useEffect(() => {
    if (watchMs <= 0) return;
    setWatching(true);
    const timer = window.setTimeout(() => setWatching(false), watchMs);
    return () => window.clearTimeout(timer);
  }, [watchMs]);

  const status = useScreenplayPreprocess(project, enabled, {
    pollWhileWaitingMs: watching ? WAIT_POLL_MS : 0,
  });
  const start = useStartScreenplayPreprocess(project);

  const data = status.data;
  const taskStatus = data?.task?.status;
  const running = isScreenplayPreprocessRunning(taskStatus);
  const failed = taskStatus === "failed";
  const hasResult = data?.has_result ?? false;

  // As soon as the stage is visible the hook's own live polling takes over, and
  // once it has produced a result there is nothing left to wait for.
  useEffect(() => {
    if (running || failed || hasResult) setWatching(false);
  }, [running, failed, hasResult]);

  const percent = Math.round((data?.task?.progress ?? 0) * 100);
  const stateLabel = running
    ? t("ingest.screenplayPreprocessRunning")
    : failed
      ? t("ingest.screenplayPreprocessFailed")
      : hasResult
        ? t("ingest.screenplayPreprocessDone")
        : t("ingest.screenplayPreprocessIdle");
  const result = data?.result ?? null;
  const stats = result?.stats ?? {};
  const changed = result?.changed ?? false;

  return (
    <div className={cn("rounded-lg border px-4 py-3", className)}>
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <span className="flex shrink-0 items-center gap-1.5 font-medium text-muted-foreground">
          {running ? (
            <Loader2 className="size-3.5 animate-spin" aria-hidden="true" />
          ) : (
            <Wand2 className="size-3.5" aria-hidden="true" />
          )}
          {t("ingest.screenplayPreprocessTitle")}
        </span>
        <span
          className={cn(
            "shrink-0 font-medium",
            running ? "text-foreground" : "text-muted-foreground",
          )}
        >
          {stateLabel}
        </span>
        {running ? (
          <span className="shrink-0 font-mono tabular-nums text-muted-foreground">
            {percent}%
          </span>
        ) : null}
        {running && data?.task?.current_task ? (
          <span className="min-w-0 flex-1 truncate text-muted-foreground">
            {data.task.current_task}
          </span>
        ) : null}
        {hasResult && result ? (
          <span className="min-w-0 flex-1 truncate text-muted-foreground">
            {changed
              ? t("ingest.screenplayPreprocessSummary", {
                  merged: stats.split_headings_merged ?? 0,
                  headings: result.scene_headings ?? 0,
                })
              : t("ingest.screenplayPreprocessUnchanged")}
          </span>
        ) : null}
        {hasResult && (result?.episodes_updated ?? 0) > 0 ? (
          <span className="shrink-0 text-muted-foreground">
            {t("ingest.screenplayPreprocessEpisodes", {
              count: result?.episodes_updated ?? 0,
            })}
          </span>
        ) : null}
        {start.isError ? (
          <span className="shrink-0 text-destructive">
            {t("ingest.screenplayPreprocessStartFailed")}
          </span>
        ) : null}
        {!running && enabled ? (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="ml-auto shrink-0"
            disabled={start.isPending}
            onClick={() => start.mutate({ force: hasResult })}
          >
            {hasResult
              ? t("ingest.screenplayPreprocessRerun")
              : t("ingest.screenplayPreprocessRun")}
          </Button>
        ) : null}
      </div>
      {running ? <Progress className="mt-2 h-1" value={percent} /> : null}
    </div>
  );
}
