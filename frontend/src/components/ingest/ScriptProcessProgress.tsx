// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Loader2, Sparkles } from "lucide-react";

import { Progress } from "@/components/ui/progress";
import { isScriptProcessRunning, useScriptProcess } from "@/lib/queries/ingest";
import { cn } from "@/lib/utils";

/** How often the bar re-reads the status while the automatic stage is pending. */
const WAIT_POLL_MS = 3000;

interface ScriptProcessProgressProps {
  project: string;
  /** Whether to read the stage status at all. */
  enabled?: boolean;
  /**
   * How long to keep asking for the automatic stage after the import finished.
   *
   * The import chains 「通用AI影视制作流程包」script processing, so a short
   * window where nothing exists yet is normal. Only the session that ran the
   * import passes this (`0` = read once and poll only while the stage is live),
   * so returning to the page later never pokes the endpoint forever.
   */
  watchMs?: number;
  className?: string;
}

/**
 * Compact status line for the script-processing stage that runs right after
 * 导入剧本. Replaces the old 「剧本处理」 card: the stage is automatic, so the
 * page only needs to show how far it is, not a manual trigger.
 */
export function ScriptProcessProgress({
  project,
  enabled = true,
  watchMs = 0,
  className,
}: ScriptProcessProgressProps) {
  const { t } = useTranslation();
  const [watching, setWatching] = useState(watchMs > 0);

  useEffect(() => {
    if (watchMs <= 0) return;
    setWatching(true);
    const timer = window.setTimeout(() => setWatching(false), watchMs);
    return () => window.clearTimeout(timer);
  }, [watchMs]);

  const status = useScriptProcess(project, enabled, {
    pollWhileWaitingMs: watching ? WAIT_POLL_MS : 0,
  });

  const data = status.data;
  const taskStatus = data?.task?.status;
  const running = isScriptProcessRunning(taskStatus);
  const failed = taskStatus === "failed";
  const hasResult = data?.has_result ?? false;

  // As soon as the stage is visible the hook's own live polling takes over, and
  // once it has produced a result there is nothing left to wait for.
  useEffect(() => {
    if (running || failed || hasResult) setWatching(false);
  }, [running, failed, hasResult]);

  const percent = Math.round((data?.task?.progress ?? 0) * 100);
  const stateLabel = running
    ? t("ingest.scriptProcessRunning")
    : failed
      ? t("ingest.scriptProcessFailed")
      : hasResult
        ? t("ingest.scriptProcessDone")
        : t("ingest.scriptProcessIdle");
  const result = data?.result ?? null;

  return (
    <div className={cn("rounded-lg border px-4 py-3", className)}>
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <span className="flex shrink-0 items-center gap-1.5 font-medium text-muted-foreground">
          {running ? (
            <Loader2 className="size-3.5 animate-spin" aria-hidden="true" />
          ) : (
            <Sparkles className="size-3.5" aria-hidden="true" />
          )}
          {t("ingest.scriptProcessTitle")}
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
            {t("ingest.scriptProcessSummary", {
              characters: result.characters?.length ?? 0,
              scenes: result.scenes?.length ?? 0,
              gaps: result.gaps?.length ?? 0,
            })}
          </span>
        ) : null}
        {hasResult && (result?.degraded_chunks ?? 0) > 0 ? (
          <span className="shrink-0 text-amber-600 dark:text-amber-500">
            {t("ingest.scriptProcessDegraded", {
              count: result?.degraded_chunks ?? 0,
            })}
          </span>
        ) : null}
      </div>
      {running ? <Progress className="mt-2 h-1" value={percent} /> : null}
    </div>
  );
}
