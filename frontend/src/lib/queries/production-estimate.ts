// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { p } from "@/lib/api-path";
import { queryKeys } from "@/lib/query-keys";
import type { OkResponse } from "@/types/api";

/**
 * 制作成本预估与报价单（后端 `production_estimate.py` / `production_prices.py`）。
 *
 * 口径是**上游真金白银**（¥）：价格来自可编辑单价表（`/data/production-prices.json`），
 * 来源优先级是 手工规则 > 账单反推 > 上游同步价目 > 未定价。未定价的项**不会**
 * 静默按 0 计，而是进 `unpriced` 清单如实暴露。
 *
 * `stage` 是 `coarse`（只有字数，按假设表推）或 `refined`（分镜已写完，逐项数得出来）。
 *
 * 客户视角（`view === "client"`）时后端会把成本全部抹掉、只留 `price`（成本 ×
 * 该客户倍率）；未配倍率则 `price.cny` 为 `null`，前端不显示任何金额。
 */

export type ProductionEstimateStage = "auto" | "coarse" | "refined";
export type ProductionQuoteFormat = "md" | "csv" | "txt";

export interface ProductionEstimateItem {
  kind: string;
  kind_label?: string;
  model?: string;
  label: string;
  unit: string;
  /** 该单位的价格；客户视角恒为 null。 */
  unit_price: number | null;
  /** 金额（¥）；客户视角恒为 null。 */
  cny: number | null;
  quantity: number;
  episode?: number | null;
  rule_id?: string | null;
  /** manual | actual | synced | unpriced（客户视角为空串）。 */
  source?: string;
  note?: string;
}

export interface ProductionEstimateByKind {
  kind: string;
  kind_label: string;
  items: number;
  /** 该种类的成本（¥）；客户视角恒为 null。 */
  cny: number | null;
  unpriced: number;
}

export interface ProductionEstimate {
  stage: "coarse" | "refined";
  episode: number | null;
  episode_count?: number | null;
  currency: string;
  /** internal 或 client。 */
  view: "internal" | "client";
  generated_at?: string;
  contingency_pct: number;
  assumptions: Record<string, number>;
  /** 客户视角为 null（防泄利润）。 */
  cost: { cny: number; contingency_cny: number; total_cny: number } | null;
  price: { cny: number | null; markup: number | null; markup_source: string };
  by_kind: ProductionEstimateByKind[];
  items: ProductionEstimateItem[];
  unpriced: ProductionEstimateItem[];
  unpriced_count: number;
  priced_count: number;
  actual?: { cny: number | null; delta_pct: number | null };
  calibrated_at?: string;
  prices_synced_at?: string;
  project?: string;
}

export interface ProductionEstimateParams {
  episode?: number | null;
  stage?: ProductionEstimateStage;
  chars?: number;
  episodes?: number;
  chapters?: number;
  /** 「以客户 X 的身份看」：只有内部端能用。 */
  asClient?: string;
  actual?: boolean;
  enabled?: boolean;
}

export function useProductionEstimate(
  project: string,
  params: ProductionEstimateParams = {},
) {
  const {
    episode,
    stage = "auto",
    chars,
    episodes,
    chapters,
    asClient,
    actual,
    enabled = true,
  } = params;
  return useQuery({
    queryKey: queryKeys.productionEstimate(project, episode ?? null, stage),
    queryFn: ({ signal }) =>
      api
        .get(p`api/v1/projects/${project}/production-estimate`, {
          signal,
          searchParams: {
            stage,
            ...(episode ? { episode: String(episode) } : {}),
            ...(chars ? { chars: String(chars) } : {}),
            ...(episodes ? { episodes: String(episodes) } : {}),
            ...(chapters ? { chapters: String(chapters) } : {}),
            ...(asClient ? { as_client: asClient } : {}),
            ...(actual ? { actual: "true" } : {}),
          },
        })
        .json<OkResponse<ProductionEstimate>>(),
    enabled: !!project && enabled,
    staleTime: 15_000,
  });
}

/** 报价单（服务端渲染的 md/csv/txt）；返回文件名与内容，由调用方决定下载/复制。 */
export async function fetchProductionQuote(
  project: string,
  params: {
    episode?: number | null;
    stage?: ProductionEstimateStage;
    format?: ProductionQuoteFormat;
    title?: string;
    asClient?: string;
  } = {},
): Promise<{ format: ProductionQuoteFormat; filename: string; content: string }> {
  const { episode, stage = "auto", format = "md", title, asClient } = params;
  const response = await api
    .get(p`api/v1/projects/${project}/production-estimate/quote`, {
      searchParams: {
        format,
        stage,
        ...(episode ? { episode: String(episode) } : {}),
        ...(title ? { title } : {}),
        ...(asClient ? { as_client: asClient } : {}),
      },
    })
    .json<
      OkResponse<{
        format: ProductionQuoteFormat;
        filename: string;
        content: string;
        view: string;
      }>
    >();
  return response.data;
}

// ---------------------------------------------------------------------------
// 单价表（仅内部端 /console）
// ---------------------------------------------------------------------------

export interface ProductionPriceRule {
  id?: string;
  match?: Record<string, unknown>;
  kind?: string;
  model?: string;
  unit: string;
  price: number;
  note?: string;
  /** actual_rules 独有：标定样本数与总金额。 */
  samples?: number;
  total_cny?: number;
  total_units?: number;
}

export interface ProductionPriceRelay {
  label: string;
  base_url: string;
  usd_cny_rate: number;
  quota_per_unit?: number;
  pricing_version?: string;
  models?: number;
}

export interface ProductionPriceBook {
  path: string;
  currency: string;
  contingency_pct: number;
  markup_default: number | null;
  client_markups: Record<string, { markup?: number; label?: string } | number>;
  assumptions: Record<string, number>;
  default_assumptions: Record<string, number>;
  aliases: Record<string, string>;
  units: Record<string, string>;
  rules: ProductionPriceRule[];
  actual_rules: ProductionPriceRule[];
  calibrated_at: string;
  synced: {
    fetched_at: string;
    relays: ProductionPriceRelay[];
    model_count: number;
    errors: string[];
  };
}

export function useProductionPriceBook(options: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: queryKeys.productionPriceBook(),
    queryFn: ({ signal }) =>
      api
        .get(p`api/v1/settings/production-prices`, { signal })
        .json<OkResponse<ProductionPriceBook>>(),
    enabled: options.enabled ?? true,
    staleTime: 30_000,
  });
}

export function useUpdateProductionPrices() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (patch: Partial<ProductionPriceBook> & Record<string, unknown>) => {
      const response = await api
        .put(p`api/v1/settings/production-prices`, { json: patch })
        .json<OkResponse<ProductionPriceBook>>();
      return response.data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.productionPriceBook() });
    },
  });
}

export function useSyncProductionPrices() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      const response = await api
        .post(p`api/v1/settings/production-prices/sync`)
        .json<
          OkResponse<{
            fetched_at: string;
            relays: ProductionPriceRelay[];
            models: number;
            errors: string[];
          }>
        >();
      return response.data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.productionPriceBook() });
    },
  });
}

export function useCalibrateProductionPrices() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ project, apply }: { project: string; apply?: boolean }) => {
      const response = await api
        .post(p`api/v1/settings/production-prices/calibrate`, {
          searchParams: {
            project,
            ...(apply ? { apply: "true" } : {}),
          },
        })
        .json<
          OkResponse<{
            preview: boolean;
            rules: ProductionPriceRule[];
            metadata: Record<string, unknown>;
          }>
        >();
      return response.data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.productionPriceBook() });
    },
  });
}
