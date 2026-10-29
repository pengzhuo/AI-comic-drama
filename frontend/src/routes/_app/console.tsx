// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { createFileRoute, redirect } from "@tanstack/react-router";

import { internalEntryPath } from "@/lib/app-mode";
import { ProjectDashboard } from "./index";

/**
 * 双入口实例的**内部入口**：与 `/` 同一份内部首页（项目列表），只是换了个地址。
 *
 * 背景：本实例一套产物同时服务两类人 —— 根地址 `/` 给客户工作室（`/studio`），
 * 内部端（捏料/捏塘/捏镜/捏画/捏条/捏账）挂 `VITE_INTERNAL_ENTRY_PATH`（`/console`）。
 * 内部端的深链（`/projects/<id>/...`）仍在这棵路由树上，只有「入口」需要一个
 * 稳定、好记的地址，否则内部用户只能靠猜 `/projects/...`。
 *
 * 未配置双入口（上游 CE/EE 内部产物、客户端产物）时这个路由不该有第二个首页，
 * 直接回 `/`。注意：这个文件的路径必须与 `internalEntryPath()` 的取值一致。
 */
export const Route = createFileRoute("/_app/console")({
  beforeLoad: () => {
    if (internalEntryPath() === "") {
      throw redirect({ to: "/", replace: true });
    }
  },
  component: ProjectDashboard,
});
