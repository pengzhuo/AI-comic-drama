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

vi.mock("@/lib/app-mode", () => ({
  isClientMode: () => false,
  internalEntryPath: () => "/console",
}));

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

import { Route } from "@/routes/studio";

// 全为测试专用假值：真实客户的手机号与专属校验码不入库。
const CODE = "00000000000000000000000000000000";
const PHONE = "13000000000";

interface LocationLike {
  pathname: string;
  hash: string;
  searchStr: string;
  /** 路由默认 `parseSearch` 的产物（见下）。 */
  search: Record<string, unknown>;
}

/**
 * TanStack Router 的默认 `parseSearch` 会对每个 query 值做 `JSON.parse`，
 * 所以 `?phone=13000000000` 在 `location.search` 里是 **number**（已用真实的
 * `defaultParseSearch` 验证）。这个夹具刻意保留该形态：如果实现退回读
 * `location.search.phone` 并要求 string，下面的用例会失败。
 */
function locationFor(searchStr: string, search: Record<string, unknown>): LocationLike {
  return { pathname: "/studio", hash: "", searchStr, search };
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

describe("/studio 客户链接兑换", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    accessMocks.fetchAccessSession.mockResolvedValue({ enabled: true, kind: "none", phone: "" });
    accessMocks.activateClientSession.mockResolvedValue({ ok: true });
    accessMocks.useAccessSession.mockReturnValue({ enabled: true, kind: "none", phone: "" });
  });

  it("手机号被解析成 number 时也必须发起兑换（回归）", async () => {
    const error = await runBeforeLoad(
      locationFor(`?phone=${PHONE}&code=${CODE}`, { phone: Number(PHONE), code: CODE }),
    );

    expect(accessMocks.activateClientSession).toHaveBeenCalledWith(PHONE, CODE);
    expect(accessMocks.setAccessNotice).toHaveBeenCalledWith(null);
    // 兑换成功后立刻从地址栏抹掉 query（校验码等价于密码）。
    expect(redirectOptions(error)).toMatchObject({ href: "/studio", replace: true });
  });

  it("字符串形态的参数同样可以兑换", async () => {
    await runBeforeLoad(locationFor(`?phone=${PHONE}&code=${CODE}`, { phone: PHONE, code: CODE }));

    expect(accessMocks.activateClientSession).toHaveBeenCalledWith(PHONE, CODE);
  });

  it("服务端拒绝时把错误文案交给门禁页，并照旧抹掉校验码", async () => {
    accessMocks.activateClientSession.mockResolvedValue({
      ok: false,
      error: "访问链接无效，请使用收到的专属链接",
    });

    const error = await runBeforeLoad(
      locationFor(`?phone=${PHONE}&code=${CODE}`, { phone: Number(PHONE), code: CODE }),
    );

    expect(accessMocks.setAccessNotice).toHaveBeenCalledWith("访问链接无效，请使用收到的专属链接");
    expect(redirectOptions(error)).toMatchObject({ href: "/studio", replace: true });
  });

  it("链接只有一半参数时给出「不完整」提示，而不是静默回到门禁页", async () => {
    const error = await runBeforeLoad(locationFor(`?phone=${PHONE}`, { phone: Number(PHONE) }));

    expect(accessMocks.activateClientSession).not.toHaveBeenCalled();
    expect(accessMocks.setAccessNotice).toHaveBeenCalledWith(expect.stringContaining("不完整"));
    expect(error).toBeNull();
  });

  it("普通访问 /studio（无参数）不发兑换、也不打扰用户", async () => {
    const error = await runBeforeLoad(locationFor("", {}));

    expect(accessMocks.activateClientSession).not.toHaveBeenCalled();
    expect(accessMocks.setAccessNotice).not.toHaveBeenCalled();
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
});

describe("/studio 客户外壳", () => {
  it("主内容区可被 RouteFocusManager 聚焦（id + tabIndex=-1）", () => {
    accessMocks.useAccessSession.mockReturnValue({
      enabled: true,
      kind: "client",
      phone: "13000000000",
    });

    const Layout = Route.options.component as ComponentType;
    render(createElement(Layout));

    // __root.tsx 的 RouteFocusManager 靠 document.getElementById("main-content")
    // 搬焦点；不可聚焦的元素上 focus() 是空操作，所以这里必须有 tabIndex={-1}。
    const main = document.getElementById("main-content");
    expect(main).not.toBeNull();
    expect(main).toHaveAttribute("tabindex", "-1");
  });
});
