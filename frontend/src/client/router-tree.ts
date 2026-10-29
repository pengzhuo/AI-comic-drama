// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab

/**
 * 客户端产物的路由树（`vite build --mode client`）。
 *
 * 这个目录整体被 `tsconfig.app.json` 排除：客户端树只保留 `__root` / `login` /
 * `studio`，与内部树的 `FileRoutesByPath` 声明会同名不同型，放进同一个 tsc 程序
 * 必然冲突。客户端构建不跑 `tsc`（见 `package.json` 的 `build:client`）。
 *
 * `./routeTree.client.gen` 由 `@tanstack/router-plugin` 在构建时按
 * `routeFileIgnorePattern` 生成（见 `vite.config.ts`），不入库。
 */
export { routeTree } from "./routeTree.client.gen";
