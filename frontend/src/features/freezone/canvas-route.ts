// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useRouterState } from "@tanstack/react-router";

/** 画布宿主需要的最小输入：哪个项目、当前是不是正在看画布。 */
export interface FreezoneCanvasRoute {
  /** 项目 id（URL 段），不在项目路径上时为 null。 */
  project: string | null;
  /** 画布是不是当前页面；false 表示被宿主保活在后台。 */
  active: boolean;
}

/**
 * `/projects/<项目>/<section>` → `{ project, section }`。section 可能不存在
 * （项目根路径）。
 *
 * 内部端的捏画仍然是 `/projects/<id>/freezone`；这条路径形状只属于内部壳，
 * 所以解析放在这里、由内部壳喂给 `FreezoneCanvasHost`，画布宿主本身不再认识
 * URL。这样 `/canvas/<项目>` 那种独立入口只要自己算好 `{ project, active }`
 * 就能挂同一份画布。
 */
export function projectSectionForPath(pathname: string): {
  project: string | null;
  section: string | null;
} {
  const match = pathname.match(/^\/projects\/([^/]+)(?:\/([^/]+))?/);
  if (!match) return { project: null, section: null };
  return { project: decodeURIComponent(match[1]), section: match[2] ?? null };
}

/**
 * 纯函数：内部端路径 → 画布宿主的输入。捏画（`section === "freezone"`）时
 * `active` 为真，同一项目内切到别的 section 时保持同一个 `project`，好让宿主
 * 继续保活。
 */
export function freezoneCanvasRouteForPath(pathname: string): FreezoneCanvasRoute {
  const { project, section } = projectSectionForPath(pathname);
  return { project, active: section === "freezone" };
}

/**
 * 内部壳（`_app.tsx`）用的版本：跟随路由当前路径实时求值。画布宿主被挂在
 * `_app` 的路由动画容器之外，靠这个订阅在切换 section 时只切 `active`、不重建。
 */
export function useFreezoneCanvasRoute(): FreezoneCanvasRoute {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  return freezoneCanvasRouteForPath(pathname);
}
