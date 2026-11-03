// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab

/**
 * 捏画的地址方案：同一份画布代码跑在两套顶层路径上。
 *
 * - `internal`：`/projects/<项目>/freezone`（内部壳里的捏画；`?canvas=` 选画布）。
 * - `canvas`：`/canvas/<项目>`（客户画布独立入口；`?canvas=` 选画布）。
 *
 * 画布栈里有几十处 `readUrl().project` / `writeUrl({ canvas })`，都靠这里把
 * 「当前是哪个项目、切画布该跳到哪」统一算出来，而不是各自硬编码 `/projects/...`。
 * 画布宿主 `FreezoneCanvasHost` 只吃 `{ project, active }`，真正认识 URL 的是本模块。
 */
export type FreezoneScheme = "internal" | "canvas";

export interface FreezoneEntry {
  scheme: FreezoneScheme;
  project: string;
}

/**
 * 从 pathname 反推「当前是不是在捏画、哪个项目」。
 *
 * - `/projects/<项目>/freezone[/...]` → internal
 * - `/canvas/<项目>[/...]` → canvas
 *
 * 只认捏画路径：`/projects/<项目>/characters` 这类同项目别的 section 返回 null
 * （既有的 `writeUrl` 就是靠这一点，在用户已经离开捏画时把过期的 canvas 写入
 * no-op 掉）。
 */
export function freezoneEntryFromPath(pathname: string): FreezoneEntry | null {
  const internal = pathname.match(/^\/projects\/([^/]+)\/freezone(?:\/|$)/);
  if (internal) {
    return { scheme: "internal", project: decodeURIComponent(internal[1]) };
  }
  const canvas = pathname.match(/^\/canvas\/([^/]+)(?:\/|$)/);
  if (canvas) {
    return { scheme: "canvas", project: decodeURIComponent(canvas[1]) };
  }
  return null;
}

/** 当前项目 id（不在捏画路径上时为 null）。`readUrl().project` 用它。 */
export function freezoneProjectFromPath(pathname: string): string | null {
  return freezoneEntryFromPath(pathname)?.project ?? null;
}

/** 捏画路径的**字面量**地址（raw History API 兜底用，不能走 router.navigate）。 */
export function freezonePathFor(scheme: FreezoneScheme, project: string): string {
  return scheme === "canvas"
    ? `/canvas/${encodeURIComponent(project)}`
    : `/projects/${encodeURIComponent(project)}/freezone`;
}

/** 离开捏画后的落点路径（raw History API 兜底用）。 */
export function freezoneLeavePathFor(scheme: FreezoneScheme, project: string): string {
  return scheme === "canvas"
    ? "/canvas"
    : `/projects/${encodeURIComponent(project)}/ingest`;
}

/**
 * 进入某项目捏画的 `router.navigate` 目标。两个 `to` 都在路由树里（内部树同时
 * 有 `/projects/$project/freezone` 与 `/canvas/$project`）。
 */
export function freezoneNavTarget(
  scheme: FreezoneScheme,
  project: string,
): { to: "/projects/$project/freezone" | "/canvas/$project"; params: { project: string } } {
  return scheme === "canvas"
    ? { to: "/canvas/$project", params: { project } }
    : { to: "/projects/$project/freezone", params: { project } };
}

/**
 * 离开捏画的 `router.navigate` 目标。内部端回到该项目的导入页（项目首页）；
 * 客户画布入口没有「项目内部页」，回到 `/canvas` 项目列表。
 */
export function freezoneLeaveTarget(
  scheme: FreezoneScheme,
  project: string,
):
  | { to: "/projects/$project/ingest"; params: { project: string } }
  | { to: "/canvas" } {
  return scheme === "canvas"
    ? { to: "/canvas" }
    : { to: "/projects/$project/ingest", params: { project } };
}
