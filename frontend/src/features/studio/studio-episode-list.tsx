// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useEffect, useRef, useState } from "react";
import { ChevronDown, Download, Loader2 } from "lucide-react";

import { StudioEpisodeCostDetail, reportRate } from "@/features/studio/studio-cost";
import { bucketCny, formatCny } from "@/lib/cost-format";
import { useCostReport } from "@/lib/queries/cost-report";
import { useFinalVideo } from "@/lib/queries/video";
import { cn } from "@/lib/utils";

/**
 * 分集成品清单：每完成一集，这一集就带着「成片 + 费用明细」出现在这里。
 *
 * 费用明细按需请求（展开才拉该集的 `by_model`），避免一部长剧在进页面时打出
 * 几十个报表请求；最新完成的一集默认展开，用户顺着往下看就行。
 */

interface EpisodeListItem {
  number: number;
  title?: string;
}

function StudioEpisodeRow({
  project,
  episode,
  title,
  active,
  costCny,
  defaultOpen,
  queryFinal,
}: {
  project: string;
  episode: number;
  title?: string;
  active: boolean;
  costCny: number | null;
  defaultOpen: boolean;
  queryFinal: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const touched = useRef(false);

  // 默认展开是异步算出来的（要等报表/成片查询回来），只在用户没动过时自动打开。
  useEffect(() => {
    if (defaultOpen && !touched.current) setOpen(true);
  }, [defaultOpen]);

  const finalVideo = useFinalVideo(project, queryFinal ? episode : 0);
  const final = finalVideo.data?.data;
  const done = Boolean(final?.exists);

  const status = done ? "已完成" : active ? "制作中" : "未开始";

  return (
    <li className="overflow-hidden rounded-xl border border-border/60 bg-card">
      <button
        type="button"
        className="flex w-full items-center gap-3 px-3 py-2.5 text-left hover:bg-muted/40"
        onClick={() => {
          touched.current = true;
          setOpen((value) => !value);
        }}
      >
        <ChevronDown
          className={cn("size-4 shrink-0 text-muted-foreground transition-transform", open && "rotate-180")}
        />
        <span className="shrink-0 text-sm font-medium">第 {episode} 集</span>
        <span className="min-w-0 flex-1 truncate text-sm text-muted-foreground">
          {title ?? ""}
        </span>
        <span className="flex shrink-0 items-center gap-1.5">
          {active && !done ? <Loader2 className="size-3.5 animate-spin text-primary" /> : null}
          <span
            className={cn(
              "text-xs",
              done
                ? "text-emerald-600 dark:text-emerald-400"
                : active
                  ? "text-primary"
                  : "text-muted-foreground",
            )}
          >
            {status}
          </span>
        </span>
        <span className="w-24 shrink-0 text-right text-sm tabular-nums">
          {costCny === null ? "—" : formatCny(costCny)}
        </span>
      </button>

      {open ? (
        <div className="space-y-3 border-t border-border/40 bg-muted/10 px-3 py-3">
          {done && final?.video_url ? (
            <div className="space-y-2">
              <video src={final.video_url} controls className="max-h-72 w-full rounded-lg bg-black" />
              <a
                href={final.video_url}
                download={final.filename || undefined}
                className="inline-flex items-center gap-1.5 text-xs text-primary hover:underline"
              >
                <Download className="size-3.5" />
                下载成片{final.filename ? `（${final.filename}）` : ""}
              </a>
            </div>
          ) : null}
          <StudioEpisodeCostDetail project={project} episode={episode} />
        </div>
      ) : null}
    </li>
  );
}

export function StudioEpisodeList({
  project,
  episodes,
  currentEpisode,
  runActive,
}: {
  project: string;
  episodes: EpisodeListItem[];
  currentEpisode: number | null;
  runActive: boolean;
}) {
  const reportQuery = useCostReport(project);
  const report = reportQuery.data?.data;
  const rate = reportRate(report);

  const costByEpisode = new Map<number, number>();
  for (const row of report?.by_episode ?? []) {
    // `episode === null` 是「没归到任何一集」的桶（公共开销），不进分集清单。
    if (row.episode === null) continue;
    costByEpisode.set(row.episode, bucketCny(row, rate));
  }

  // 最新「已完成」的集：优先当前集的前一集（成片后 run.episode 已经翻页）。
  const latestCompletedHint =
    currentEpisode && currentEpisode > 1 ? currentEpisode - 1 : null;

  if (episodes.length === 0) {
    return (
      <div className="text-sm text-muted-foreground">
        还没有剧集。导入剧本并开始制作后，这里会逐集出现成品与费用。
      </div>
    );
  }

  return (
    <ul className="space-y-2">
      {episodes.map((item) => (
        <StudioEpisodeRow
          key={item.number}
          project={project}
          episode={item.number}
          title={item.title}
          active={runActive && item.number === currentEpisode}
          costCny={costByEpisode.get(item.number) ?? null}
          defaultOpen={item.number === latestCompletedHint}
          // 只为「可能已有成片」的集查成片，避免长剧打出几十个请求。
          queryFinal={currentEpisode === null || item.number <= currentEpisode + 1}
        />
      ))}
    </ul>
  );
}
