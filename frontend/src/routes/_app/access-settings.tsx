// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { createFileRoute, redirect } from "@tanstack/react-router";

import { AccessSettings } from "@/features/access/access-settings";
import { fetchAccessSession } from "@/lib/access-session";
import { internalHomePath } from "@/lib/app-mode";

/**
 * 内部端「访问设置」页。入口是顶栏的盾牌图标，只对内部身份显示。
 *
 * 客户身份（`st_studio`）一律送回内部首页：那里会渲染口令门，客户不该在客户
 * 入口的壳里看到内部配置页。访问验证未启用时不拦——页面会显示「无法读取访问设置」，
 * 或让操作者一键恢复（那时身份就是本机单用户）。
 */
export const Route = createFileRoute("/_app/access-settings")({
  beforeLoad: async () => {
    const access = await fetchAccessSession();
    if (access.enabled && access.kind === "client") {
      throw redirect({ to: internalHomePath(), replace: true });
    }
  },
  component: AccessSettings,
});
