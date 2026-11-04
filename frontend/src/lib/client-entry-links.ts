// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab

/**
 * 客户外壳顶栏的「工作室 ↔ 捏画」互跳地址。
 *
 * 两条客户入口共用 `ClientAccessShell`：
 * - `/studio/<项目>`  一键成片工作台
 * - `/canvas/<项目>`  独立捏画
 *
 * 外壳只拿得到 pathname，所以这里把「当前在哪个入口、哪个项目」和「跳到对面哪个
 * 路由」统一算出来，别在外壳里硬编码。首页（`/studio`、`/canvas`）不带项目，
 * 返回 null —— 顶栏就不渲染互跳按钮。
 */
export type ClientEntryKind = "studio" | "canvas";

export interface ClientEntryMatch {
  kind: ClientEntryKind;
  project: string;
}

/** 从 pathname 反推「当前在哪个客户入口、哪个项目」；不是项目页时返回 null。 */
export function clientEntryFromPath(pathname: string): ClientEntryMatch | null {
  const studio = pathname.match(/^\/studio\/([^/]+)(?:\/|$)/);
  if (studio) return { kind: "studio", project: decodeURIComponent(studio[1]) };
  const canvas = pathname.match(/^\/canvas\/([^/]+)(?:\/|$)/);
  if (canvas) return { kind: "canvas", project: decodeURIComponent(canvas[1]) };
  return null;
}

export interface CrossEntryLink {
  to: "/studio/$project" | "/canvas/$project";
  params: { project: string };
  label: string;
}

/** 对面入口的 `<Link>` 目标（工作室 ↔ 捏画）。 */
export function crossEntryFor(match: ClientEntryMatch): CrossEntryLink {
  return match.kind === "studio"
    ? { to: "/canvas/$project", params: { project: match.project }, label: "进入捏画" }
    : { to: "/studio/$project", params: { project: match.project }, label: "返回工作台" };
}
