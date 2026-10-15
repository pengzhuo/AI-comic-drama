// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { describe, expect, it } from "vitest";
import {
  extractTaskFailure,
  hasFailureDetails,
  normalizeTaskFailure,
} from "@/task-center/task-failure";
import type { TaskState } from "@/task-center/types";

function buildTask(overrides: Partial<TaskState> = {}): TaskState {
  return {
    task_key: "key",
    task_id: "tid",
    task_type: "global_optimize_video",
    username: "u",
    project: "p",
    episode: 1,
    beat_num: null,
    scope: null,
    status: "failed",
    progress: 0,
    current_task: "",
    result: null,
    error: "boom",
    ...overrides,
  } as TaskState;
}

describe("task-failure", () => {
  it("normalizes a full failure payload", () => {
    const detail = normalizeTaskFailure({
      exception_type: "RuntimeError",
      message: "provider 500",
      operation: "正在全局优化镜头 2/5",
      recent_operations: ["a", "b"],
      model_calls: [
        {
          model: "gemini-2.5-pro",
          capability: "render",
          request_id: "req_1",
          status: "error",
          error: "500",
        },
      ],
      elapsed_seconds: 12.6,
      episode: 2,
      beat_num: 3,
      task_id: "tid",
    });
    expect(detail).not.toBeNull();
    expect(detail?.operation).toBe("正在全局优化镜头 2/5");
    expect(detail?.recent_operations).toEqual(["a", "b"]);
    expect(detail?.model_calls?.[0].model).toBe("gemini-2.5-pro");
    expect(detail?.model_calls?.[0].request_id).toBe("req_1");
    expect(detail?.elapsed_seconds).toBe(12.6);
  });

  it("returns null for empty or non-object payloads", () => {
    expect(normalizeTaskFailure(null)).toBeNull();
    expect(normalizeTaskFailure("nope")).toBeNull();
    expect(normalizeTaskFailure({})).toBeNull();
    expect(normalizeTaskFailure({ unrelated: 1 })).toBeNull();
  });

  it("reads failure from task metadata", () => {
    const task = buildTask({
      metadata: { failure: { operation: "上传结果", exception_type: "IOError" } },
    });
    const detail = extractTaskFailure(task);
    expect(detail?.operation).toBe("上传结果");
    expect(hasFailureDetails(detail)).toBe(true);
  });

  it("falls back to result.task_metadata", () => {
    const task = buildTask({
      result: { task_metadata: { failure: { message: "deep failure" } } },
    });
    expect(extractTaskFailure(task)?.message).toBe("deep failure");
  });

  it("returns null when a task has no failure payload", () => {
    expect(extractTaskFailure(buildTask())).toBeNull();
    expect(hasFailureDetails(null)).toBe(false);
  });
});
