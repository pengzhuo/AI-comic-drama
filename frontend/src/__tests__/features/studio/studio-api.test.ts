// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import ky from "ky";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", () => ({
  api: ky.create({ baseUrl: "http://localhost:3000/" }),
  uploadApi: ky.create({ baseUrl: "http://localhost:3000/" }),
}));

import {
  isImportRunning,
  isImportSettled,
  regenerateStudioBeatVideo,
  startStudioIngest,
  waitForStudioBeatVideo,
} from "@/features/studio/studio-api";

const server = setupServer();

beforeAll(() => server.listen());
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const START = "http://localhost:3000/api/v1/projects/demo/ingest/start";

/**
 * `start_ingest` 只在 `rebuild=true` 时放行 `spine_template`；工作室若漏发 rebuild，
 * 后端会直接拒绝（"项目类型只能在重新导入时修改"），`ingest_fast` 不入队，导入后的
 * 剧本预处理也就永远不会跑。工作室还发 `format_policy=warn`：精品剧缺场景头不拦，
 * 而是把体检结果带回来提示。这些把契约钉住。
 */
describe("studio import start payload", () => {
  it("sends rebuild:true and format_policy:warn alongside the spine template", async () => {
    let body: Record<string, unknown> = {};
    server.use(
      http.post(START, async ({ request }) => {
        body = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          ok: true,
          task_type: "ingest_fast",
          task_id: "task-1",
          message: "导入任务已进入队列",
        });
      }),
    );

    const response = await startStudioIngest("demo", "剧本.txt", "drama");

    expect(response.task_type).toBe("ingest_fast");
    expect(body).toMatchObject({
      filename: "剧本.txt",
      spine_template: "drama",
      rebuild: true,
      format_policy: "warn",
    });
  });

  it("carries the format advisory back when the backend warns", async () => {
    server.use(
      http.post(START, () =>
        HttpResponse.json({
          ok: true,
          task_type: "ingest_fast",
          task_id: "task-1",
          message: "导入任务已进入队列",
          format_check: { level: "blocking", summary: "缺场景头" },
        }),
      ),
    );

    const response = await startStudioIngest("demo", "剧本.txt", "drama");

    expect(response.format_check?.level).toBe("blocking");
  });

  it("surfaces an ok:false refusal instead of pretending the import queued", async () => {
    server.use(
      http.post(START, () =>
        HttpResponse.json({ ok: false, error: "项目类型只能在重新导入时修改" }),
      ),
    );

    await expect(startStudioIngest("demo", "剧本.txt", "drama")).rejects.toThrow(
      "项目类型只能在重新导入时修改",
    );
  });
});

/**
 * 导入链的**三态**判定：`isImportSettled` 只说「还没跑完」，工作台还要知道「到底在不在
 * 跑」——在跑就转圈、没在跑就给「开始制作」出口。现场 `t3` 永远卡在「正在导入剧本」，
 * 就是这两种情况被混成了同一个转圈。
 */
describe("导入三态判定", () => {
  it("没导入完、也没在跑 → 两侧都为 false（工作台给出口）", () => {
    const global = { ingested: false };
    expect(isImportSettled(global)).toBe(false);
    expect(isImportRunning(global, [])).toBe(false);
  });

  it("管道自己报 pending → 算在跑", () => {
    expect(isImportRunning({ script_process_pending: true }, [])).toBe(true);
    expect(isImportRunning({ screenplay_preprocess_pending: true }, [])).toBe(true);
  });

  it("任务表里还有活的导入类任务 → 也算在跑", () => {
    expect(
      isImportRunning({ ingested: false }, [{ task_type: "ingest_fast", status: "running" }]),
    ).toBe(true);
    expect(
      isImportRunning({ ingested: false }, [{ task_type: "script_process", status: "queued" }]),
    ).toBe(true);
  });

  it("导入类任务已到终态 / 无关任务 → 不算在跑", () => {
    expect(
      isImportRunning({ ingested: false }, [{ task_type: "ingest_fast", status: "completed" }]),
    ).toBe(false);
    expect(
      isImportRunning({ ingested: false }, [{ task_type: "single_video", status: "running" }]),
    ).toBe(false);
  });

  it("两端都不 pending 且已摄入 → 导入完成", () => {
    expect(
      isImportSettled({ ingested: true, script_process_pending: false }),
    ).toBe(true);
  });
});

const TASK_GET = "http://localhost:3000/api/v1/projects/demo/tasks/single_video/1";const REDO_POST = "http://localhost:3000/api/v1/projects/demo/episodes/1/beats/39/video";

function makeTask(taskId: string, status: string, error?: string) {
  return {
    task_id: taskId,
    task_type: "single_video",
    username: "demo",
    project: "demo",
    episode: 1,
    beat_num: 39,
    status,
    progress: 0,
    error,
  };
}

/**
 * 「重做该镜」必须**成功才清标记**。
 *
 * 服务端一镜过不了时会本地降级并登记 `blocked_shots`。客户点重做后若又失败，
 * 标记绝不能提前消失——否则前端会假装那镜好了。这组测试把该契约钉住。
 */
describe("重做该镜：成功才清标记", () => {
  it("记下重做前那次失败的任务，并带回本次 task_id", async () => {
    let getCalls = 0;
    server.use(
      http.get(TASK_GET, () => {
        getCalls += 1;
        return HttpResponse.json({
          ok: true,
          data:
            getCalls === 1
              ? makeTask("old-task", "failed", "旧的那次失败")
              : makeTask("new-task", "completed"),
        });
      }),
      http.post(REDO_POST, () =>
        HttpResponse.json({
          ok: true,
          task_type: "single_video",
          task_id: "new-task",
          message: "queued",
        }),
      ),
    );

    await expect(regenerateStudioBeatVideo("demo", 1, 39)).resolves.toEqual({
      taskId: "new-task",
      previousTaskId: "old-task",
    });
  });

  it("等到该镜 completed 才算成功", async () => {
    server.use(
      http.get(TASK_GET, () =>
        HttpResponse.json({ ok: true, data: makeTask("new-task", "completed") }),
      ),
    );

    await expect(
      waitForStudioBeatVideo("demo", 1, 39, { taskId: "new-task", intervalMs: 5 }),
    ).resolves.toEqual({ ok: true });
  });

  it("新任务 failed 时不算成功，并带回原因", async () => {
    server.use(
      http.get(TASK_GET, () =>
        HttpResponse.json({
          ok: true,
          data: makeTask("new-task", "failed", "内容审核未通过"),
        }),
      ),
    );

    const result = await waitForStudioBeatVideo("demo", 1, 39, {
      taskId: "new-task",
      intervalMs: 5,
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("内容审核未通过");
  });

  it("轮询到旧任务时不误判成功（超时后保留标记）", async () => {
    server.use(
      http.get(TASK_GET, () =>
        HttpResponse.json({
          ok: true,
          data: makeTask("old-task", "failed", "旧的那次失败"),
        }),
      ),
    );

    const result = await waitForStudioBeatVideo("demo", 1, 39, {
      taskId: "new-task",
      previousTaskId: "old-task",
      intervalMs: 5,
      timeoutMs: 40,
    });
    expect(result.ok).toBe(false);
  });
});
