// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";
import { createElement, type ComponentType } from "react";

const accessMocks = vi.hoisted(() => ({
  fetchAccessSession: vi.fn(),
  activateClientSession: vi.fn(),
  logoutAccessSession: vi.fn(),
  maskPhone: (phone: string) => phone,
  setAccessNotice: vi.fn(),
  takeAccessNotice: vi.fn(() => null),
  useAccessSession: vi.fn(() => ({ enabled: true, kind: "none", phone: "" })),
}));

vi.mock("@/lib/access-session", () => accessMocks);

const appModeMocks = vi.hoisted(() => ({
  hasInternalRoutes: vi.fn(() => false),
  internalEntryPath: vi.fn(() => ""),
  clientEntryPath: vi.fn(() => "/canvas"),
}));

vi.mock("@/lib/app-mode", () => appModeMocks);

vi.mock("@/lib/auth-mode", () => ({
  ensureAuthenticatedForAppRoute: vi.fn(async () => true),
}));

vi.mock("@/lib/brand-attribution", () => ({ brandAttribution: () => null }));

vi.mock("@/features/access/studio-link-gate", () => ({ StudioLinkGate: () => null }));

vi.mock("@/stores/auth-store", () => ({
  useAuthStore: (selector: (state: { username: string | null }) => unknown) =>
    selector({ username: null }),
}));

vi.mock("@tanstack/react-router", () => ({
  createFileRoute: () => (options: unknown) => ({ options }),
  Link: () => null,
  Outlet: () => null,
  redirect: (options: unknown) => ({ options }),
}));

import { Route } from "@/routes/canvas";

// 全为测试专用假值：真实客户的手机号与专属校验码不入库。
const CODE = "00000000000000000000000000000000";
const PHONE = "13000000000";

interface LocationLike {
  pathname: string;
  hash: string;
  searchStr: string;
  /** 路由默认 `parseSearch` 的产物（见 studio-link-exchange 的说明）。 */
  search: Record<string, unknown>;
}

function locationFor(searchStr: string, search: Record<string, unknown>): LocationLike {
  return { pathname: "/canvas", hash: "", searchStr, search };
}

async function runBeforeLoad(location: LocationLike): Promise<unknown> {
  try {
    await Route.options.beforeLoad?.({ location } as never);
    return null;
  } catch (error) {
    return error;
  }
}

function redirectOptions(error: unknown): Record<string, unknown> | null {
  return (error as { options?: Record<string, unknown> } | null)?.options ?? null;
}

describe("/canvas 客户链接兑换", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    appModeMocks.hasInternalRoutes.mockReturnValue(false);
    appModeMocks.internalEntryPath.mockReturnValue("");
    accessMocks.fetchAccessSession.mockResolvedValue({ enabled: true, kind: "none", phone: "" });
    accessMocks.activateClientSession.mockResolvedValue({ ok: true });
    accessMocks.useAccessSession.mockReturnValue({ enabled: true, kind: "none", phone: "" });
  });

  it("手机号被解析成 number 时也必须发起兑换（与 /studio 同一条链路）", async () => {
    const error = await runBeforeLoad(
      locationFor(`?phone=${PHONE}&code=${CODE}`, { phone: Number(PHONE), code: CODE }),
    );

    expect(accessMocks.activateClientSession).toHaveBeenCalledWith(PHONE, CODE);
    expect(accessMocks.setAccessNotice).toHaveBeenCalledWith(null);
    // 兑换成功后立刻从地址栏抹掉 query（校验码等价于密码）。
    expect(redirectOptions(error)).toMatchObject({ href: "/canvas", replace: true });
  });

  it("坏链接拦回门禁页并清掉旧会话", async () => {
    accessMocks.activateClientSession.mockResolvedValue({
      ok: false,
      error: "访问链接无效，请使用收到的专属链接",
    });

    const error = await runBeforeLoad(
      locationFor(`?phone=${PHONE}&code=${CODE}`, { phone: Number(PHONE), code: CODE }),
    );

    expect(accessMocks.setAccessNotice).toHaveBeenCalledWith("访问链接无效，请使用收到的专属链接");
    expect(accessMocks.logoutAccessSession).toHaveBeenCalledTimes(1);
    expect(redirectOptions(error)).toMatchObject({ href: "/canvas", replace: true });
  });

  it("普通访问 /canvas（无参数）不发兑换", async () => {
    const error = await runBeforeLoad(locationFor("", {}));

    expect(accessMocks.activateClientSession).not.toHaveBeenCalled();
    expect(error).toBeNull();
  });

  it("未启用访问验证的实例不走链接兑换", async () => {
    accessMocks.fetchAccessSession.mockResolvedValue({ enabled: false, kind: "open", phone: "" });

    const error = await runBeforeLoad(
      locationFor(`?phone=${PHONE}&code=${CODE}`, { phone: Number(PHONE), code: CODE }),
    );

    expect(accessMocks.activateClientSession).not.toHaveBeenCalled();
    expect(error).toBeNull();
  });

  it("纯内部端（有内部树、内部入口就是 /）回内部首页", async () => {
    appModeMocks.hasInternalRoutes.mockReturnValue(true);
    appModeMocks.internalEntryPath.mockReturnValue("");

    const error = await runBeforeLoad(locationFor("", {}));

    expect(redirectOptions(error)).toMatchObject({ to: "/", replace: true });
    expect(accessMocks.fetchAccessSession).not.toHaveBeenCalled();
  });

  it("双入口实例（内部端挂在 /console）放行 /canvas", async () => {
    appModeMocks.hasInternalRoutes.mockReturnValue(true);
    appModeMocks.internalEntryPath.mockReturnValue("/console");

    const error = await runBeforeLoad(locationFor("", {}));

    expect(error).toBeNull();
    expect(accessMocks.fetchAccessSession).toHaveBeenCalledTimes(1);
  });
});

describe("/canvas 客户外壳", () => {
  it("主区用 fill 布局：不滚动，画布宿主拿它当定位上下文", () => {
    accessMocks.useAccessSession.mockReturnValue({
      enabled: true,
      kind: "client",
      phone: "13000000000",
    });

    const Layout = Route.options.component as ComponentType;
    render(createElement(Layout));

    const main = document.getElementById("main-content");
    expect(main).not.toBeNull();
    expect(main?.className).toContain("overflow-hidden");
    expect(main?.className).toContain("relative");
    expect(main?.className).not.toContain("overflow-y-auto");
  });
});
