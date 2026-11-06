// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import ky from "ky";
import type { ReactNode } from "react";
import { createElement, useState } from "react";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", () => ({
  api: ky.create({ baseUrl: "http://localhost:3000/" }),
  uploadApi: ky.create({ baseUrl: "http://localhost:3000/" }),
}));

import {
  isBlockedPause,
  isStuckLikely,
  isVoicePolicyPause,
  pauseKindOf,
  useStudioRun,
} from "@/features/studio/use-studio-run";

/**
 * 阻塞型暂停不能被当成普通硬暂停自动续跑。
 *
 * 现场 ``nn_1104_1``：确定性派发找不到可做的 stage，agent 也没写出新任务；客户端每
 * 2.5s 自动 ``/continue`` 一次，服务端把阶段重试预算清零后继续空转——客户只看到
 * 「制作中」，进度永远不动。识别出这类原因后必须停手，把原因交给客户。
 */
describe("isBlockedPause", () => {
  it("识别「未产生写任务」暂停", () => {
    expect(isBlockedPause("自动续跑未产生写任务")).toBe(true);
    expect(isBlockedPause("自动续跑未产生写任务（最后一次工具调用：x）")).toBe(true);
  });

  it("识别「未产生新的任务」暂停", () => {
    expect(isBlockedPause("自动续跑未产生新的任务")).toBe(true);
  });

  it("普通失败 / 空原因不算阻塞", () => {
    expect(isBlockedPause("")).toBe(false);
    expect(isBlockedPause(null)).toBe(false);
    expect(isBlockedPause(undefined)).toBe(false);
    expect(isBlockedPause("有任务失败：Seedance2 请求超时")).toBe(false);
  });
});

/**
 * 声线策略暂停没有 `[tag]`，只能靠 ``last_error`` 的特征子串识别；命中后工作室要摆出
 * 声线选择卡，**不能**自动续跑（否则会用默认策略把客户的选择权绕过去）。
 */
describe("isVoicePolicyPause", () => {
  it("识别后端 VOICE_POLICY_REQUIRED_ERROR", () => {
    expect(
      isVoicePolicyPause("缺少配音声线，且未选择声线策略（系统声线／自定义声线）"),
    ).toBe(true);
  });

  it("普通暂停 / 角色声线缺失不算声线策略暂停", () => {
    expect(isVoicePolicyPause("")).toBe(false);
    expect(isVoicePolicyPause(null)).toBe(false);
    expect(isVoicePolicyPause("Beat 08 角色声线缺失：伊卡")).toBe(false);
    expect(isVoicePolicyPause("自动续跑未产生写任务")).toBe(false);
  });
});

/**
 * 审核未过 / 卡住 / 前置补不齐：过去这类暂停会「暂停↔续跑」死循环。
 *
 * 现在**服务端**负责不再假死：单镜做不出来就用本地素材降级该镜并跳过继续
 * （`director_auto._block_shot_and_continue`），每次续跑都真的会往前走。所以客户端
 * 恢复「暂停即自动 /continue」，并靠服务端暴露的 `blocked_shots` 做顶部报错 + 逐镜
 * 标记——不再在客户端按关键词硬停（原先的 `isHumanDecisionPause` 已移除）。
 */
describe("hard-paused runs resume automatically", () => {
  it("审核类暂停不再被客户端当成「必须人工」而停手", () => {
    // 关键词直到后端都会命中，但客户端不据此停：只要不是阻塞型暂停 / 声线策略 / 闸门，
    // 就走自动续跑分支。这里用「不是 blocked、也不是 voice policy」间接断言。
    expect(isBlockedPause("内容审核未通过，需要人工处理")).toBe(false);
    expect(isVoicePolicyPause("内容审核未通过，需要人工处理")).toBe(false);
  });
});

/**
 * 身份图闸门自动放行。
 *
 * 现场 `系统标准格式版`：run 停在 `identity_images`（3/7 身份图），没人点卡片就一直
 * 挂着——「不指定就由 AI 生成」本来没有必须人做主的决定。工作台开着时给一段宽限期，
 * 到点替客户把「跳过」答了；真正需要客户点头的角色肖像 / 报价闸门仍然停下等人。
 */

const server = setupServer();

beforeAll(() => server.listen());
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

function wrapper({ children }: { children: ReactNode }) {
  // 客户端要**每个挂载只建一次**：以前把 ``new QueryClient(...)`` 放在函数体里，
  // 组件每次重渲染都会换一个 client，mutation 会跟着 observer 重新挂载，测试里
  // 表现为同一次点击打了两遍接口。
  const [qc] = useState(
    () =>
      new QueryClient({
        defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
      }),
  );
  // `.ts`（非 `.tsx`）里不能写 JSX，用 createElement 保住纯 TS。
  return createElement(QueryClientProvider, { client: qc }, children);
}

function pausedRun(gate: string): Record<string, unknown> {
  return {
    status: "paused",
    episode: 1,
    run_id: `run-${gate}`,
    gate,
    last_error: `[${gate}] 本集还有身份没有身份图：可上传本地图片后继续，或直接跳过由 AI 生成`,
  };
}

function usePausedGate(gate: string, confirmed: unknown[], graceMs = 20) {
  server.use(
    http.get("http://localhost:3000/api/v1/projects/demo/chat/director-auto", () =>
      HttpResponse.json({ ok: true, data: pausedRun(gate) }),
    ),
    http.post(
      `http://localhost:3000/api/v1/projects/demo/chat/director-auto/gates/${gate}/confirm`,
      async ({ request }) => {
        confirmed.push(await request.json());
        return HttpResponse.json({ ok: true, data: { status: "running" } });
      },
    ),
  );
  return renderHook(() => useStudioRun("demo", { auto: true, identityGraceMs: graceMs }), {
    wrapper,
  });
}

describe("identity gate auto-advances after a grace window", () => {
  it("倒计时结束替客户点「跳过，由 AI 生成」", async () => {
    const confirmed: unknown[] = [];
    const { result } = usePausedGate("identity_images", confirmed);

    await waitFor(() => expect(result.current.gateKind).toBe("identity_images"));
    // 宽限期内先把「稍后会自动跳过」摆在顶部，客户仍来得及上传自己的图。
    expect(result.current.needsHuman ?? "").toContain("自动跳过");

    await waitFor(() => expect(confirmed).toHaveLength(1), { timeout: 2000 });
    expect(confirmed[0]).toEqual({ episode: 1 });
  });

  it("角色肖像闸门仍交给客户，不自动确认", async () => {
    const confirmed: unknown[] = [];
    const { result } = usePausedGate("character_images", confirmed);

    await waitFor(() => expect(result.current.gateKind).toBe("character_images"));
    // 宽限期是给身份图的；肖像闸门连「会自动跳过」都不该说。
    expect(result.current.needsHuman ?? "").not.toContain("自动跳过");

    await new Promise((resolve) => setTimeout(resolve, 80));
    expect(confirmed).toHaveLength(0);
  });
});

describe("pauseKindOf", () => {
  it("优先使用服务端下发的 pause_kind", () => {
    expect(
      pauseKindOf({ status: "paused", episode: 1, run_id: "r", pause_kind: "blocked" }),
    ).toBe("blocked");
  });

  it("老行没有 pause_kind 时按 last_error 兜底", () => {
    expect(
      pauseKindOf({ status: "paused", episode: 1, run_id: "r", gate: "identity_images" }),
    ).toBe("gate");
    expect(
      pauseKindOf({
        status: "paused",
        episode: 1,
        run_id: "r",
        last_error: "缺少配音声线，且未选择声线策略（系统声线／自定义声线）",
      }),
    ).toBe("voice_setup");
    expect(
      pauseKindOf({ status: "paused", episode: 1, run_id: "r", last_error: "自动续跑未产生写任务" }),
    ).toBe("blocked");
    expect(
      pauseKindOf({ status: "paused", episode: 1, run_id: "r", last_error: "项目配置缺失（如视觉风格）" }),
    ).toBe("needs_config");
    expect(
      pauseKindOf({ status: "paused", episode: 1, run_id: "r", last_error: "生成失败" }),
    ).toBeNull();
    expect(pauseKindOf(undefined)).toBeNull();
  });
});

describe("isStuckLikely", () => {
  it("运行中但很久没有更新算疑似卡住", () => {
    const old = new Date(Date.now() - 20 * 60 * 1000).toISOString();
    expect(
      isStuckLikely({ status: "running", episode: 1, run_id: "r", updated_at: old }),
    ).toBe(true);
  });

  it("刚更新过 / 非运行态 / 缺时间戳都不算", () => {
    const old = new Date(Date.now() - 20 * 60 * 1000).toISOString();
    expect(
      isStuckLikely({
        status: "running",
        episode: 1,
        run_id: "r",
        updated_at: new Date().toISOString(),
      }),
    ).toBe(false);
    expect(
      isStuckLikely({ status: "paused", episode: 1, run_id: "r", updated_at: old }),
    ).toBe(false);
    expect(isStuckLikely({ status: "running", episode: 1, run_id: "r" })).toBe(false);
  });
});

describe("认不出暂停原因也必须给出出口", () => {
  it("paused 但 last_error 认不出 kind → 仍然置 needsHuman（横幅出现「继续制作」）", async () => {
    server.use(
      http.get("http://localhost:3000/api/v1/projects/demo/chat/director-auto", () =>
        HttpResponse.json({
          ok: true,
          data: {
            status: "paused",
            episode: 1,
            run_id: "run-unknown",
            last_error: "生成失败",
          },
        }),
      ),
    );

    const { result } = renderHook(() => useStudioRun("demo", { auto: false }), { wrapper });
    await waitFor(() => expect(result.current.run?.status).toBe("paused"));
    // 以前这里 needsHuman 是 null：页面只剩一句「已暂停」，客户没有任何按钮可点。
    await waitFor(() => expect(result.current.needsHuman).toBeTruthy());
    expect(result.current.needsHuman).toContain("生成失败");
  });
});

describe("manual resume picks the endpoint by status", () => {
  it("awaiting_confirmation 走 /resume（resume_suspended），不再必失败", async () => {
    const calls: string[] = [];
    server.use(
      http.get("http://localhost:3000/api/v1/projects/demo/chat/director-auto", () =>
        HttpResponse.json({
          ok: true,
          data: {
            status: "awaiting_confirmation",
            episode: 1,
            run_id: "run-await",
            last_error: "[stall] 某任务卡住",
          },
        }),
      ),
      http.post(
        "http://localhost:3000/api/v1/projects/demo/chat/director-auto/resume",
        () => {
          calls.push("resume");
          return HttpResponse.json({ ok: true, data: { status: "running" } });
        },
      ),
      http.post(
        "http://localhost:3000/api/v1/projects/demo/chat/director-auto/continue",
        () => {
          calls.push("continue");
          return HttpResponse.json({ ok: true, data: { status: "running" } });
        },
      ),
    );

    const { result } = renderHook(() => useStudioRun("demo", { auto: false }), { wrapper });
    await waitFor(() => expect(result.current.needsHuman).toBeTruthy());
    await act(async () => {
      result.current.resumeManually();
      await Promise.resolve();
    });

    await waitFor(() => expect(calls).toContain("resume"));
    // 绝不能误打 /continue（它只接受 paused，会 409）。
    expect(calls).not.toContain("continue");
  });

  it("paused 走 /continue（resume_paused），人工续跑不带 auto", async () => {
    const calls: string[] = [];
    server.use(
      http.get("http://localhost:3000/api/v1/projects/demo/chat/director-auto", () =>
        HttpResponse.json({
          ok: true,
          data: {
            status: "paused",
            episode: 1,
            run_id: "run-p",
            pause_kind: "retryable",
            last_error: "生成失败",
          },
        }),
      ),
      http.post(
        "http://localhost:3000/api/v1/projects/demo/chat/director-auto/continue",
        async ({ request }) => {
          calls.push(JSON.stringify(await request.json()));
          return HttpResponse.json({ ok: true, data: { status: "running" } });
        },
      ),
      http.post(
        "http://localhost:3000/api/v1/projects/demo/chat/director-auto/resume",
        () => {
          calls.push("resume");
          return HttpResponse.json({ ok: true, data: { status: "running" } });
        },
      ),
    );

    const { result } = renderHook(() => useStudioRun("demo", { auto: false }), { wrapper });
    await waitFor(() => expect(result.current.run?.status).toBe("paused"));
    act(() => result.current.resumeManually());

    await waitFor(() => expect(calls.length).toBeGreaterThan(0));
    // 人工点击不带 auto，服务端据此清零自动恢复预算。
    expect(JSON.parse(calls[0]!)).toEqual({});
    expect(calls).not.toContain("resume");
  });
});
