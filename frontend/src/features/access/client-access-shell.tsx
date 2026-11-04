// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { Link, useRouterState } from "@tanstack/react-router";
import { useState, type ReactNode } from "react";

import { buttonVariants } from "@/components/ui/button";
import { StudioLinkGate } from "@/features/access/studio-link-gate";
import { clientEntryPath, isCanvasMode } from "@/lib/app-mode";
import { brandAttribution } from "@/lib/brand-attribution";
import { clientEntryFromPath, crossEntryFor } from "@/lib/client-entry-links";
import {
  logoutAccessSession,
  maskPhone,
  takeAccessNotice,
  useAccessSession,
} from "@/lib/access-session";
import { cn } from "@/lib/utils";
import { useAuthStore } from "@/stores/auth-store";

export interface ClientAccessShellProps {
  /**
   * 内容区。工作室会再套一层 `max-w-5xl`；画布入口整屏铺开，见 `content`。
   */
  children: ReactNode;
  /**
   * 主区的框：
   * - `scroll`（默认）：工作室那种可滚动的居中内容（`overflow-y-auto`）。
   * - `fill`：整屏铺开、由子内容自己定位/裁剪（捏画入口用；画布宿主是
   *   `absolute inset-0`，需要 `main` 做定位上下文且不能滚动）。
   */
  content?: "scroll" | "fill";
}

/**
 * 客户入口外壳（`/studio`、以及将来的 `/canvas`）。
 *
 * 这个外壳故意挂在 `_app` **之外**：客户产物在构建期就把 `_app` 整棵树排除掉了
 * （见 `vite.config.ts` 的 client 分支），所以内部页不是「藏起来」而是不在产物里。
 * 代价是要在这里自己做鉴权与最小外框。
 *
 * 访问验证启用时（后端配了 client secret），这里是客户入口的唯一收口：没有有效
 * 会话就渲染门禁页；有会话就渲染顶栏身份（客户显示脱敏手机号，内部身份回落用户名）
 * 与退出入口。链接兑换本身在路由的 `beforeLoad` 里做（见 `studio-link-entry.ts`），
 * 因为那一步要 `throw redirect`。
 */
export function ClientAccessShell({ children, content = "scroll" }: ClientAccessShellProps) {
  const access = useAccessSession() ?? { enabled: false, kind: "open" as const, phone: "" };
  const username = useAuthStore((state) => state.username);
  const [notice] = useState(() => takeAccessNotice());
  // 顶栏「工作室 ↔ 捏画」互跳按钮要看当前在哪个入口/哪个项目，所以读一次 pathname。
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  // 实例级署名。内部端挂在底部状态栏上，但状态栏属于 `_app`（客户产物里整棵被
  // 排除），所以客户外壳自己渲染一次；未设置（上游构建）时不渲染。
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
  // 只要这次请求带着访问身份（客户或内部）就给一个退出入口：纯内部身份可能没有旧
  // 登录用户名（identity 为空），以前会连按钮都不渲染，导致无法从 /studio 登出。
  const canLogout = access.enabled && (access.kind === "client" || access.kind === "internal");

  // 项目页才有的「进入捏画 / 返回工作台」互跳；首页（无项目）不显示。
  // `canvas` 产物里没有 `/studio`，所以那边不渲染「返回工作台」，免得跳到 404。
  const entry = clientEntryFromPath(pathname);
  const crossEntry =
    entry && !(entry.kind === "canvas" && isCanvasMode()) ? crossEntryFor(entry) : null;

  return (
    <div className="flex h-dvh flex-col overflow-hidden bg-background text-foreground">
      <header className="shrink-0 border-b border-border/60 bg-card/60 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-5xl items-center justify-between px-4">
          <Link to={clientEntryPath()} className="text-sm font-semibold tracking-tight">
            拿捏漫剧工作台
          </Link>
          <div className="flex items-center gap-3 text-xs text-muted-foreground">
            {identity ? <span className="truncate">{identity}</span> : null}
            {crossEntry ? (
              <Link
                to={crossEntry.to}
                params={crossEntry.params}
                className={cn(buttonVariants({ variant: "outline", size: "sm" }))}
              >
                {crossEntry.label}
              </Link>
            ) : null}
            {canLogout ? (
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
      {/* 客户外壳自己提供主区框：全站 `html/body/#root` 锁了 `overflow: hidden`，
          没有它内容一超一屏就被裁掉（顶栏/页脚固定，只滚中间）。
          `fill`（捏画）不滚动、改成相对定位：画布宿主是 `absolute inset-0`，要拿
          `main` 当定位上下文，滚动交给画布内部。 */}
      <main
        id="main-content"
        tabIndex={-1}
        className={cn(
          "flex-1 focus:outline-none",
          content === "fill"
            ? "relative min-h-0 overflow-hidden"
            : "ui-scrollbar min-h-0 overflow-y-auto",
        )}
      >
        {children}
      </main>
      {attribution ? (
        // 纯文本、无交互：保持 `text-muted-foreground` 不降透明度，12px 也要过 AA。
        <footer className="shrink-0 border-t border-border/60">
          <div className="mx-auto max-w-5xl px-4 py-3 text-right text-xs text-muted-foreground">
            <span data-testid="studio-brand-attribution">{attribution}</span>
          </div>
        </footer>
      ) : null}
    </div>
  );
}
