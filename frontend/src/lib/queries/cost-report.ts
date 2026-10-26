// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { p } from "@/lib/api-path";
import { queryKeys } from "@/lib/query-keys";
import type { OkResponse } from "@/types/api";

/**
 * 捏驿口径的项目成本报表。
 *
 * `quota` 是网关（NewAPI）账单里的额度快照，`usd = quota / quota_per_usd`。
 * `cost_quota` 尚未回填（`novelvideo cost-reconcile` 未跑或没匹配上）的请求不算进
 * `quota`，而是计进 `unattributed_requests`。
 */
export interface CostReportBucket {
  requests: number;
  /** 未从网关账单回填到 `cost_quota` 的请求数（费用未知，不计入 quota/usd）。 */
  unattributed_requests: number;
  quota: number;
  usd: number;
  /** 制作时长：该集视频最早提交 → 最晚完成，缺时间戳时为 0。 */
  duration_seconds: number;
  duration_hours: number;
  started_at: string | null;
  finished_at: string | null;
}

export interface CostReportByKind extends CostReportBucket {
  kind: string;
  kind_label: string;
}

export interface CostReportByModel extends CostReportBucket {
  kind: string;
  kind_label: string;
  model: string;
}

export interface CostReportByEpisode extends CostReportBucket {
  episode: number | null;
}

export interface CostReportByDay extends CostReportBucket {
  date: string;
}

export interface CostReport {
  generated_at: string;
  /** 非空表示这是一次单集查询，各分组的请求都只含这一集。 */
  episode: number | null;
  quota_per_usd: number;
  project?: { id: string; name: string; owner: string };
  totals: CostReportBucket;
  by_kind: CostReportByKind[];
  by_model: CostReportByModel[];
  by_episode: CostReportByEpisode[];
  by_day: CostReportByDay[];
  unattributed: { requests: number; by_kind: Record<string, number> };
}

export function useCostReport(project: string, episode?: number) {
  return useQuery({
    queryKey: queryKeys.costReport(project, episode),
    queryFn: ({ signal }) =>
      api
        .get(p`api/v1/projects/${project}/cost-report`, {
          signal,
          ...(episode ? { searchParams: { episode: String(episode) } } : {}),
        })
        .json<OkResponse<CostReport>>(),
    enabled: !!project,
    // 费用是慢变数据（要有网关账单回填才会动），不必每次进页面都拉。
    staleTime: 60_000,
  });
}
