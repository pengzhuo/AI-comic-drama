// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useState } from "react";
import { CircleSlash, Loader2, Receipt } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { jsonWithBackendError } from "@/lib/api-errors";
import { p } from "@/lib/api-path";
import { formatCny, formatQuota } from "@/lib/cost-format";
import { useProductionEstimate } from "@/lib/queries/production-estimate";
import { cn } from "@/lib/utils";

/**
 * 分镜写完之后、第一批图/视频开跑之前的「制作成本预估」闸门卡片。
 *
 * 口径是**上游真金白银**（¥），数出来的是这一刻已经能确定的开销：分镜数、时长、
 * 配音字数、文本 token。**未定价的项不会被静默按 0 计**——它会进 `unpriced` 清单
 * 在卡片里如实报出来，因为「看起来便宜但其实是不知道」比「贵」危险得多。
 *
 * 客户链接看到的是**售价**（成本 × 该客户倍率），成本、单价、未定价项都被后端抹掉；
 * 没给这个客户配倍率时后端连售价都不给，卡片就只说「报价由客服提供」。
 */
export function DirectorCostEstimateCard({
  project,
  episode,
  busy = false,
  onResolved,
}: {
  project: string;
  episode: number;
  busy?: boolean;
  onResolved: () => void;
}) {
  const estimateQuery = useProductionEstimate(project, {
    episode,
    stage: "refined",
  });
  const [confirming, setConfirming] = useState(false);
  const [dismissing, setDismissing] = useState(false);

  const estimate = estimateQuery.data?.data;
  const isClient = estimate?.view === "client";
  const loading = estimateQuery.isLoading;
  const failed = estimateQuery.isError;

  const resolve = async () => {
    if (confirming || busy) return;
    setConfirming(true);
    try {
      // 直接打确认端点：后端在写闸门答案之前会先预扣伴点，业务失败（如余额不足）
      // 返回 402 + ``detail``，交给 jsonWithBackendError 取出来弹给用户。
      await jsonWithBackendError<unknown>(
        api.post(
          p`api/v1/projects/${project}/chat/director-auto/gates/cost_estimate/confirm`,
          { json: { episode }, throwHttpErrors: false },
        ),
      );
      onResolved();
    } catch (error) {
      // 欠费/服务不可用时弹业务原文且不收起卡片——闸门没答，run 仍停在这里。
      toast.error(
        error instanceof Error && error.message
          ? error.message
          : "伴点预扣失败，请稍后重试",
      );
    } finally {
      setConfirming(false);
    }
  };

  const total = estimate?.cost?.total_cny ?? null;
  const price = estimate?.price?.cny ?? null;
  const points = estimate?.price?.points ?? null;
  const unpricedCount = estimate?.unpriced_count ?? 0;
  const coarse = estimate?.stage === "coarse";

  return (
    <div className="border-b border-black/10 bg-black/[0.03] px-4 pb-3 pt-3 text-sm">
      <div className="mb-2 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 text-sm font-medium leading-5 text-foreground">
            <Receipt className="size-3.5 shrink-0 text-primary" />
            {isClient ? "本集制作报价" : "本集制作成本预估"}
          </div>
          <div className="mt-0.5 text-xs leading-5 text-muted-foreground">
            {isClient
              ? "这是本集的制作报价。确认后就开始生成画面、配音与视频。"
              : "分镜已完成，接下来要生成画面、配音与视频——这些是真花钱的步骤。"}
          </div>
        </div>
        {coarse ? (
          <span className="shrink-0 rounded-full bg-black/[0.05] px-2 py-1 text-xs tabular-nums text-muted-foreground">
            粗略估算
          </span>
        ) : null}
      </div>

      <div className="mb-2 overflow-hidden rounded-2xl bg-black/[0.035] ring-1 ring-black/[0.07]">
        {loading ? (
          <div className="flex items-center gap-2 px-3 py-4 text-xs text-muted-foreground">
            <Loader2 className="size-3.5 animate-spin" />
            正在估算…
          </div>
        ) : failed || !estimate ? (
          <div className="px-3 py-4 text-xs text-muted-foreground">
            暂时算不出预估金额。可以照常继续制作，页面稍后会再试。
          </div>
        ) : (
          <div className="divide-y divide-black/[0.06]">
            {(estimate.by_kind ?? []).map((bucket) => (
              <div
                key={bucket.kind}
                className="flex items-center justify-between gap-3 px-3 py-2"
              >
                <span className="min-w-0 truncate text-xs leading-5 text-foreground/90">
                  {bucket.kind_label}
                  <span className="ml-1.5 tabular-nums text-muted-foreground">
                    {bucket.items}
                  </span>
                </span>
                <span className="shrink-0 text-xs tabular-nums text-foreground/90">
                  {bucket.cny === null ? "—" : formatCny(bucket.cny)}
                </span>
              </div>
            ))}
            <div className="flex items-center justify-between gap-3 bg-black/[0.03] px-3 py-2.5">
              <span className="text-xs font-medium leading-5 text-foreground">
                {isClient ? "报价合计" : "成本合计（含预留）"}
              </span>
              <span className="text-sm font-semibold tabular-nums text-foreground">
                {isClient
                  ? price === null
                    ? "由客服提供"
                    : formatCny(price)
                  : total === null
                    ? "—"
                    : formatCny(total)}
              </span>
            </div>
            {isClient && points !== null && points > 0 ? (
              <div className="bg-primary/[0.05] px-3 py-2 text-xs leading-5 text-muted-foreground">
                确认后将从你的账户预扣{" "}
                <span className="font-semibold tabular-nums text-foreground">
                  {formatQuota(points)}
                </span>{" "}
                伴点（100 伴点 = ¥1）。
              </div>
            ) : null}
          </div>
        )}
      </div>

      {estimate && !isClient && unpricedCount > 0 ? (
        <div className="mb-2 flex items-start gap-2 rounded-xl bg-black/[0.035] px-3 py-2 text-xs leading-5 text-warning">
          <CircleSlash className="mt-0.5 size-3.5 shrink-0" />
          <span>
            有 {unpricedCount} 项还没配单价，上面的合计只算了配好价的项；补齐单价表后再估
            一次才是全价。
          </span>
        </div>
      ) : null}

      {estimate && coarse ? (
        <div className="mb-2 px-1 text-xs leading-5 text-muted-foreground">
          还没有分镜，暂按字数与假设推的粗略值；分镜写完后会自动换成逐项精估。
        </div>
      ) : null}

      <div className="flex items-center justify-end gap-2">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className={cn(
            "h-8 rounded-full px-3 text-xs text-muted-foreground hover:bg-black/[0.08] hover:text-foreground",
          )}
          disabled={busy || confirming || dismissing}
          onClick={() => {
            setDismissing(true);
            // 只是先收起卡片：闸门没答，run 仍停在这里，下次「继续」还会再问一次。
            onResolved();
          }}
        >
          先不生成
        </Button>
        <Button
          type="button"
          size="sm"
          className="h-8 rounded-full px-3.5 text-xs"
          disabled={busy || confirming}
          onClick={() => void resolve()}
        >
          {confirming ? <Loader2 className="size-3.5 animate-spin" /> : null}
          继续制作
        </Button>
      </div>
    </div>
  );
}
