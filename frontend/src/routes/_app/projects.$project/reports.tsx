// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { createFileRoute } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import { CircleSlash, Clock, Coins, Hash, Receipt } from "lucide-react";

import { HeaderRefreshButton } from "@/components/ui/header-refresh-button";
import { useCostReport, type CostReportBucket } from "@/lib/queries/cost-report";
import { useEpisodes } from "@/lib/queries/episodes";
import { cn } from "@/lib/utils";

/** 费用小数位随量级自适应：小额（单张图/单次文本）也要看得出差别。 */
function usdFractionDigits(value: number): number {
  const abs = Math.abs(value);
  if (abs === 0) return 2;
  if (abs < 0.01) return 6;
  if (abs < 1) return 4;
  return 2;
}

function formatUsd(value: number): string {
  const digits = usdFractionDigits(value);
  return `$${value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

function formatQuota(value: number): string {
  return value.toLocaleString("en-US");
}

function formatCount(value: number): string {
  return value.toLocaleString("en-US");
}

/** 秒 → 「3h 12m」/「12m 5s」/「5s」。0 表示没有可用的时间戳。 */
function formatDuration(seconds: number): string {
  if (!seconds || seconds <= 0) return "--";
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${secs}s`;
  return `${secs}s`;
}

/** 后端写的是本地 naive ISO，直接字符串裁剪，避免显式时区换算把时刻挪走。 */
function formatTimestamp(value: string | null): string {
  if (!value) return "--";
  return value.replace("T", " ").slice(0, 16);
}

const TABLE_HEAD_CLASS = "bg-black/[0.035] text-xs text-foreground/38";
const TABLE_HEAD_CELL_CLASS = "px-4 py-3 font-medium";
const TABLE_ROW_CLASS = "border-t border-black/8";

function StatItem({
  icon: Icon,
  label,
  value,
}: {
  icon: typeof Coins;
  label: string;
  value: string;
}) {
  return (
    <div className="flex items-center gap-2">
      <Icon className="size-3.5 shrink-0 text-muted-foreground" />
      <div className="flex items-center gap-5">
        <span className="truncate text-[11px] text-muted-foreground">{label}</span>
        <span className="shrink-0 text-xs font-medium tabular-nums text-foreground">
          {value}
        </span>
      </div>
    </div>
  );
}

function SectionCard({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="overflow-hidden rounded-[10px] border border-black/[0.085] bg-black/[0.015]">
      <header className="flex items-baseline justify-between gap-3 border-b border-black/[0.075] px-4 py-3">
        <h2 className="text-sm font-medium text-foreground">{title}</h2>
        {hint ? (
          <span className="text-[11px] text-muted-foreground/70">{hint}</span>
        ) : null}
      </header>
      {children}
    </section>
  );
}

/** 请求数 / 额度 / 费用 三列，各组表复用，保证数字右对齐、等宽。 */
function UsageCells({ bucket }: { bucket: CostReportBucket }) {
  return (
    <>
      <td className="px-4 py-3 text-right tabular-nums text-foreground/58">
        {formatCount(bucket.requests)}
      </td>
      <td className="px-4 py-3 text-right tabular-nums text-foreground/45">
        {formatQuota(bucket.quota)}
      </td>
      <td className="px-4 py-3 text-right font-medium tabular-nums">
        {formatUsd(bucket.usd)}
      </td>
    </>
  );
}

function EmptyRow({ colSpan, text }: { colSpan: number; text: string }) {
  return (
    <tr>
      <td colSpan={colSpan} className="px-4 py-10 text-center text-sm text-muted-foreground">
        {text}
      </td>
    </tr>
  );
}

function ReportsPage() {
  const { t } = useTranslation();
  const { project } = Route.useParams();
  const [episode, setEpisode] = useState<number | undefined>(undefined);

  const { data, isLoading, isRefetching, refetch } = useCostReport(project, episode);
  const { data: episodesRes } = useEpisodes(project);

  const report = data?.data;
  const totals = report?.totals;
  const episodeOptions = useMemo(
    () => (episodesRes?.data ?? []).map((item) => item.number).sort((a, b) => a - b),
    [episodesRes?.data],
  );
  const unattributedCount = report?.unattributed.requests ?? 0;

  return (
    <div className="-m-6 flex h-[calc(100%+3rem)] flex-col overflow-hidden">
      <div className="flex shrink-0 flex-col gap-3 border-b border-border/30 bg-background px-9 py-5 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          <span className="mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-md bg-primary text-primary-foreground">
            <Receipt className="size-[18px]" />
          </span>
          <div className="min-w-0">
            <h1 className="truncate text-2xl font-semibold tracking-tight text-foreground">
              {t("nav.reports")}
            </h1>
            <p className="mt-3 max-w-2xl text-sm leading-6 text-muted-foreground">
              {t("reports.subtitle")}
            </p>
          </div>
        </div>

        <div className="flex shrink-0 flex-wrap items-center gap-2 lg:justify-end">
          <select
            value={episode === undefined ? "" : String(episode)}
            onChange={(event) => {
              const next = event.target.value;
              setEpisode(next ? Number(next) : undefined);
            }}
            aria-label={t("reports.episodeFilter")}
            className="h-8 min-w-32 rounded-[8px] border border-black/10 bg-black/[0.025] px-3 text-xs text-foreground outline-none focus-visible:border-black/25 dark:bg-black/[0.025]"
          >
            <option value="">{t("reports.allEpisodes")}</option>
            {episodeOptions.map((number) => (
              <option key={number} value={String(number)}>
                {t("reports.episodeOption", { number })}
              </option>
            ))}
          </select>
          <HeaderRefreshButton
            label={t("common.refresh")}
            refreshing={isRefetching}
            onRefresh={async () => {
              const result = await refetch();
              if (result.isError) {
                toast.error(t("common.error"));
                return false;
              }
              return true;
            }}
          />
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-6">
        {isLoading ? (
          <div className="mt-16 text-center text-sm text-muted-foreground">
            {t("common.loading")}
          </div>
        ) : !report || !totals ? (
          <div className="mt-16 flex flex-col items-center text-center">
            <div className="mb-4 flex size-12 items-center justify-center rounded-full border border-black/[0.06] bg-black/[0.03]">
              <Receipt className="size-5 text-muted-foreground/50" />
            </div>
            <p className="text-sm text-muted-foreground">{t("reports.noData")}</p>
          </div>
        ) : (
          <div className="space-y-5">
            <div className="flex flex-wrap items-center justify-center gap-x-10 gap-y-2 rounded-[10px] border border-black/[0.085] bg-black/[0.015] py-3">
              <StatItem
                icon={Coins}
                label={t("reports.stats.totalCost")}
                value={formatUsd(totals.usd)}
              />
              <StatItem
                icon={Clock}
                label={t("reports.stats.totalDuration")}
                value={formatDuration(totals.duration_seconds)}
              />
              <StatItem
                icon={Hash}
                label={t("reports.stats.requests")}
                value={formatCount(totals.requests)}
              />
              <StatItem
                icon={CircleSlash}
                label={t("reports.stats.unattributed")}
                value={formatCount(unattributedCount)}
              />
            </div>

            {unattributedCount > 0 ? (
              <p className="text-xs leading-5 text-muted-foreground/80">
                {t("reports.unattributedHint", { count: unattributedCount })}
              </p>
            ) : null}

            <SectionCard
              title={t("reports.byEpisode.title")}
              hint={t("reports.durationLegend")}
            >
              <div className="overflow-x-auto">
                <table className="w-full min-w-[720px] text-left text-sm">
                  <thead className={TABLE_HEAD_CLASS}>
                    <tr>
                      <th className={TABLE_HEAD_CELL_CLASS}>{t("reports.byEpisode.episode")}</th>
                      <th className={cn(TABLE_HEAD_CELL_CLASS, "text-right")}>
                        {t("reports.columns.requests")}
                      </th>
                      <th className={cn(TABLE_HEAD_CELL_CLASS, "text-right")}>
                        {t("reports.columns.quota")}
                      </th>
                      <th className={cn(TABLE_HEAD_CELL_CLASS, "text-right")}>
                        {t("reports.columns.cost")}
                      </th>
                      <th className={cn(TABLE_HEAD_CELL_CLASS, "text-right")}>
                        {t("reports.columns.duration")}
                      </th>
                      <th className={cn(TABLE_HEAD_CELL_CLASS, "text-right")}>
                        {t("reports.columns.window")}
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.by_episode.length === 0 ? (
                      <EmptyRow colSpan={6} text={t("reports.noData")} />
                    ) : (
                      report.by_episode.map((row) => (
                        <tr key={row.episode ?? "project"} className={TABLE_ROW_CLASS}>
                          <td className="px-4 py-3">
                            {row.episode === null
                              ? t("reports.projectLevel")
                              : t("reports.episodeOption", { number: row.episode })}
                          </td>
                          <UsageCells bucket={row} />
                          <td className="px-4 py-3 text-right tabular-nums text-foreground/58">
                            {formatDuration(row.duration_seconds)}
                          </td>
                          <td className="px-4 py-3 text-right text-xs tabular-nums text-foreground/45">
                            {row.started_at
                              ? `${formatTimestamp(row.started_at)} → ${formatTimestamp(row.finished_at)}`
                              : "--"}
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </SectionCard>

            <SectionCard
              title={t("reports.byModel.title")}
              hint={t("reports.quotaRatio", { ratio: formatQuota(report.quota_per_usd) })}
            >
              <div className="overflow-x-auto">
                <table className="w-full min-w-[640px] text-left text-sm">
                  <thead className={TABLE_HEAD_CLASS}>
                    <tr>
                      <th className={TABLE_HEAD_CELL_CLASS}>{t("reports.byModel.model")}</th>
                      <th className={TABLE_HEAD_CELL_CLASS}>{t("reports.columns.kind")}</th>
                      <th className={cn(TABLE_HEAD_CELL_CLASS, "text-right")}>
                        {t("reports.columns.requests")}
                      </th>
                      <th className={cn(TABLE_HEAD_CELL_CLASS, "text-right")}>
                        {t("reports.columns.quota")}
                      </th>
                      <th className={cn(TABLE_HEAD_CELL_CLASS, "text-right")}>
                        {t("reports.columns.cost")}
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.by_model.length === 0 ? (
                      <EmptyRow colSpan={5} text={t("reports.noData")} />
                    ) : (
                      report.by_model.map((row) => (
                        <tr key={`${row.kind}-${row.model}`} className={TABLE_ROW_CLASS}>
                          <td className="max-w-72 truncate px-4 py-3 font-medium">
                            {row.model || "--"}
                          </td>
                          <td className="px-4 py-3 text-xs text-foreground/45">
                            {t(`reports.kind.${row.kind}`, { defaultValue: row.kind_label })}
                          </td>
                          <UsageCells bucket={row} />
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </SectionCard>

            <SectionCard title={t("reports.byDay.title")}>
              <div className="overflow-x-auto">
                <table className="w-full min-w-[520px] text-left text-sm">
                  <thead className={TABLE_HEAD_CLASS}>
                    <tr>
                      <th className={TABLE_HEAD_CELL_CLASS}>{t("reports.byDay.date")}</th>
                      <th className={cn(TABLE_HEAD_CELL_CLASS, "text-right")}>
                        {t("reports.columns.requests")}
                      </th>
                      <th className={cn(TABLE_HEAD_CELL_CLASS, "text-right")}>
                        {t("reports.columns.quota")}
                      </th>
                      <th className={cn(TABLE_HEAD_CELL_CLASS, "text-right")}>
                        {t("reports.columns.cost")}
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.by_day.length === 0 ? (
                      <EmptyRow colSpan={4} text={t("reports.noData")} />
                    ) : (
                      report.by_day.map((row) => (
                        <tr key={row.date} className={TABLE_ROW_CLASS}>
                          <td className="px-4 py-3 tabular-nums">{row.date}</td>
                          <UsageCells bucket={row} />
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            </SectionCard>
          </div>
        )}
      </div>
    </div>
  );
}

export const Route = createFileRoute("/_app/projects/$project/reports")({
  component: ReportsPage,
});
