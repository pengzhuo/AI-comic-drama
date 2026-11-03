// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { render, screen, waitFor } from "@testing-library/react";
import {
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
  Outlet,
  RouterProvider,
} from "@tanstack/react-router";
import { describe, expect, it, vi } from "vitest";

import { FreezoneCanvasHost } from "@/features/freezone/FreezoneCanvasHost";
import { useFreezoneCanvasRoute } from "@/features/freezone/canvas-route";

vi.mock("@/features/freezone/FreezoneCanvasRoot", () => ({
  default: ({ active }: { active: boolean }) => (
    <div data-testid="canvas-root" data-active={String(active)} />
  ),
}));

/**
 * 内部壳的接线：URL → `{ project, active }` 由 `useFreezoneCanvasRoute()` 负责，
 * 宿主只吃 props。这里照 _app.tsx 的用法还原一遍。
 */
function WiredFreezoneCanvasHost() {
  const route = useFreezoneCanvasRoute();
  return <FreezoneCanvasHost project={route.project} active={route.active} />;
}

function renderAt(pathname: string) {
  const rootRoute = createRootRoute({
    component: () => (
      <>
        <Outlet />
        <WiredFreezoneCanvasHost />
      </>
    ),
  });
  const freezoneRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/projects/$project/freezone",
    component: () => <div>freezone</div>,
  });
  const ingestRoute = createRoute({
    getParentRoute: () => rootRoute,
    path: "/projects/$project/ingest",
    component: () => <div>ingest</div>,
  });
  const router = createRouter({
    routeTree: rootRoute.addChildren([freezoneRoute, ingestRoute]),
    history: createMemoryHistory({ initialEntries: [pathname] }),
  });
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const result = render(<RouterProvider router={router as any} />);
  return { router, ...result };
}

const hostContainer = () =>
  screen.getByTestId("canvas-root").closest("div[aria-hidden]") as HTMLElement;

describe("FreezoneCanvasHost", () => {
  it("keeps the canvas visible and active on the freezone route", async () => {
    renderAt("/projects/p1/freezone");
    await waitFor(() => expect(screen.getByTestId("canvas-root")).toBeTruthy());
    expect(screen.getByTestId("canvas-root").dataset.active).toBe("true");
    expect(hostContainer().className).not.toContain("invisible");
  });

  it("hides and deactivates the canvas after navigating to a 捏集 sub-page", async () => {
    const { router } = renderAt("/projects/p1/freezone");
    await waitFor(() => expect(screen.getByTestId("canvas-root")).toBeTruthy());

    await router.navigate({ to: "/projects/$project/ingest", params: { project: "p1" } });

    await waitFor(() =>
      expect(screen.getByTestId("canvas-root").dataset.active).toBe("false"),
    );
    const container = hostContainer();
    expect(container.className).toContain("invisible");
    expect(container.className).toContain("pointer-events-none");
    expect(container.getAttribute("aria-hidden")).toBe("true");
  });

  it("does not mount the canvas for a project that never opened 捏画", async () => {
    renderAt("/projects/p1/ingest");
    await waitFor(() => expect(screen.getByText("ingest")).toBeTruthy());
    expect(screen.queryByTestId("canvas-root")).toBeNull();
  });
});
