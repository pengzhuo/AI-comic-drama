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

// TanStack Router 的 <Link> 需要路由上下文；这里降级成一个普通 <a>。
vi.mock("@tanstack/react-router", () => ({
  Link: ({
    to,
    params,
    children,
    ...rest
  }: {
    to: string;
    params?: { project?: string };
    children?: ReactNode;
  }) => (
    <a href={to.replace("$project", params?.project ?? "")} {...rest}>
      {children}
    </a>
  ),
}));

const confirm = vi.hoisted(() => vi.fn(async () => true));
vi.mock("@/components/confirm-dialog-host", () => ({ confirmDialog: confirm }));

import { StudioProjectList } from "@/features/studio/studio-project-list";

const server = setupServer();

beforeAll(() => server.listen());
afterEach(() => {
  server.resetHandlers();
  confirm.mockClear();
});
afterAll(() => server.close());

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function summaries(items: unknown[]) {
  server.use(
    http.get("http://localhost:3000/api/v1/projects/summaries", () =>
      HttpResponse.json({ ok: true, data: items }),
    ),
  );
}

describe("StudioProjectList", () => {
  it("隐藏已删除的项目，并可通过确认弹窗软删除", async () => {
    let deletedId: string | null = null;
    summaries([
      { id: "p-active", name: "在制作品", status: "active", episode_count: 3 },
      { id: "p-gone", name: "已删除作品", status: "deleted" },
    ]);
    server.use(
      http.post("http://localhost:3000/api/v1/projects/p-active/delete", () => {
        deletedId = "p-active";
        return HttpResponse.json({
          ok: true,
          data: { name: "在制作品", status: "deleted" },
        });
      }),
    );

    render(<StudioProjectList to="/studio/$project" />, { wrapper });

    expect(await screen.findByText("在制作品")).toBeTruthy();
    // 软删除的项目不该再出现在客户列表里。
    expect(screen.queryByText("已删除作品")).toBeNull();
    expect(screen.getByRole("link").getAttribute("href")).toBe("/studio/p-active");

    fireEvent.click(screen.getByRole("button", { name: "删除《在制作品》" }));

    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(deletedId).toBe("p-active"));
  });

  it("弹窗取消时不发起删除", async () => {
    confirm.mockResolvedValueOnce(false);
    let deleteCalls = 0;
    summaries([{ id: "p-active", name: "在制作品", status: "active" }]);
    server.use(
      http.post("http://localhost:3000/api/v1/projects/p-active/delete", () => {
        deleteCalls += 1;
        return HttpResponse.json({ ok: true, data: {} });
      }),
    );

    render(<StudioProjectList to="/canvas/$project" />, { wrapper });

    expect(await screen.findByText("在制作品")).toBeTruthy();
    expect(screen.getByRole("link").getAttribute("href")).toBe("/canvas/p-active");

    fireEvent.click(screen.getByRole("button", { name: "删除《在制作品》" }));

    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
    expect(deleteCalls).toBe(0);
  });
});
