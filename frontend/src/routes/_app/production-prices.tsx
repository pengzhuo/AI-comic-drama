// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { createFileRoute, redirect } from "@tanstack/react-router";

import { ProductionPricesSettings } from "@/features/access/production-prices";
import { fetchAccessSession } from "@/lib/access-session";
import { internalHomePath } from "@/lib/app-mode";

/**
 * 内部端「制作单价表」页。入口是顶栏的票据图标，只对内部身份显示。
 *
 * 客户身份一律送回内部首页：报价的定价与加价倍率是内部信息，不该出现在客户外壳里。
 */
export const Route = createFileRoute("/_app/production-prices")({
  beforeLoad: async () => {
    const access = await fetchAccessSession();
    if (access.enabled && access.kind === "client") {
      throw redirect({ to: internalHomePath(), replace: true });
    }
  },
  component: ProductionPricesSettings,
});
