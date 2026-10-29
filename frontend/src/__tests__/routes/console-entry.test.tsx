// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { beforeEach, describe, expect, it, vi } from "vitest";

// `/console` 是双入口实例的内部入口：与 `/` 同一个内部首页，只在开关打开时存在。
// 这里只验证「开关关着时它不能变成第二个首页」，路由挂载本身由生成的 routeTree 覆盖。
const state = vi.hoisted(() => ({ entryPath: "" }));

vi.mock("@tanstack/react-router", () => ({
  createFileRoute: () => (options: unknown) => options,
  redirect: (options: unknown) => options,
}));

vi.mock("@/lib/app-mode", () => ({
  internalEntryPath: () => state.entryPath,
  internalHomePath: () => (state.entryPath === "" ? "/" : "/console"),
}));

vi.mock("@/routes/_app/index", () => ({
  ProjectDashboard: function ProjectDashboard() {
    return null;
  },
}));

import { ProjectDashboard } from "@/routes/_app/index";
import { Route } from "@/routes/_app/console";

const route = Route as unknown as {
  beforeLoad: () => void;
  component: unknown;
};

describe("internal console entry", () => {
  beforeEach(() => {
    state.entryPath = "";
  });

  it("sends a stray /console back to the internal home when dual entry is off", () => {
    expect(() => route.beforeLoad()).toThrow(
      expect.objectContaining({ to: "/", replace: true }),
    );
  });

  it("serves the internal dashboard once the instance points its internal entry here", () => {
    state.entryPath = "/console";
    expect(route.beforeLoad()).toBeUndefined();
    expect(route.component).toBe(ProjectDashboard);
  });
});
