// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { Outlet, createFileRoute, redirect, useRouterState } from "@tanstack/react-router";

import { ClientAccessShell } from "@/features/access/client-access-shell";
import { exchangeClientLinkFromLocation } from "@/features/access/studio-link-entry";
import { hasInternalRoutes, internalEntryPath } from "@/lib/app-mode";
import { fetchAccessSession } from "@/lib/access-session";
import { ensureAuthenticatedForAppRoute } from "@/lib/auth-mode";

/**
 * 客户画布入口（`/canvas`、`/canvas/<项目>`）。
 *
 * 与 `/studio` 是同一套客户入口语义：门禁沿用工作室的专属链接
 * （`?phone=&code=` → `st_studio` cookie，见 `features/access/studio-link-entry.ts`），
 * 外壳复用 `ClientAccessShell`。区别只在内容：这里是捏画，不是一键成片。
 *
 * 这个路由同样挂在 `_app` **之外**：客户产物（`client` / `canvas`）构建期就把
 * `_app` 整棵树排除掉了，所以内部页不在产物里。守卫与 `__root.tsx` 一致：纯内部端
 * （有内部树且内部入口就是 `/`）没有客户入口，回内部首页；双入口实例与两种客户
 * 产物都放行。
 *
 * 两个子路由要的外壳内容模式不同，由这里按路径分派（全站 `html/body/#root` 锁了
 * `overflow: hidden`，模式给错内容一超一屏就被裁掉、滚不动）：
 *
 * - `/canvas`（首页：上传卡 + 「我的剧」列表）是普通文档流 → `scroll`；
 * - `/canvas/<项目>` 是整屏画布（宿主 `absolute inset-0`，滚动交给画布内部）→ `fill`。
 */
export const Route = createFileRoute("/canvas")({
  beforeLoad: async ({ location }) => {
    if (hasInternalRoutes() && internalEntryPath() === "") {
      throw redirect({ to: "/", replace: true });
    }

    const access = await fetchAccessSession();
    if (!access.enabled) {
      // 未启用访问验证的实例（上游 CE / 其他部署）：维持原有登录态判断。
      if (!(await ensureAuthenticatedForAppRoute())) {
        throw redirect({ to: "/login", replace: true });
      }
      return;
    }

    // 带链接参数就当场兑换（校验失败/半截链接一律拦回门禁页），并立刻从地址栏与
    // 历史里抹掉 query。客户链接由手机号生成，不绑定项目，所以 `/canvas` 与
    // `/canvas/<项目>` 都用同一条链路直达。
    const redirectHref = await exchangeClientLinkFromLocation(location);
    if (redirectHref) {
      throw redirect({ href: redirectHref, replace: true });
    }

    if (access.kind === "client" || access.kind === "internal") {
      // 内部身份也能看客户画布（同一份产物、同一套接口）。
      await ensureAuthenticatedForAppRoute();
    }
  },
  component: CanvasLayout,
});

function CanvasLayout() {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  // `/canvas/` 与 `/canvas` 都算首页；`/canvas/<项目>` 走整屏画布。
  const isHome = pathname.replace(/\/+$/, "") === "/canvas";

  return (
    <ClientAccessShell content={isHome ? "scroll" : "fill"}>
      <Outlet />
    </ClientAccessShell>
  );
}
