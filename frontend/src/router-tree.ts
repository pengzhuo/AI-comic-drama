// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab

/**
 * 路由树入口。同一份源码有两棵互不相交的树：
 *
 * - 内部端（默认构建）：`./routeTree.gen`，含捏料/捏塘/捏镜/捏画/捏条/捏账全部路由。
 * - 客户端（`vite build --mode client`）：只有 `__root` / `login` / `studio`，
 *   由 `vite.config.ts` 用 alias 把 `@/router-tree` 指到 `./client/router-tree`。
 *
 * 走一层转发而不是在 `main.tsx` 里直接 import，是为了让「这次构建用的是哪棵树」
 * 只由构建配置决定：`main.tsx` 一句话都不改，两棵树的类型也不会在同一个 tsc
 * 程序里相遇（`src/client` 已从 `tsconfig.app.json` 排除）。
 */
export { routeTree } from "./routeTree.gen";
