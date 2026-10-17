// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useTranslation } from "react-i18next";
import { Loader2, RefreshCw, Sparkles } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  isScriptProcessRunning,
  useScriptProcess,
  useStartScriptProcess,
} from "@/lib/queries/ingest";
import { cn } from "@/lib/utils";

interface ScriptProcessCardProps {
  project: string;
  /**
   * Whether to fetch status. Only mount this once a script has been imported;
   * the card surfaces the automatic post-import stage as well as a manual
   * re-run.
   */
  enabled?: boolean;
  className?: string;
}

/**
 * "剧本处理" card: shows the state of the film-process-pack script processing
 * stage that runs automatically right after 导入剧本, and lets the user re-run it.
 */
export function ScriptProcessCard({
  project,
  enabled = true,
  className,
}: ScriptProcessCardProps) {
  const { t } = useTranslation();
  const statusQuery = useScriptProcess(project, enabled);
  const startMutation = useStartScriptProcess(project);

  const data = statusQuery.data;
  const taskStatus = data?.task?.status;
  const running = isScriptProcessRunning(taskStatus);
  const hasResult = data?.has_result ?? false;
  const result = data?.result ?? null;

  const stateLabel = running
    ? t("ingest.scriptProcessRunning")
    : taskStatus === "failed"
      ? t("ingest.scriptProcessFailed")
      : hasResult
        ? t("ingest.scriptProcessDone")
        : t("ingest.scriptProcessIdle");

  const busy = running || startMutation.isPending;
  const error = startMutation.error;

  return (
    <div
      className={cn("overflow-hidden rounded-lg border p-4", className)}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className="flex items-center gap-2 text-xs font-medium uppercase tracking-wider text-muted-foreground">
            <Sparkles className="size-3.5" aria-hidden="true" />
            {t("ingest.scriptProcessTitle")}
          </p>
          <p className="mt-2 text-xs leading-5 text-muted-foreground">
            {t("ingest.scriptProcessDesc")}
          </p>
        </div>
        <Button
          size="sm"
          variant={hasResult ? "outline" : "default"}
          disabled={busy}
          onClick={() => startMutation.mutate({ force: hasResult })}
        >
          {busy ? (
            <Loader2 className="size-3.5 animate-spin" aria-hidden="true" />
          ) : (
            <RefreshCw className="size-3.5" aria-hidden="true" />
          )}
          {hasResult
            ? t("ingest.scriptProcessRerun")
            : t("ingest.scriptProcessStart")}
        </Button>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <span
          className={cn(
            "font-medium",
            running ? "text-foreground" : "text-muted-foreground",
          )}
        >
          {stateLabel}
        </span>
        {running && typeof data?.task?.progress === "number" ? (
          <span className="font-mono tabular-nums text-muted-foreground">
            {Math.round(data.task.progress * 100)}%
          </span>
        ) : null}
        {hasResult && result ? (
          <span className="text-muted-foreground">
            {t("ingest.scriptProcessSummary", {
              characters: result.characters?.length ?? 0,
              scenes: result.scenes?.length ?? 0,
              gaps: result.gaps?.length ?? 0,
            })}
          </span>
        ) : null}
        {hasResult && (result?.degraded_chunks ?? 0) > 0 ? (
          <span className="text-amber-600 dark:text-amber-500">
            {t("ingest.scriptProcessDegraded", {
              count: result?.degraded_chunks ?? 0,
            })}
          </span>
        ) : null}
      </div>

      {running && data?.task?.current_task ? (
        <p className="mt-2 truncate text-xs text-muted-foreground">
          {data.task.current_task}
        </p>
      ) : null}

      {error ? (
        <p className="mt-2 text-xs leading-5 text-destructive">
          {error instanceof Error ? error.message : String(error)}
        </p>
      ) : null}
    </div>
  );
}
