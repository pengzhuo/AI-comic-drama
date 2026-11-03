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

vi.mock("sonner", () => ({
  toast: { error: vi.fn(), success: vi.fn(), message: vi.fn() },
}));

import { toast } from "sonner";

import { DirectorCostEstimateCard } from "@/features/superchat/director-cost-estimate-card";

const server = setupServer();

beforeAll(() => server.listen());
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function estimatePayload(overrides: Record<string, unknown> = {}) {
  return {
    ok: true,
    data: {
      stage: "refined",
      episode: 1,
      episode_count: 1,
      currency: "CNY",
      view: "internal",
      generated_at: "2026-11-02T10:00:00",
      contingency_pct: 10,
      assumptions: {},
      cost: { cny: 100, contingency_cny: 9.09, total_cny: 109.09 },
      price: { cny: null, markup: null, markup_source: "none" },
      by_kind: [
        { kind: "image", kind_label: "图片", items: 12, cny: 6, unpriced: 0 },
        { kind: "video", kind_label: "视频", items: 6, cny: 94, unpriced: 0 },
      ],
      items: [],
      unpriced: [],
      unpriced_count: 0,
      priced_count: 2,
      actual: { cny: null, delta_pct: null },
      ...overrides,
    },
  };
}

function mockEstimate(payload: Record<string, unknown>) {
  server.use(
    http.get(
      "http://localhost:3000/api/v1/projects/demo/production-estimate",
      () => HttpResponse.json(payload),
    ),
  );
}

describe("DirectorCostEstimateCard", () => {
  it("shows the internal cost total and confirms the cost gate", async () => {
    const confirmed: unknown[] = [];
    mockEstimate(estimatePayload());
    server.use(
      http.post(
        "http://localhost:3000/api/v1/projects/demo/chat/director-auto/gates/cost_estimate/confirm",
        async ({ request }) => {
          confirmed.push(await request.json());
          return HttpResponse.json({ ok: true, data: { status: "running" } });
        },
      ),
    );
    const onResolved = vi.fn();

    render(<DirectorCostEstimateCard project="demo" episode={1} onResolved={onResolved} />, {
      wrapper,
    });

    expect(await screen.findByText("成本合计（含预留）")).toBeTruthy();
    expect(screen.getByText("¥109.09")).toBeTruthy();
    expect(screen.getByText("图片")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /继续制作/ }));

    await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(1));
    expect(confirmed).toEqual([{ episode: 1 }]);
  });

  it("shows the client the price only, never the cost", async () => {
    mockEstimate(
      estimatePayload({
        view: "client",
        cost: null,
        price: { cny: 218.18, markup: 2, markup_source: "client" },
        by_kind: [
          { kind: "image", kind_label: "图片", items: 12, cny: null, unpriced: 0 },
        ],
      }),
    );

    render(
      <DirectorCostEstimateCard project="demo" episode={1} onResolved={() => undefined} />,
      { wrapper },
    );

    expect(await screen.findByText("报价合计")).toBeTruthy();
    expect(screen.getByText("¥218.18")).toBeTruthy();
    expect(screen.queryByText("成本合计（含预留）")).toBeNull();
    expect(screen.queryByText("¥100.00")).toBeNull();
  });

  it("honestly reports unpriced items instead of counting them as 0", async () => {
    mockEstimate(
      estimatePayload({
        unpriced_count: 3,
        by_kind: [{ kind: "audio", kind_label: "音频", items: 40, cny: null, unpriced: 3 }],
      }),
    );

    render(
      <DirectorCostEstimateCard project="demo" episode={1} onResolved={() => undefined} />,
      { wrapper },
    );

    expect(await screen.findByText(/有 3 项还没配单价/)).toBeTruthy();
  });

  it("tells the client how many points will be deducted", async () => {
    mockEstimate(
      estimatePayload({
        view: "client",
        cost: null,
        price: { cny: 218.18, markup: 2, markup_source: "client", points: 21818 },
        by_kind: [{ kind: "image", kind_label: "图片", items: 12, cny: null, unpriced: 0 }],
      }),
    );

    render(
      <DirectorCostEstimateCard project="demo" episode={1} onResolved={() => undefined} />,
      { wrapper },
    );

    expect(await screen.findByText(/预扣/)).toBeTruthy();
    expect(screen.getByText("21,818")).toBeTruthy();
  });

  it("surfaces the backend message when the points prepay is refused", async () => {
    mockEstimate(estimatePayload());
    server.use(
      http.post(
        "http://localhost:3000/api/v1/projects/demo/chat/director-auto/gates/cost_estimate/confirm",
        () => HttpResponse.json({ detail: "伴点不足，请先充值" }, { status: 402 }),
      ),
    );
    const onResolved = vi.fn();

    render(<DirectorCostEstimateCard project="demo" episode={1} onResolved={onResolved} />, {
      wrapper,
    });

    fireEvent.click(await screen.findByRole("button", { name: /继续制作/ }));

    await waitFor(() =>
      expect(vi.mocked(toast.error)).toHaveBeenCalledWith("伴点不足，请先充值"),
    );
    // 闸门没答：制作保持暂停，卡片不收起。
    expect(onResolved).not.toHaveBeenCalled();
  });
});
