// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab

/**
 * 客户画布产物的路由树（`vite build --mode canvas`）。
 *
 * 比工作室产物还窄：只保留 `__root` / `login` / `canvas`，内部树与 `studio` 都在
 * 构建期被整棵排除（见 `vite.config.ts` 的 `CANVAS_ROUTE_IGNORE_PATTERN`）。
 *
 * 这个目录整体被 `tsconfig.app.json` 排除：三棵树（内部 / 工作室 / 画布）的
 * `FileRoutesByPath` 声明会同名不同型，放进同一个 tsc 程序必然冲突。画布构建
 * 不跑 `tsc`（见 `package.json` 的 `build:canvas`）。
 *
 * `./routeTree.canvas.gen` 由 `@tanstack/router-plugin` 在构建时生成，不入库。
 */
export { routeTree } from "./routeTree.canvas.gen";
