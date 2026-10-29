// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import type { CostReportBucket } from "@/lib/queries/cost-report";

/**
 * 费用展示口径（捏账 / 捏导客户端费用卡共用）。
 *
 * 后端回填的是账单 `quota`：`usd = quota / quota_per_usd`，`cny = usd * 汇率`。
 * 中转站按 ¥ 展示，所以界面统一用人民币。
 */

/** 费用小数位随量级自适应：小额（单张图/单次文本）也要看得出差别。 */
export function costFractionDigits(value: number): number {
  const abs = Math.abs(value);
  if (abs === 0) return 2;
  if (abs < 0.01) return 6;
  if (abs < 1) return 4;
  return 2;
}

/** 中转站按 ¥ 展示；后端 `usd_cny_rate` 缺省时用同一常数兜底。 */
export const FALLBACK_USD_CNY_RATE = 7.3;

/**
 * 报表费用（人民币）。后端回填的是账单 quota，`cny = usd * 汇率`；
 * 旧后端还没带 `cny` 时前端自己按 `usd_cny_rate`（或兜底汇率）折算。
 */
export function bucketCny(bucket: CostReportBucket, rate: number): number {
  return bucket.cny ?? bucket.usd * rate;
}

export function formatCny(value: number): string {
  const digits = costFractionDigits(value);
  return `¥${value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

export function formatQuota(value: number): string {
  return value.toLocaleString("en-US");
}

/** 秒 → 「3h 12m」/「12m 5s」/「5s」。0 表示没有可用的时间戳。 */
export function formatDuration(seconds: number): string {
  if (!seconds || seconds <= 0) return "--";
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}
