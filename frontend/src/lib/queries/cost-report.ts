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
 * 口径默认取**本地单价表**（`cost-reconcile --source local`）：按「用量 × 单价」
 * 计算，单价里的 `actual_rules` 由真账反推（实测优先），因此合计与上游真账一致，
 * 且**不登录上游**、不受中转站 429/409 影响。`--source relay` 仍可手动取上游真实
 * 账单，`--source gateway` 取本地捏驿网关账单库（对 DC-* / 媒体模型别名没有价格
 * 条目，会落到默认倍率而明显偏高）。自动回填（成片后结算钩子、前端「重新结算」
 * 按钮）现在都走 local；`cost_source === "gateway"` 时前端把金额标注为「估值口径
 * （偏高）」。
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

/** 金额来源分布：`relay`=中转站真账单，`gateway`=本地网关估值，`unmatched`=未命中。 */
export interface CostReportSource {
  source: string;
  requests: number;
  quota: number;
  usd: number;
  cny: number;
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
  /**
   * 报表金额口径：`local`=本地按用量 × 单价（实测优先，默认），`relay`=中转站真
   * 账单，`gateway`=本地网关默认倍率估值（偏高），`mixed`=多种混用，空=还没有可
   * 计价的行。旧后端缺省时为 undefined。
   */
  cost_source?: string;
  /**
   * 是否还有未归因请求：true 表示合计不完整（`unattributed.requests > 0`），
   * 即便 `cost_source === "relay"` 也只是「部分归因」。旧后端缺省时为 undefined。
   */
  partial?: boolean;
  /** 按来源拆分的金额分布（明细），旧后端缺省。 */
  sources?: CostReportSource[];
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

