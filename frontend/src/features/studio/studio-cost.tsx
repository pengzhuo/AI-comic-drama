// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  bucketCny,
  FALLBACK_USD_CNY_RATE,
  formatCny,
  formatDuration,
  formatQuota,
} from "@/lib/cost-format";
import {
  useCostReport,
  useReconcileCostReport,
  type CostReport,
} from "@/lib/queries/cost-report";
import { cn } from "@/lib/utils";

/**
 * 费用展示（客户端工作室）：用户只看「这一集花了多少钱、花在哪个模型上」。
 *
 * 口径说明（页面上也照实说）：金额来自账单 `quota`，`usd = quota / 500000`，
 * 再按汇率折成 ¥。**没有网关单号的请求（比如 TTS）算不进金额**，会如实报
 * 「未归因 N 笔」，而不是把数字静默做小。
 */

export function reportRate(report: CostReport | undefined): number {
  const rate = report?.usd_cny_rate;
  return typeof rate === "number" && rate > 0 ? rate : FALLBACK_USD_CNY_RATE;
}

function StatChip({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="rounded-lg border border-border/60 bg-muted/30 px-3 py-2">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="mt-0.5 text-sm font-semibold tabular-nums" title={hint}>
        {value}
      </div>
    </div>
  );
}

function ReconcileButton({ project, episode }: { project: string; episode?: number }) {
  const reconcile = useReconcileCostReport(project);
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      disabled={reconcile.isPending}
      onClick={() =>
        reconcile.mutate({ episode, force: true }, {
          onError: (error) => {
            // 对账失败不影响报表可读，这里只提示一次。
            console.warn("cost reconcile failed", error);
          },
        })
      }
    >
      <RefreshCw className={cn("size-3.5", reconcile.isPending && "animate-spin")} />
      重新结算
    </Button>
  );
}

/** 单集费用明细：分模型 + 原始 quota + 未归因笔数。 */
export function StudioEpisodeCostDetail({
  project,
  episode,
}: {
  project: string;
  episode: number;
}) {
  const reportQuery = useCostReport(project, episode);
  const report = reportQuery.data?.data;
  const rate = reportRate(report);
  const unattributed = report?.unattributed.requests ?? 0;
  const settled = (report?.totals.requests ?? 0) > 0 && unattributed === 0;

  if (reportQuery.isLoading) {
    return <div className="text-sm text-muted-foreground">正在读取费用…</div>;
  }
  if (!report) {
    return <div className="text-sm text-muted-foreground">暂无费用数据。</div>;
  }

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <StatChip
          label="本集合计"
          value={formatCny(bucketCny(report.totals, rate))}
          hint={`原始 quota ${formatQuota(report.totals.quota)}`}
        />
        <StatChip label="计费请求" value={`${report.totals.requests} 笔`} />
        <StatChip label="制作时长" value={formatDuration(report.totals.duration_seconds)} />
        <StatChip
          label="未归因"
          value={`${unattributed} 笔`}
          hint="没有网关单号的请求无法计价"
        />
      </div>

      {!settled ? (
        <p className="text-xs text-amber-600 dark:text-amber-500">
          结算中：{unattributed} 笔请求还没有账单单价，金额偏小属正常现象；成片后会自动重新结算。
        </p>
      ) : null}

      <div className="overflow-hidden rounded-lg border border-border/60">
        <table className="w-full text-sm">
          <thead className="bg-muted/40 text-xs text-muted-foreground">
            <tr>
              <th className="px-3 py-2 text-left font-medium">类型</th>
              <th className="px-3 py-2 text-left font-medium">模型</th>
              <th className="px-3 py-2 text-right font-medium">请求</th>
              <th className="px-3 py-2 text-right font-medium">原始 quota</th>
              <th className="px-3 py-2 text-right font-medium">金额</th>
            </tr>
          </thead>
          <tbody>
            {report.by_model.length === 0 ? (
              <tr>
                <td className="px-3 py-3 text-muted-foreground" colSpan={5}>
                  这一集还没有计费请求。
                </td>
              </tr>
            ) : (
              report.by_model.map((row) => (
                <tr key={`${row.kind}:${row.model}`} className="border-t border-border/40">
                  <td className="px-3 py-2 text-muted-foreground">{row.kind_label}</td>
                  <td className="px-3 py-2 font-mono text-xs">{row.model}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{row.requests}</td>
                  <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
                    {formatQuota(row.quota)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {formatCny(bucketCny(row, rate))}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <div className="flex items-center justify-between gap-3">
        <p className="text-[11px] leading-relaxed text-muted-foreground">
          金额按账单额度折算（1 美元 = {formatQuota(report.quota_per_usd)} 额度
          {report.currency ? `，展示币种 ${report.currency}` : ""}）。失败请求不计费。
        </p>
        <ReconcileButton project={project} episode={episode} />
      </div>
    </div>
  );
}

/** 全剧费用总览：合计 + 每集合计（点开某一集才拉该集明细）。 */
export function StudioCostOverview({ project }: { project: string }) {
  const reportQuery = useCostReport(project);
  const report = reportQuery.data?.data;
  const rate = reportRate(report);

  if (reportQuery.isLoading) {
    return <div className="text-sm text-muted-foreground">正在读取费用…</div>;
  }
  if (!report) {
    return (
      <div className="flex items-center justify-between gap-3">
        <span className="text-sm text-muted-foreground">暂无费用数据。</span>
        <ReconcileButton project={project} />
      </div>
    );
  }

  const attributed = (report.totals.requests ?? 0) - (report.unattributed.requests ?? 0);
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <StatChip
          label="全剧合计"
          value={formatCny(bucketCny(report.totals, rate))}
          hint={`原始 quota ${formatQuota(report.totals.quota)}`}
        />
        <StatChip label="已计价请求" value={`${attributed} 笔`} />
        <StatChip label="未归因" value={`${report.unattributed.requests} 笔`} />
        <StatChip label="制作时长" value={formatDuration(report.totals.duration_seconds)} />
      </div>
      <div className="flex items-center justify-between gap-3">
        <p className="text-[11px] text-muted-foreground">
          未归因的请求（多为配音）没有网关单号，暂时无法计价。
        </p>
        <ReconcileButton project={project} />
      </div>
    </div>
  );
}
