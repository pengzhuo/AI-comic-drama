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

import { startStudioIngest } from "@/features/studio/studio-api";

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
