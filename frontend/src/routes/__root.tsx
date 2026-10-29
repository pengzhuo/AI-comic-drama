// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import {
  createRootRoute,
  Outlet,
  redirect,
  useNavigate,
  useRouterState,
  type ErrorComponentProps,
} from "@tanstack/react-router";
import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { AppUpdateRequired } from "@/components/app-update-required";
import { ConfirmDialogHost } from "@/components/confirm-dialog-host";
import { ThemedToaster } from "@/components/themed-toaster";
import { internalEntryPath, isClientMode } from "@/lib/app-mode";
import { isChunkLoadError } from "@/lib/chunk-load-recovery";

/**
 * Moves keyboard focus to the main landmark on each navigation, so
 * screen-reader and keyboard users don't lose orientation after a route
 * change. The `<main id="main-content" tabIndex={-1}>` lives in _app.tsx.
 * Skipped on first mount — initial focus stays wherever the browser placed
 * it (usually the URL bar).
 */
function RouteFocusManager() {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const didMount = useRef(false);
  useEffect(() => {
    if (!didMount.current) {
      didMount.current = true;
      return;
    }
    const main = document.getElementById("main-content");
    main?.focus({ preventScroll: true });
  }, [pathname]);
  return null;
}

function RootLayout() {
  return (
    <>
      <RouteFocusManager />
      <Outlet />
      <ThemedToaster />
      <ConfirmDialogHost />
    </>
  );
}

function RootErrorComponent({ error }: ErrorComponentProps) {
  const { t } = useTranslation();
  if (isChunkLoadError(error)) {
    return <AppUpdateRequired />;
  }

  return (
    <div className="flex h-dvh items-center justify-center bg-background px-6 text-foreground">
      <div className="w-full max-w-lg rounded-2xl border border-destructive/30 bg-destructive/5 p-8">
        <h1 className="text-lg font-semibold">{t("app.routeError.title")}</h1>
        <p className="mt-3 break-words text-sm leading-6 text-muted-foreground">
          {error instanceof Error ? error.message : t("app.routeError.description")}
        </p>
      </div>
    </div>
  );
}

/**
 * 客户端产物里没有 `/`（内部首页 `_app/index.tsx` 被整棵排除），客户直接打开
 * 域名根路径时不该撞上「页面不存在」。这里把根路径送回工作室。
 *
 * 双入口实例（`VITE_INTERNAL_ENTRY_PATH`）也走这里：同一份产物里内部首页确实
 * 存在，但根地址是客户入口，内部端在 `/console` 之类的前缀上——`/` 仍然是
 * 「该去工作室」的信号。
 */
function ClientNotFoundRedirect() {
  const navigate = useNavigate();
  useEffect(() => {
    void navigate({ to: "/studio", replace: true });
  }, [navigate]);
  return (
    <div className="flex h-dvh items-center justify-center bg-background text-sm text-muted-foreground">
      正在打开工作室…
    </div>
  );
}

// 根地址归客户工作室的两种产物：纯客户端产物（没有内部首页）、双入口产物
// （有内部首页，但内部入口在别的路径上）。内部产物里两者都是常量 false，分支被摇掉。
function rootBelongsToStudio(): boolean {
  return isClientMode() || internalEntryPath() !== "";
}

export const Route = createRootRoute({
  component: RootLayout,
  errorComponent: RootErrorComponent,
  beforeLoad: ({ location }) => {
    if (rootBelongsToStudio() && location.pathname === "/") {
      throw redirect({ to: "/studio", replace: true });
    }
  },
  notFoundComponent: rootBelongsToStudio() ? ClientNotFoundRedirect : undefined,
});
