// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useState } from "react";
import { Link, createFileRoute } from "@tanstack/react-router";
import { ArrowLeft, Check, Copy, Download, Loader2, Printer } from "lucide-react";

import { Button } from "@/components/ui/button";
import { formatCny } from "@/lib/cost-format";
import {
  fetchProductionQuote,
  useProductionEstimate,
  type ProductionQuoteFormat,
} from "@/lib/queries/production-estimate";
import { useEpisodes } from "@/lib/queries/episodes";
import { useAllProjectSummaries } from "@/lib/queries/projects";

/**
 * 可打印的《制作报价单》（A4）。
 *
 * 之所以挂在 `/studio/quote/$project` 而不是 `/studio/$project/quote`：`$project.tsx`
 * 是工作台页面本身（不是只有 `<Outlet/>` 的布局），做二级嵌套会把整页内容一起渲染。
 *
 * 内容与服务端渲染的 Markdown/CSV/纯文本同源（同一个 `production-estimate` 口径），
 * 只是排版成纸面：客户版只有售价合计，成本与未定价项由后端抹掉后根本不会到达这里。
 * 打印时隐藏工作台外壳（顶栏/页脚）与本页工具栏，只留 A4 纸面。
 */
export const Route = createFileRoute("/studio/quote/$project")({
  validateSearch: (search: Record<string, unknown>): { episode?: number } => {
    // 路由默认 `parseSearch` 会 JSON.parse，`?episode=1` 到这里是 number 1。
    const raw = search.episode;
    const episode = typeof raw === "number" ? raw : Number(raw);
    return Number.isFinite(episode) && episode > 0 ? { episode } : {};
  },
  component: StudioQuotePage,
});

const PRINT_CSS = `
@media print {
  @page { size: A4; margin: 14mm; }
  header, footer, .quote-toolbar, .quote-back { display: none !important; }
  html, body { overflow: visible !important; background: #fff !important; }
  main { overflow: visible !important; height: auto !important; }
  .quote-sheet { box-shadow: none !important; border: 0 !important; margin: 0 !important; }
  .quote-sheet, .quote-sheet * { color: #000 !important; }
}
`;

function StudioQuotePage() {
  const { project } = Route.useParams();
  const { episode } = Route.useSearch();

  const estimateQuery = useProductionEstimate(project, { episode, stage: "refined" });
  const episodesQuery = useEpisodes(project);
  const summariesQuery = useAllProjectSummaries();

  const [busy, setBusy] = useState<ProductionQuoteFormat | "copy" | null>(null);
  const [copied, setCopied] = useState(false);

  const estimate = estimateQuery.data?.data;
  const isClient = estimate?.view === "client";
  const projectName =
    summariesQuery.data?.find((item) => item.id === project)?.name ?? project;
  const episodeTitle = episodesQuery.data?.data?.find(
    (item) => item.number === episode,
  )?.title;
  const price = estimate?.price?.cny ?? null;
  const total = estimate?.cost?.total_cny ?? null;
  const contingency = estimate?.cost?.contingency_cny ?? null;
  const generatedAt = estimate?.generated_at
    ? new Date(estimate.generated_at).toLocaleString("zh-CN")
    : new Date().toLocaleString("zh-CN");

  const fetchQuote = async (format: ProductionQuoteFormat) => {
    setBusy(format);
    try {
      return await fetchProductionQuote(project, {
        episode,
        stage: estimate?.stage ?? "auto",
        format,
        title: projectName,
      });
    } finally {
      setBusy(null);
    }
  };

  const download = async (format: ProductionQuoteFormat) => {
    const quote = await fetchQuote(format);
    if (!quote) return;
    const blob = new Blob([quote.content], {
      type: format === "csv" ? "text/csv;charset=utf-8" : "text/plain;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = quote.filename;
    anchor.click();
    URL.revokeObjectURL(url);
  };

  const copyText = async () => {
    setBusy("copy");
    try {
      const quote = await fetchProductionQuote(project, {
        episode,
        stage: estimate?.stage ?? "auto",
        format: "txt",
        title: projectName,
      });
      await navigator.clipboard.writeText(quote.content);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="space-y-4">
      <style>{PRINT_CSS}</style>

      <div className="quote-back flex items-center gap-3">
        <Link
          to="/studio/$project"
          params={{ project }}
          className="inline-flex items-center gap-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
        >
          <ArrowLeft className="size-3.5" />
          返回制作台
        </Link>
      </div>

      {estimateQuery.isLoading ? (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          正在生成报价单…
        </div>
      ) : null}

      <article className="quote-sheet mx-auto w-full max-w-[210mm] rounded-2xl border border-border/60 bg-white px-8 py-8 text-black shadow-sm">
        <header className="flex items-start justify-between gap-4 border-b border-black/15 pb-4">
          <div className="min-w-0">
            <h1 className="text-xl font-semibold tracking-tight">
              {isClient ? "制作报价单" : "制作成本报价单"}
            </h1>
            <p className="mt-1 text-xs text-black/60">
              {estimate?.stage === "coarse" ? "粗估（按字数与经验值推算）" : "精估（按分镜逐项）"}
              {" · "}
              生成于 {generatedAt}
            </p>
          </div>
          <div className="shrink-0 text-right text-xs leading-5 text-black/70">
            <div className="text-sm font-medium text-black">{projectName}</div>
            {episode ? (
              <div>
                第 {episode} 集
                {episodeTitle ? `《${episodeTitle}》` : ""}
              </div>
            ) : (
              <div>整部</div>
            )}
          </div>
        </header>

        {!estimate ? (
          <p className="mt-4 text-sm text-black/70">暂时没有可用的报价数据。</p>
        ) : (
          <>
            <table className="mt-4 w-full border-collapse text-xs">
              <thead>
                <tr className="border-b border-black/15 text-left text-black/60">
                  <th className="py-2 font-medium">项目</th>
                  <th className="py-2 text-right font-medium">数量</th>
                  <th className="py-2 text-right font-medium">单位</th>
                  {isClient ? null : (
                    <>
                      <th className="py-2 text-right font-medium">单价</th>
                      <th className="py-2 text-right font-medium">金额</th>
                    </>
                  )}
                </tr>
              </thead>
              <tbody>
                {estimate.items.map((item, index) => (
                  <tr
                    key={`${item.kind}-${item.model ?? ""}-${index}`}
                    className="border-b border-black/10"
                  >
                    <td className="py-1.5 pr-2">
                      {item.label}
                      {item.model ? (
                        <span className="ml-1 text-black/50">（{item.model}）</span>
                      ) : null}
                    </td>
                    <td className="py-1.5 text-right tabular-nums">{item.quantity}</td>
                    <td className="py-1.5 text-right text-black/60">{item.unit}</td>
                    {isClient ? null : (
                      <>
                        <td className="py-1.5 text-right tabular-nums">
                          {item.unit_price === null ? "—" : formatCny(item.unit_price)}
                        </td>
                        <td className="py-1.5 text-right tabular-nums">
                          {item.cny === null ? "未定价" : formatCny(item.cny)}
                        </td>
                      </>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>

            <div className="mt-4 ml-auto w-full max-w-xs space-y-1 text-xs">
              {!isClient && total !== null ? (
                <>
                  <div className="flex justify-between">
                    <span className="text-black/60">小计</span>
                    <span className="tabular-nums">
                      {formatCny(total - (contingency ?? 0))}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-black/60">预留（{estimate.contingency_pct}%）</span>
                    <span className="tabular-nums">
                      {contingency === null ? "—" : formatCny(contingency)}
                    </span>
                  </div>
                </>
              ) : null}
              <div className="flex items-baseline justify-between border-t border-black/20 pt-2">
                <span className="text-sm font-medium">
                  {isClient ? "报价合计" : "成本合计"}
                </span>
                <span className="text-lg font-semibold tabular-nums">
                  {isClient
                    ? price === null
                      ? "由客服提供"
                      : formatCny(price)
                    : total === null
                      ? "—"
                      : formatCny(total)}
                </span>
              </div>
            </div>

            {!isClient && estimate.unpriced_count > 0 ? (
              <p className="mt-4 rounded border border-black/15 px-3 py-2 text-xs leading-5">
                有 {estimate.unpriced_count} 项尚未配置单价，未计入合计——这不是 0 元，而是
                「暂时不知道」。
              </p>
            ) : null}

            <p className="mt-4 border-t border-black/10 pt-3 text-xs leading-5 text-black/60">
              本单为按当前分镜与配置的估算，实际费用会随重跑、返工与模型选择变化；未列出的项以
              实际发生为准。
            </p>
          </>
        )}
      </article>

      <div className="quote-toolbar flex flex-wrap items-center justify-end gap-2 print:hidden">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-8 rounded-full px-3 text-xs"
          disabled={busy !== null}
          onClick={() => void copyText()}
        >
          {busy === "copy" ? (
            <Loader2 className="size-3.5 animate-spin" />
          ) : copied ? (
            <Check className="size-3.5" />
          ) : (
            <Copy className="size-3.5" />
          )}
          {copied ? "已复制" : "复制文本"}
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-8 rounded-full px-3 text-xs"
          disabled={busy !== null}
          onClick={() => void download("csv")}
        >
          {busy === "csv" ? (
            <Loader2 className="size-3.5 animate-spin" />
          ) : (
            <Download className="size-3.5" />
          )}
          下载 CSV
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-8 rounded-full px-3 text-xs"
          disabled={busy !== null}
          onClick={() => void download("md")}
        >
          {busy === "md" ? (
            <Loader2 className="size-3.5 animate-spin" />
          ) : (
            <Download className="size-3.5" />
          )}
          下载 Markdown
        </Button>
        <Button
          type="button"
          size="sm"
          className="h-8 rounded-full px-3.5 text-xs"
          onClick={() => window.print()}
        >
          <Printer className="size-3.5" />
          打印 / 存为 PDF
        </Button>
      </div>
    </div>
  );
}
