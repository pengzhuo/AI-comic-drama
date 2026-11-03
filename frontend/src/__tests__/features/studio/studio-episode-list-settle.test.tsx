// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import ky from "ky";
import type { ReactNode } from "react";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", () => ({
  api: ky.create({ baseUrl: "http://localhost:3000/" }),
  uploadApi: ky.create({ baseUrl: "http://localhost:3000/" }),
}));

import { StudioEpisodeList } from "@/features/studio/studio-episode-list";

const server = setupServer();

beforeAll(() => server.listen());
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

const FINAL = "http://localhost:3000/api/v1/projects/demo/episodes/1/final";
const SETTLE = "http://localhost:3000/api/v1/projects/demo/episodes/1/points/settle";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function mockCostReport() {
  const bucket = {
    requests: 0,
    unattributed_requests: 0,
    quota: 0,
    usd: 0,
    cny: 0,
    duration_seconds: 0,
    duration_hours: 0,
    started_at: null,
    finished_at: null,
  };
  server.use(
    http.get("http://localhost:3000/api/v1/projects/demo/cost-report", () =>
      HttpResponse.json({
        ok: true,
        data: {
          generated_at: "2026-11-03T00:00:00",
          usd_cny_rate: 7.3,
          quota_per_usd: 500000,
          totals: { ...bucket },
          unattributed: { ...bucket },
          by_kind: [],
          by_model: [],
          by_episode: [],
        },
      }),
    ),
  );
}

function blockedPayload() {
  return {
    exists: true,
    filename: "ep001_final.mp4",
    blocked: true,
    settle_status: "owed",
    owed_points: 900,
    message: "伴点不足，请先充值",
  };
}

function renderList() {
  return render(
    <StudioEpisodeList
      project="demo"
      episodes={[{ number: 1 }]}
      currentEpisode={null}
      runActive={false}
    />,
    { wrapper },
  );
}

describe("StudioEpisodeList 成片欠费", () => {
  it("欠费时不给播放器/下载，而是补缴卡片", async () => {
    mockCostReport();
    server.use(http.get(FINAL, () => HttpResponse.json({ ok: true, data: blockedPayload() })));
    renderList();

    expect(await screen.findByText("待补缴 900 伴点 · 补缴后解锁成片")).toBeTruthy();
    expect(screen.getByText("伴点不足，请先充值")).toBeTruthy();
    expect(screen.getByRole("button", { name: /我已补缴，解锁成片/ })).toBeTruthy();
    expect(screen.getByText("待补缴")).toBeTruthy();
    // 预览与下载一起挡：播放器与下载链接都不渲染。
    expect(document.querySelector("video")).toBeNull();
    expect(screen.queryByText(/下载成片/)).toBeNull();
  });

  it("补缴成功后刷新出播放器", async () => {
    mockCostReport();
    let blocked = true;
    server.use(
      http.get(FINAL, () =>
        HttpResponse.json({
          ok: true,
          data: blocked
            ? blockedPayload()
            : {
                exists: true,
                filename: "ep001_final.mp4",
                blocked: false,
                video_url: "/static/projects/demo/videos/episodes/ep001_final.mp4",
              },
        }),
      ),
      http.post(SETTLE, () => {
        blocked = false;
        return HttpResponse.json({
          ok: true,
          data: { status: "settled", blocked: false, owed_points: 0, message: "" },
        });
      }),
    );
    renderList();

    fireEvent.click(await screen.findByRole("button", { name: /我已补缴，解锁成片/ }));

    expect(await screen.findByText("已完成")).toBeTruthy();
    await waitFor(() => expect(document.querySelector("video")).not.toBeNull());
    expect(screen.queryByText(/待补缴 900 伴点/)).toBeNull();
  });

  it("补缴仍不足时把接口原文显示出来", async () => {
    mockCostReport();
    server.use(
      http.get(FINAL, () => HttpResponse.json({ ok: true, data: blockedPayload() })),
      http.post(SETTLE, () =>
        HttpResponse.json({
          ok: true,
          data: {
            status: "owed",
            blocked: true,
            owed_points: 900,
            message: "伴点不足，请先充值",
          },
        }),
      ),
    );
    renderList();

    fireEvent.click(await screen.findByRole("button", { name: /我已补缴，解锁成片/ }));

    await waitFor(() =>
      expect(screen.getAllByText("伴点不足，请先充值").length).toBeGreaterThan(0),
    );
    expect(document.querySelector("video")).toBeNull();
  });
});
