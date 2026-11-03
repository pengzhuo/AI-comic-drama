// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { Outlet, createFileRoute, redirect } from "@tanstack/react-router";

import { ClientAccessShell } from "@/features/access/client-access-shell";
import { exchangeClientLinkFromLocation } from "@/features/access/studio-link-entry";
import { internalEntryPath, isClientMode } from "@/lib/app-mode";
import { fetchAccessSession } from "@/lib/access-session";
import { ensureAuthenticatedForAppRoute } from "@/lib/auth-mode";

/**
 * 客户端工作室（`/studio`）。
 *
 * 外壳与链接兑换都在 `features/access/` 里（`ClientAccessShell` /
 * `exchangeClientLinkFromLocation`），本路由只负责：模式守卫 + 触发兑换 +
 * 交回 href 跳转。同一套客户入口语义将来也给 `/canvas` 复用。
 *
 * 这个路由故意挂在 `_app` **之外**：客户产物在构建期就把 `_app` 整棵树排除掉了
 * （见 `vite.config.ts` 的 client 分支），所以内部页不是「藏起来」而是不在产物里。
 *
 * 访问验证启用时（后端配了 client secret），这里是客户入口的唯一收口：
 * 链接里的 `?phone=&code=` 在这里换成 `st_studio` cookie，并立刻从地址栏抹掉。
 */
export const Route = createFileRoute("/studio")({
  beforeLoad: async ({ location }) => {
    // 内部端也有这个路由（同一份源码），默认回内部首页，避免两套入口打架。
    // 双入口实例（根地址让给工作室、内部端挂 VITE_INTERNAL_ENTRY_PATH）例外，
    // 那时这个路由就是客户入口本身。
    if (!isClientMode() && internalEntryPath() === "") {
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
    // 历史里抹掉 query（校验码等价于密码）。没有参数时返回 null，往下走鉴权。
    const redirectHref = await exchangeClientLinkFromLocation(location);
    if (redirectHref) {
      throw redirect({ href: redirectHref, replace: true });
    }

    if (access.kind === "client" || access.kind === "internal") {
      // 内部身份也能浏览客户端工作台（同一份产物、同一套接口）。
      await ensureAuthenticatedForAppRoute();
    }
  },
  component: StudioLayout,
});

function StudioLayout() {
  return (
    <ClientAccessShell>
      <div className="mx-auto w-full max-w-5xl px-4 py-6">
        <Outlet />
      </div>
    </ClientAccessShell>
  );
}
