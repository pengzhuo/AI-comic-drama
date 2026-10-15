// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useTranslation } from "react-i18next";
import type { TaskFailureDetail, TaskModelCall } from "@/task-center/task-failure";
import { hasFailureDetails } from "@/task-center/task-failure";

function modelCallLabel(call: TaskModelCall): string {
  return call.model || call.provider || "—";
}

/**
 * Expandable "what was running / which model was called" block for a failed or
 * stalled task.  Shared by the task-center detail pane and the one-click stall
 * dialog so an operator sees the same evidence in both places.
 */
export function TaskFailureDetails({
  failure,
  summaryLabel,
  defaultOpen = true,
}: {
  failure: TaskFailureDetail | null;
  summaryLabel?: string;
  defaultOpen?: boolean;
}) {
  const { t } = useTranslation();
  if (!hasFailureDetails(failure)) return null;
  const detail = failure as TaskFailureDetail;
  const operations = detail.recent_operations ?? [];
  const modelCalls = detail.model_calls ?? [];

  return (
    <details
      open={defaultOpen}
      className="mt-2 rounded border border-border/60 bg-background/40 p-2 text-xs"
    >
      <summary className="cursor-pointer text-muted-foreground">
        {summaryLabel ?? t("taskCenter.detail.failure.title")}
      </summary>
      <div className="mt-2 grid gap-2">
        {detail.operation ? (
          <div>
            <div className="text-muted-foreground">
              {t("taskCenter.detail.failure.operation")}
            </div>
            <div className="mt-0.5 break-words">{detail.operation}</div>
          </div>
        ) : null}
        {operations.length > 1 ? (
          <div>
            <div className="text-muted-foreground">
              {t("taskCenter.detail.failure.recentOperations")}
            </div>
            <ol className="mt-0.5 list-decimal space-y-0.5 pl-4">
              {operations.map((item, index) => (
                <li key={`${index}-${item}`} className="break-words">
                  {item}
                </li>
              ))}
            </ol>
          </div>
        ) : null}
        <div>
          <div className="text-muted-foreground">
            {t("taskCenter.detail.failure.modelCalls")}
          </div>
          {modelCalls.length ? (
            <ul className="mt-0.5 space-y-1">
              {modelCalls.map((call, index) => (
                <li
                  key={`${index}-${modelCallLabel(call)}`}
                  className="rounded bg-muted/50 px-2 py-1 font-mono text-[11px]"
                >
                  <div className="break-all">
                    <span>{modelCallLabel(call)}</span>
                    {call.capability ? (
                      <span className="text-muted-foreground"> · {call.capability}</span>
                    ) : null}
                    <span
                      className={
                        call.status === "error"
                          ? "text-destructive"
                          : "text-muted-foreground"
                      }
                    >
                      {" "}
                      ·{" "}
                      {call.status === "error"
                        ? t("taskCenter.detail.failure.requestFailed")
                        : t("taskCenter.detail.failure.requestOk")}
                    </span>
                  </div>
                  {call.request_id ? (
                    <div className="break-all text-muted-foreground">
                      {t("taskCenter.detail.failure.requestId")}: {call.request_id}
                    </div>
                  ) : null}
                  {call.error ? (
                    <div className="break-words text-destructive">{call.error}</div>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
            <div className="mt-0.5 text-muted-foreground">
              {t("taskCenter.detail.failure.noModelCalls")}
            </div>
          )}
        </div>
        {detail.exception_type || detail.message ? (
          <div>
            <div className="text-muted-foreground">
              {t("taskCenter.detail.failure.exception")}
            </div>
            <div className="mt-0.5 break-words">
              {detail.exception_type ? (
                <span className="font-mono">{detail.exception_type}</span>
              ) : null}
              {detail.message ? (
                <span>{detail.exception_type ? " " : ""}{detail.message}</span>
              ) : null}
            </div>
          </div>
        ) : null}
        {detail.traceback ? (
          <details className="rounded border border-border/60 bg-muted/30 p-1.5">
            <summary className="cursor-pointer text-muted-foreground">
              {t("taskCenter.detail.failure.traceback")}
            </summary>
            <pre className="mt-1.5 max-h-52 overflow-auto whitespace-pre-wrap break-all font-mono text-[11px]">
              {detail.traceback}
            </pre>
          </details>
        ) : null}
        {typeof detail.elapsed_seconds === "number" ? (
          <div className="text-muted-foreground">
            {t("taskCenter.detail.failure.elapsed", {
              seconds: Math.round(detail.elapsed_seconds),
            })}
          </div>
        ) : null}
      </div>
    </details>
  );
}
