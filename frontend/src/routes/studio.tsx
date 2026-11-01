// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { Link, Outlet, createFileRoute, redirect } from "@tanstack/react-router";
import { useState } from "react";

import { internalEntryPath, isClientMode } from "@/lib/app-mode";
import { ensureAuthenticatedForAppRoute } from "@/lib/auth-mode";
import { brandAttribution } from "@/lib/brand-attribution";
import {
  activateClientSession,
  fetchAccessSession,
  logoutAccessSession,
  maskPhone,
  setAccessNotice,
  takeAccessNotice,
  useAccessSession,
} from "@/lib/access-session";
import { StudioLinkGate } from "@/features/access/studio-link-gate";
import { useAuthStore } from "@/stores/auth-store";

/**
 * 客户端工作室外壳（`/studio`）。
 *
 * 这个路由故意挂在 `_app` **之外**：客户端产物在构建期就把 `_app` 整棵树排除掉了
 * （见 `vite.config.ts` 的 client 分支），所以内部页不是「藏起来」而是不在产物里。
 * 代价是要在这里自己做鉴权与最小外框。
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

    const search = location.search as Record<string, unknown>;
    const phone = typeof search.phone === "string" ? search.phone.trim() : "";
    const code = typeof search.code === "string" ? search.code.trim() : "";
    if (phone && code) {
      const result = await activateClientSession(phone, code);
      setAccessNotice(result.ok ? null : (result.error ?? "访问链接无效"));
      // 校验码等价于密码：换到 cookie 之后立刻把 query 从地址栏与历史里抹掉
      // （replace，不留下一条能按「后退」拿回校验码的记录）。
      throw redirect({ href: `${location.pathname}${location.hash}`, replace: true });
    }

    if (access.kind === "client" || access.kind === "internal") {
      // 内部身份也能浏览客户端工作台（同一份产物、同一套接口）。
      await ensureAuthenticatedForAppRoute();
    }
  },
  component: StudioLayout,
});

function StudioLayout() {
  const access = useAccessSession() ?? { enabled: false, kind: "open" as const, phone: "" };
  const username = useAuthStore((state) => state.username);
  const [notice] = useState(() => takeAccessNotice());
  // 实例级署名。内部端挂在底部状态栏上，但状态栏属于 `_app`（客户端产物里整棵被
  // 排除），所以客户端外壳自己渲染一次；未设置（上游构建）时不渲染。
  const attribution = brandAttribution();

  if (access.enabled && access.kind === "none") {
    return (
      <StudioLinkGate
        notice={notice}
        attribution={attribution}
        onLogout={() => {
          // 整页重载：一次性清掉查询缓存与内存态，避免上一位用户的数据残留。
          void logoutAccessSession().then(() => window.location.reload());
        }}
      />
    );
  }

  // 客户端身份只显示脱敏手机号；内部身份（看全部）回落到用户名。
  const identity = access.phone ? maskPhone(access.phone) : username;

  return (
    <div className="flex min-h-dvh flex-col bg-background text-foreground">
      <header className="border-b border-border/60 bg-card/60 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-5xl items-center justify-between px-4">
          <Link to="/studio" className="text-sm font-semibold tracking-tight">
            拿捏漫剧工作台
          </Link>
          <div className="flex items-center gap-3 text-xs text-muted-foreground">
            {identity ? <span className="truncate">{identity}</span> : null}
            {identity ? (
              <button
                type="button"
                className="transition-colors hover:text-foreground"
                onClick={() => {
                  void logoutAccessSession().then(() => window.location.reload());
                }}
              >
                退出
              </button>
            ) : null}
          </div>
        </div>
      </header>
      <main id="main-content" className="mx-auto w-full max-w-5xl flex-1 px-4 py-6">
        <Outlet />
      </main>
      {attribution ? (
        // 纯文本、无交互：保持 `text-muted-foreground` 不降透明度，12px 也要过 AA。
        <footer className="border-t border-border/60">
          <div className="mx-auto max-w-5xl px-4 py-3 text-right text-xs text-muted-foreground">
            <span data-testid="studio-brand-attribution">{attribution}</span>
          </div>
        </footer>
      ) : null}
    </div>
  );
}
