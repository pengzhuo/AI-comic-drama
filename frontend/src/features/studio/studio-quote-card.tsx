// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useState } from "react";
import { Link } from "@tanstack/react-router";
import { CircleSlash, Loader2, Receipt } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { formatCny, formatQuota } from "@/lib/cost-format";
import { useConfirmDirectorGate } from "@/lib/queries/director-auto";
import {
  useEpisodeVideoModel,
  useProductionEstimate,
  useUpdateEpisodeVideoModel,
} from "@/lib/queries/production-estimate";

/**
 * 客户端工作室的「本集制作报价」确认卡。
 *
 * 分镜写完之后、画面与视频开跑之前，服务端会把一键成片停在这里等客户点头
 * （`cost_estimate` 闸门，**不会**被无人值守控制器自动确认）。客户看到的是**售价**
 * （成本 × 该客户倍率），成本与未定价项由后端抹掉；没配倍率的客户只会看到
 * 「报价由客服提供」，不会看到任何金额。
 *
 * 「先不生成」只是收起这张卡（`onDismiss`），闸门没答，制作保持暂停；客户之后从
 * 进度卡的「继续」再进一次，还会被问同一件事。
 */
export function StudioQuoteCard({
  project,
  episode,
  onDismiss,
}: {
  project: string;
  episode: number;
  onDismiss: () => void;
}) {
  const [dismissed, setDismissed] = useState(false);
  const estimateQuery = useProductionEstimate(project, { episode, stage: "refined" });
  const confirmGate = useConfirmDirectorGate(project);
  const videoModelQuery = useEpisodeVideoModel(project, episode, !dismissed);
  const updateVideoModel = useUpdateEpisodeVideoModel(project);

  const estimate = estimateQuery.data?.data;
  const isClient = estimate?.view === "client";
  const price = estimate?.price?.cny ?? null;
  const points = estimate?.price?.points ?? null;
  const total = estimate?.cost?.total_cny ?? null;
  const unpricedCount = estimate?.unpriced_count ?? 0;

  // 确认即预扣伴点（后端按售价折算）。欠费/服务不可用时后端返回 402/502，把业务
  // 原文弹出来，卡片不收起——闸门没答，制作保持暂停。
  const confirmAndResume = () =>
    confirmGate.mutate(
      { gate: "cost_estimate", episode },
      {
        onSuccess: onDismiss,
        onError: (error) => {
          toast.error(
            error instanceof Error && error.message
              ? error.message
              : "伴点预扣失败，请稍后重试",
          );
        },
      },
    );

  if (dismissed) {
    return (
      <div className="flex items-center justify-between gap-3 rounded-2xl border border-border/60 bg-card px-4 py-3">
        <span className="text-sm text-muted-foreground">
          本集报价已收起，制作保持暂停。
        </span>
        <div className="flex shrink-0 items-center gap-2">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-8 rounded-full px-3 text-xs"
            onClick={() => setDismissed(false)}
          >
            再看一次
          </Button>
          <Button
            type="button"
            size="sm"
            className="h-8 rounded-full px-3.5 text-xs"
            disabled={confirmGate.isPending}
            onClick={confirmAndResume}
          >
            {confirmGate.isPending ? <Loader2 className="size-3.5 animate-spin" /> : null}
            确认报价，继续制作
          </Button>
        </div>
      </div>
    );
  }

  return (
    <section className="space-y-3 rounded-2xl border border-primary/30 bg-card p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-1.5 text-sm font-semibold">
            <Receipt className="size-4 shrink-0 text-primary" />
            {isClient ? "本集制作报价" : "本集制作成本预估"}
          </div>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            {isClient
              ? `第 ${episode} 集分镜已完成，这是制作报价。确认后就开始生成画面、配音与视频。`
              : `第 ${episode} 集分镜已完成。确认后开始生成画面、配音与视频。`}
          </p>
        </div>
      </div>

      {videoModelQuery.data?.data ? (
        <label className="flex items-center justify-between gap-3 rounded-xl border border-border/60 bg-background px-3 py-2">
          <span className="text-xs font-medium text-muted-foreground">视频模型</span>
          <select
            value={videoModelQuery.data.data.video_model}
            disabled={updateVideoModel.isPending}
            onChange={(event) =>
              updateVideoModel.mutate({
                episode,
                videoModel: event.target.value,
              })
            }
            className="h-8 max-w-[60%] rounded-lg border border-border bg-background px-2 text-xs outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/40 disabled:opacity-60"
          >
            {videoModelQuery.data.data.options.map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      ) : null}

      {estimateQuery.isLoading ? (
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 className="size-3.5 animate-spin" />
          正在估算…
        </div>
      ) : !estimate ? (
        <p className="text-xs leading-5 text-muted-foreground">
          暂时算不出预估价。可以照常继续制作。
        </p>
      ) : (
        <div className="overflow-hidden rounded-xl border border-border/60">
          <div className="divide-y divide-border/60">
            {(estimate.by_kind ?? []).map((bucket) => (
              <div key={bucket.kind} className="flex items-center justify-between px-3 py-2">
                <span className="min-w-0 truncate text-xs text-foreground/90">
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
          </div>
          <div className="flex items-center justify-between border-t border-border/60 bg-muted/30 px-3 py-2.5">
            <span className="text-xs font-medium">
              {isClient ? "报价合计" : "成本合计（含预留）"}
            </span>
            <span className="text-base font-semibold tabular-nums">
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
            <div className="border-t border-border/60 bg-primary/[0.04] px-3 py-2 text-xs leading-5 text-muted-foreground">
              确认后将从你的账户预扣{" "}
              <span className="font-semibold tabular-nums text-foreground">
                {formatQuota(points)}
              </span>{" "}
              伴点（100 伴点 = ¥1）。成片完成后按实际用量多退少补：用得省自动退还，
              超出则需补缴差额后解锁成片。
            </div>
          ) : null}
        </div>
      )}

      {estimate && !isClient && unpricedCount > 0 ? (
        <div className="flex items-start gap-2 rounded-xl border border-warning/30 bg-warning/[0.06] px-3 py-2 text-xs leading-5 text-warning">
          <CircleSlash className="mt-0.5 size-3.5 shrink-0" />
          <span>有 {unpricedCount} 项还没配单价，合计只算了配好价的项。</span>
        </div>
      ) : null}

      <div className="flex flex-wrap items-center justify-end gap-2">
        <Link
          to="/studio/quote/$project"
          params={{ project }}
          search={{ episode }}
          className="mr-auto text-xs text-muted-foreground underline-offset-4 transition-colors hover:text-foreground hover:underline"
        >
          查看完整报价单
        </Link>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-8 rounded-full px-3 text-xs"
          disabled={confirmGate.isPending}
          onClick={() => setDismissed(true)}
        >
          先不生成
        </Button>
        <Button
          type="button"
          size="sm"
          className="h-8 rounded-full px-3.5 text-xs"
          disabled={confirmGate.isPending}
          onClick={confirmAndResume}
        >
          {confirmGate.isPending ? <Loader2 className="size-3.5 animate-spin" /> : null}
          确认报价，继续制作
        </Button>
      </div>
    </section>
  );
}
