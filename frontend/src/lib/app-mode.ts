// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab

/**
 * 应用模式：同仓一份代码，构建期决定这次产物是「内部端」还是「客户端」。
 *
 * - `internal`（默认）：捏料/捏塘/捏镜/捏画/捏条/捏账 全套内部页，`/` 是项目列表。
 * - `client`：只保留 `/studio`（上传剧本 → 一键成片 → 进度 → 每集费用）。
 *   客户端产物在构建期就把内部路由排除出路由树，所以内部页**不在产物里**，
 *   而不是「藏起来」。
 *
 * 另有一个正交的实例开关 `VITE_INTERNAL_ENTRY_PATH`（见下面的
 * `internalEntryPath()`）：内部端产物 + 根地址让给 `/studio` + 内部端挂独立路径。
 *
 * 用构建期常量而不是运行时开关：客户端产物不该带着内部页的代码和文案，
 * 运行时开关做不到这一点（代码仍然在 bundle 里）。
 */
export type AppMode = "internal" | "client";

function readAppMode(): string {
  const env = import.meta.env as Record<string, unknown> | undefined;
  return String(env?.VITE_APP_MODE ?? "").trim().toLowerCase();
}

export function appMode(): AppMode {
  return readAppMode() === "client" ? "client" : "internal";
}

export function isClientMode(): boolean {
  return appMode() === "client";
}

/**
 * 双入口实例：根地址留给客户工作室，内部端（捏料/捏塘/捏镜/捏画/捏条/捏账）
 * 挂在一个独立路径上。值是内部入口路径，空/未设置 = 传统内部端（`/` 就是内部首页）。
 *
 * - 只有「一套产物 + 两个入口」的形态用得到它：`npm run build:ce` 产物里内部树与
 *   `/studio` 都在，靠这个开关决定根地址归谁。
 * - 它**不是**访问控制：同一个 bundle、同域、无鉴权，只是路径分区。
 * - 构建期常量（写进产物）。本实例值在 `.env.ce.local`，见
 *   `docs/agents/web-hotpatch-deploy.md` 的双入口产物小节。
 *
 * 改这个值时必须同步 `src/routes/_app/console.tsx`（那个路由文件的路径就是内部
 * 入口的真身，目前只支持 `/console`）。
 */
export function internalEntryPath(): string {
  const env = import.meta.env as Record<string, unknown> | undefined;
  const raw = String(env?.VITE_INTERNAL_ENTRY_PATH ?? "").trim();
  if (!raw || raw === "/") return "";
  // 归一化：必须有前导 `/`，去掉尾斜杠，避免 `/console/` 与 `/console` 两个值。
  const withLeadingSlash = raw.startsWith("/") ? raw : `/${raw}`;
  return withLeadingSlash.replace(/\/+$/, "");
}

/**
 * 内部端「回首页」该去的地方：双入口实例是内部入口（`/console`），否则就是 `/`。
 * 内部壳（品牌 logo / 返回项目列表 / 项目不存在回退 / 算力页）与 credits 都用它，
 * 免得双入口形态下把内部用户甩进客户工作室。
 *
 * 返回的是**路由字面量联合**而不是 `string`：TanStack 的 `Link to` / `navigate({to})`
 * 都按生成的路由树做类型检查，`string` 过不了。因此这里只承认有对应路由文件的
 * `/console`（`src/routes/_app/console.tsx`）—— 换路径要改路由文件名，本来就得上代码。
 */
export function internalHomePath(): "/console" | "/" {
  return internalEntryPath() === "" ? "/" : "/console";
}
