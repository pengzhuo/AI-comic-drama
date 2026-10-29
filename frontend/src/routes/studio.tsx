// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { Link, Outlet, createFileRoute, redirect } from "@tanstack/react-router";

import { internalEntryPath, isClientMode } from "@/lib/app-mode";
import { ensureAuthenticatedForAppRoute } from "@/lib/auth-mode";
import { brandAttribution } from "@/lib/brand-attribution";
import { useAuthStore } from "@/stores/auth-store";

/**
 * 客户端工作室外壳（`/studio`）。
 *
 * 这个路由故意挂在 `_app` **之外**：客户端产物在构建期就把 `_app` 整棵树排除掉了
 * （见 `vite.config.ts` 的 client 分支），所以内部页不是「藏起来」而是不在产物里。
 * 代价是要在这里自己做鉴权与最小外框。
 */
export const Route = createFileRoute("/studio")({
  beforeLoad: async () => {
    // 内部端也有这个路由（同一份源码），默认回内部首页，避免两套入口打架。
    // 双入口实例（根地址让给工作室、内部端挂 VITE_INTERNAL_ENTRY_PATH）例外，
    // 那时这个路由就是客户入口本身。
    if (!isClientMode() && internalEntryPath() === "") {
      throw redirect({ to: "/", replace: true });
    }
    if (!(await ensureAuthenticatedForAppRoute())) {
      throw redirect({ to: "/login", replace: true });
    }
  },
  component: StudioLayout,
});

function StudioLayout() {
  const username = useAuthStore((state) => state.username);
  const logout = useAuthStore((state) => state.logout);
  // 实例级署名。内部端挂在底部状态栏上，但状态栏属于 `_app`（客户端产物里整棵被
  // 排除），所以客户端外壳自己渲染一次；未设置（上游构建）时不渲染。
  const attribution = brandAttribution();

  return (
    <div className="flex min-h-dvh flex-col bg-background text-foreground">
      <header className="border-b border-border/60 bg-card/60 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-5xl items-center justify-between px-4">
          <Link to="/studio" className="text-sm font-semibold tracking-tight">
            拿捏漫剧工作台
          </Link>
          <div className="flex items-center gap-3 text-xs text-muted-foreground">
            {username ? <span className="truncate">{username}</span> : null}
            {username ? (
              <button
                type="button"
                className="transition-colors hover:text-foreground"
                onClick={() => void logout()}
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
