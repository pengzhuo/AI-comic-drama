// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import type { TaskState } from "./types";

/** One recorded provider/model invocation inside a task. */
export interface TaskModelCall {
  model?: string;
  provider?: string;
  capability?: string;
  request_id?: string;
  source?: string;
  status?: string;
  error?: string;
  at?: number;
}

/**
 * Structured failure diagnostics attached by the task runner
 * (`metadata.failure`).  Rendered as the expandable "failure detail" block.
 */
export interface TaskFailureDetail {
  exception_type?: string;
  message?: string;
  traceback?: string;
  operation?: string;
  recent_operations?: string[];
  model_calls?: TaskModelCall[];
  elapsed_seconds?: number;
  task_type?: string;
  episode?: number;
  beat_num?: number;
  scope?: string;
  task_id?: string;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function asStringArray(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const items = value
    .map((item) => (typeof item === "string" ? item.trim() : ""))
    .filter(Boolean);
  return items.length ? items : undefined;
}

function asModelCalls(value: unknown): TaskModelCall[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const calls = value
    .map((item) => asRecord(item))
    .filter((item): item is Record<string, unknown> => item !== null)
    .map((item) => ({
      model: asString(item.model),
      provider: asString(item.provider),
      capability: asString(item.capability),
      request_id: asString(item.request_id),
      source: asString(item.source),
      status: asString(item.status),
      error: asString(item.error),
      at: typeof item.at === "number" ? item.at : undefined,
    }));
  return calls.length ? calls : undefined;
}

/** Coerce an unknown payload (task metadata or a ws frame) into failure detail. */
export function normalizeTaskFailure(value: unknown): TaskFailureDetail | null {
  const record = asRecord(value);
  if (!record) return null;
  const detail: TaskFailureDetail = {};
  const strings: Array<[keyof TaskFailureDetail, unknown]> = [
    ["exception_type", record.exception_type],
    ["message", record.message],
    ["traceback", record.traceback],
    ["operation", record.operation],
    ["task_type", record.task_type],
    ["scope", record.scope],
    ["task_id", record.task_id],
  ];
  for (const [key, raw] of strings) {
    const text = asString(raw);
    if (text) {
      (detail as Record<string, unknown>)[key] = text;
    }
  }
  const recentOperations = asStringArray(record.recent_operations);
  if (recentOperations) detail.recent_operations = recentOperations;
  const modelCalls = asModelCalls(record.model_calls);
  if (modelCalls) detail.model_calls = modelCalls;
  const elapsed = record.elapsed_seconds;
  if (typeof elapsed === "number" && Number.isFinite(elapsed)) {
    detail.elapsed_seconds = elapsed;
  }
  const episode = record.episode;
  if (typeof episode === "number" && Number.isFinite(episode)) {
    detail.episode = episode;
  }
  const beatNum = record.beat_num;
  if (typeof beatNum === "number" && Number.isFinite(beatNum)) {
    detail.beat_num = beatNum;
  }
  return Object.keys(detail).length ? detail : null;
}

function failureFrom(container: unknown): TaskFailureDetail | null {
  const record = asRecord(container);
  return record ? normalizeTaskFailure(record.failure) : null;
}

/** Read `metadata.failure` (or `result.task_metadata.failure`) off a task. */
export function extractTaskFailure(task: TaskState): TaskFailureDetail | null {
  const direct = failureFrom(task.metadata);
  if (direct) return direct;
  const result = asRecord(task.result);
  return failureFrom(result?.task_metadata);
}

/** True when there is something worth opening a detail block for. */
export function hasFailureDetails(failure: TaskFailureDetail | null): boolean {
  if (!failure) return false;
  return Boolean(
    failure.operation ||
      failure.exception_type ||
      failure.message ||
      failure.traceback ||
      failure.model_calls?.length,
  );
}
