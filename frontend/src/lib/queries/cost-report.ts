// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { p } from "@/lib/api-path";
import { queryKeys } from "@/lib/query-keys";
import type { OkResponse } from "@/types/api";

/**
 * 项目成本报表（捏驿「捏账」页）。
 *
 * `quota` 是账单额度快照，`usd = quota / quota_per_usd`，`cny = usd * 汇率`。
 * 口径优先取上游中转站真实账单（`cost-reconcile --source relay`）：本地捏驿网关
 * 对 DC-* / 媒体模型别名没有价格条目，会落到默认倍率而明显偏高。中转站凭据未配置
 * 时只能走本地捏驿（`--source gateway`）——客户端「一键成片」每集完成后自动跑的
 * 就是这条口径，此时金额 = 用户本地捏驿被扣的量。
 *
 * `cost_quota` 尚未回填（未跑对账或没匹配上）的请求不算进 `quota`，而是计进
 * `unattributed_requests`。
 */
export interface CostReportBucket {
  requests: number;
  /** 未从账单回填到 `cost_quota` 的请求数（费用未知，不计入 quota/usd）。 */
  unattributed_requests: number;
  quota: number;
  usd: number;
  /** 人民币金额（中转站 `usd_exchange_rate` 折算）；旧后端缺省时前端兜底。 */
  cny?: number;
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
  /** 报表币种；中转站按 ¥ 展示时为 "CNY"。 */
  currency?: string;
  /** 美元 → 人民币汇率（中转站 `usd_exchange_rate`）。 */
  usd_cny_rate?: number;
  project?: { id: string; name: string; owner: string };
  /** 只有对账接口（POST .../cost-report/reconcile）返回。 */
  reconcile?: CostReportReconcileSummary;
  totals: CostReportBucket;
  by_kind: CostReportByKind[];
  by_model: CostReportByModel[];
  by_episode: CostReportByEpisode[];
  by_day: CostReportByDay[];
  unattributed: { requests: number; by_kind: Record<string, number> };
}

/** 对账接口这一次实际做了什么：命中去抖时 `skipped=true`，`matched/rows` 为 0。 */
export interface CostReportReconcileSummary {
  skipped: boolean;
  applied: boolean;
  source: string;
  quota: number;
  usd: number;
  matched: number;
  rows: number;
  errors: string[];
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

/**
 * 增量对账：按本地捏驿账单把 `cost_quota` 回填到项目库，再返回刷新后的报表。
 *
 * 服务端在每集成片完成后也会自动跑一次；这个 mutation 是给「重新结算」按钮用的，
 * 也用于用户等不及自动结算时手动催一次。`force` 才忽略服务端去抖窗口。
 */
export function useReconcileCostReport(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      episode,
      force,
    }: { episode?: number; force?: boolean } = {}) => {
      const response = await api
        .post(p`api/v1/projects/${project}/cost-report/reconcile`, {
          searchParams: {
            ...(episode ? { episode: String(episode) } : {}),
            ...(force ? { force: "true" } : {}),
          },
        })
        .json<OkResponse<CostReport>>();
      return response.data;
    },
    onSuccess: () => {
      // 项目级报表与单集报表共用同一前缀：一次对账会同时让两者过期。
      queryClient.invalidateQueries({ queryKey: queryKeys.costReportAll(project) });
    },
  });
}

