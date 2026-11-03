// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useEffect, useState } from "react";
import { Loader2, Plus, RefreshCw, Save, Trash2, Wand2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  useCalibrateProductionPrices,
  useProductionPriceBook,
  useSyncProductionPrices,
  useUpdateProductionPrices,
} from "@/lib/queries/production-estimate";

/**
 * 内部端「制作单价表」设置页。
 *
 * 一张 JSON（`/data/production-prices.json`）就是全部配置：手工单价、客户倍率、
 * 假设表、上游同步快照、账单标定结果。改完热生效，预估请求不触网。
 *
 * 价格优先级：**手工规则 > 账单反推 > 上游同步 > 未定价**。未定价的项在预估里被
 * 单独列出，绝不按 0 计。
 */

const KINDS = [
  { value: "image", label: "图片" },
  { value: "video", label: "视频" },
  { value: "audio", label: "音频" },
  { value: "text", label: "文本" },
];

const UNITS = [
  { value: "call", label: "每次调用" },
  { value: "item", label: "每件产出" },
  { value: "second", label: "每秒视频" },
  { value: "1k_token", label: "每千 token" },
  { value: "1k_prompt_token", label: "每千输入 token" },
  { value: "1k_completion_token", label: "每千输出 token" },
  { value: "char", label: "每字" },
  { value: "1k_char", label: "每千字" },
];

type RuleDraft = {
  id: string;
  kind: string;
  model: string;
  unit: string;
  price: string;
};

type MarkupDraft = { phone: string; markup: string };
type AssumptionDraft = { key: string; value: string; editable?: boolean };

function toRuleDrafts(rules: { id?: string; match?: Record<string, unknown>; kind?: string; model?: string; unit: string; price: number }[]): RuleDraft[] {
  return rules.map((rule, index) => ({
    id: rule.id ?? `rule-${index}`,
    kind: String(rule.match?.kind ?? rule.kind ?? ""),
    model: String(rule.match?.model ?? rule.model ?? ""),
    unit: rule.unit || "call",
    price: String(rule.price ?? ""),
  }));
}

export function ProductionPricesSettings() {
  const bookQuery = useProductionPriceBook();
  const update = useUpdateProductionPrices();
  const sync = useSyncProductionPrices();
  const calibrate = useCalibrateProductionPrices();

  const book = bookQuery.data?.data;

  const [rules, setRules] = useState<RuleDraft[]>([]);
  const [markups, setMarkups] = useState<MarkupDraft[]>([]);
  const [assumptions, setAssumptions] = useState<AssumptionDraft[]>([]);
  const [markupDefault, setMarkupDefault] = useState("");
  const [contingency, setContingency] = useState("0");
  const [project, setProject] = useState("");
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!book) return;
    setRules(toRuleDrafts(book.rules ?? []));
    setMarkups(
      Object.entries(book.client_markups ?? {}).map(([phone, value]) => ({
        phone,
        markup: String(
          typeof value === "number" ? value : (value as { markup?: number }).markup ?? 1,
        ),
      })),
    );
    setAssumptions(
      Object.entries(book.assumptions ?? {}).map(([key, value]) => ({
        key,
        value: String(value),
        editable: !(key in (book.default_assumptions ?? {})),
      })),
    );
    setMarkupDefault(
      book.markup_default === null || book.markup_default === undefined
        ? ""
        : String(book.markup_default),
    );
    setContingency(String(book.contingency_pct ?? 0));
  }, [book]);

  const save = () => {
    setMessage(null);
    const cleanRules = rules
      .filter((rule) => rule.unit && rule.price.trim() !== "")
      .map((rule, index) => {
        const match: Record<string, unknown> = {};
        if (rule.kind) match.kind = rule.kind;
        if (rule.model.trim()) match.model = rule.model.trim();
        const price = Number(rule.price);
        return {
          id: rule.id || `manual-${index + 1}`,
          match,
          unit: rule.unit,
          price: Number.isFinite(price) ? price : 0,
        };
      });
    const cleanMarkups: Record<string, { markup: number }> = {};
    for (const row of markups) {
      const phone = row.phone.trim();
      const markup = Number(row.markup);
      if (phone && Number.isFinite(markup) && markup > 0) {
        cleanMarkups[phone] = { markup };
      }
    }
    const cleanAssumptions: Record<string, number> = {};
    for (const row of assumptions) {
      const key = row.key.trim();
      const value = Number(row.value);
      if (key && Number.isFinite(value)) cleanAssumptions[key] = value;
    }
    const parsedDefault = markupDefault.trim() === "" ? null : Number(markupDefault);
    const parsedContingency = Number(contingency);

    update.mutate(
      {
        rules: cleanRules,
        client_markups: cleanMarkups,
        assumptions: cleanAssumptions,
        markup_default:
          parsedDefault !== null && Number.isFinite(parsedDefault) ? parsedDefault : null,
        contingency_pct: Number.isFinite(parsedContingency) ? parsedContingency : 0,
      },
      {
        onSuccess: () => setMessage("已保存并热生效。"),
        onError: (error) => setMessage(`保存失败：${error.message}`),
      },
    );
  };

  if (bookQuery.isLoading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" />
        正在读取单价表…
      </div>
    );
  }

  if (bookQuery.isError || !book) {
    return (
      <p className="text-sm text-muted-foreground">
        读不到单价表（仅内部端可用）。
      </p>
    );
  }

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">制作单价表</h1>
          <p className="mt-1 text-xs leading-5 text-muted-foreground">
            预估口径是上游真金白银（¥）。优先级：手工规则 &gt; 账单反推 &gt; 上游同步 &gt; 未定价。
            未定价的项会在预估里单独列出，不会按 0 计。
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            文件：<code>{book.path}</code>
            {book.synced?.fetched_at ? ` · 上次同步 ${book.synced.fetched_at}` : ""}
            {book.calibrated_at ? ` · 上次标定 ${book.calibrated_at}` : ""}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-8 rounded-full px-3 text-xs"
            disabled={sync.isPending}
            onClick={() =>
              sync.mutate(undefined, {
                onSuccess: (data) =>
                  setMessage(
                    data.errors?.length
                      ? `同步完成，但有 ${data.errors.length} 个中转站失败。`
                      : `已同步 ${data.models} 个模型的价目。`,
                  ),
                onError: (error) => setMessage(`同步失败：${error.message}`),
              })
            }
          >
            {sync.isPending ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <RefreshCw className="size-3.5" />
            )}
            同步上游价目
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-8 rounded-full px-3 text-xs"
            disabled={bookQuery.isFetching}
            onClick={() => void bookQuery.refetch()}
          >
            重新读取
          </Button>
          <Button
            type="button"
            size="sm"
            className="h-8 rounded-full px-3.5 text-xs"
            disabled={update.isPending}
            onClick={save}
          >
            {update.isPending ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Save className="size-3.5" />
            )}
            保存
          </Button>
        </div>
      </header>

      {message ? (
        <div className="rounded-lg border border-border/60 bg-muted/30 px-3 py-2 text-xs">
          {message}
        </div>
      ) : null}

      <section className="space-y-3 rounded-2xl border border-border/60 bg-card p-4">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold">手工单价规则</h2>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-8 rounded-full px-3 text-xs"
            onClick={() =>
              setRules((current) => [
                ...current,
                { id: "", kind: "image", model: "", unit: "call", price: "" },
              ])
            }
          >
            <Plus className="size-3.5" />
            加一条
          </Button>
        </div>
        <div className="space-y-2">
          {rules.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              还没有手工规则。可以先「同步上游价目」，再按实际账单修正。
            </p>
          ) : (
            rules.map((rule, index) => (
              <div key={`${rule.id}-${index}`} className="flex flex-wrap items-center gap-2">
                <select
                  value={rule.kind}
                  onChange={(event) =>
                    setRules((current) =>
                      current.map((item, i) =>
                        i === index ? { ...item, kind: event.target.value } : item,
                      ),
                    )
                  }
                  className="h-8 rounded-lg border border-input bg-transparent px-2 text-xs"
                >
                  {KINDS.map((kind) => (
                    <option key={kind.value} value={kind.value}>
                      {kind.label}
                    </option>
                  ))}
                </select>
                <Input
                  value={rule.model}
                  placeholder="模型（留空=全部）"
                  className="h-8 w-44 text-xs"
                  onChange={(event) =>
                    setRules((current) =>
                      current.map((item, i) =>
                        i === index ? { ...item, model: event.target.value } : item,
                      ),
                    )
                  }
                />
                <select
                  value={rule.unit}
                  onChange={(event) =>
                    setRules((current) =>
                      current.map((item, i) =>
                        i === index ? { ...item, unit: event.target.value } : item,
                      ),
                    )
                  }
                  className="h-8 rounded-lg border border-input bg-transparent px-2 text-xs"
                >
                  {UNITS.map((unit) => (
                    <option key={unit.value} value={unit.value}>
                      {unit.label}
                    </option>
                  ))}
                </select>
                <Input
                  value={rule.price}
                  inputMode="decimal"
                  placeholder="¥"
                  className="h-8 w-24 text-xs"
                  onChange={(event) =>
                    setRules((current) =>
                      current.map((item, i) =>
                        i === index ? { ...item, price: event.target.value } : item,
                      ),
                    )
                  }
                />
                <Button
                  type="button"
                  variant="ghost"
                  size="icon-sm"
                  aria-label="删除规则"
                  onClick={() =>
                    setRules((current) => current.filter((_, i) => i !== index))
                  }
                >
                  <Trash2 className="size-3.5" />
                </Button>
              </div>
            ))
          )}
        </div>
      </section>

      <section className="space-y-3 rounded-2xl border border-border/60 bg-card p-4">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold">客户倍率（按手机号）</h2>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-8 rounded-full px-3 text-xs"
            onClick={() => setMarkups((current) => [...current, { phone: "", markup: "2" }])}
          >
            <Plus className="size-3.5" />
            加一个客户
          </Button>
        </div>
        <p className="text-xs leading-5 text-muted-foreground">
          客户看到的售价 = 成本 × 倍率。没配倍率的客户在客户端看不到任何金额。
        </p>
        {markups.length === 0 ? (
          <p className="text-xs text-muted-foreground">还没有配置客户倍率。</p>
        ) : (
          markups.map((row, index) => (
            <div key={index} className="flex flex-wrap items-center gap-2">
              <Input
                value={row.phone}
                placeholder="11 位手机号"
                inputMode="numeric"
                className="h-8 w-40 text-xs"
                onChange={(event) =>
                  setMarkups((current) =>
                    current.map((item, i) =>
                      i === index ? { ...item, phone: event.target.value } : item,
                    ),
                  )
                }
              />
              <span className="text-xs text-muted-foreground">×</span>
              <Input
                value={row.markup}
                placeholder="倍率"
                inputMode="decimal"
                className="h-8 w-20 text-xs"
                onChange={(event) =>
                  setMarkups((current) =>
                    current.map((item, i) =>
                      i === index ? { ...item, markup: event.target.value } : item,
                    ),
                  )
                }
              />
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                aria-label="删除客户倍率"
                onClick={() => setMarkups((current) => current.filter((_, i) => i !== index))}
              >
                <Trash2 className="size-3.5" />
              </Button>
            </div>
          ))
        )}

        <div className="flex flex-wrap items-center gap-3 border-t border-border/60 pt-3">
          <label className="flex items-center gap-2 text-xs">
            兜底倍率
            <Input
              value={markupDefault}
              placeholder="留空=不给未配客户看价"
              inputMode="decimal"
              className="h-8 w-40 text-xs"
              onChange={(event) => setMarkupDefault(event.target.value)}
            />
          </label>
          <label className="flex items-center gap-2 text-xs">
            预留比例 %
            <Input
              value={contingency}
              inputMode="decimal"
              className="h-8 w-20 text-xs"
              onChange={(event) => setContingency(event.target.value)}
            />
          </label>
        </div>
      </section>

      <section className="space-y-3 rounded-2xl border border-border/60 bg-card p-4">
        <h2 className="text-sm font-semibold">假设表（粗估用）</h2>
        <p className="text-xs leading-5 text-muted-foreground">
          还没有分镜时，粗估按这些经验值把字数折算成成本。带默认值的项改了就会被记住。
        </p>
        {assumptions.map((row, index) => (
          <div key={row.key} className="flex flex-wrap items-center gap-2">
            <span className="w-52 truncate text-xs text-muted-foreground">{row.key}</span>
            <Input
              value={row.value}
              inputMode="decimal"
              className="h-8 w-28 text-xs"
              onChange={(event) =>
                setAssumptions((current) =>
                  current.map((item, i) =>
                    i === index ? { ...item, value: event.target.value } : item,
                  ),
                )
              }
            />
          </div>
        ))}
      </section>

      <section className="space-y-3 rounded-2xl border border-border/60 bg-card p-4">
        <h2 className="text-sm font-semibold">用账单标定（反推真实单价）</h2>
        <p className="text-xs leading-5 text-muted-foreground">
          拿一个已经跑过对账的项目，把真实花费摊到「种类 × 模型」上，得到 ¥/次、¥/秒、
          ¥/千 token。默认只预览，确认后再写入。
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <Input
            value={project}
            placeholder="项目 id"
            className="h-8 w-56 text-xs"
            onChange={(event) => setProject(event.target.value)}
          />
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-8 rounded-full px-3 text-xs"
            disabled={!project.trim() || calibrate.isPending}
            onClick={() =>
              calibrate.mutate(
                { project: project.trim(), apply: false },
                {
                  onSuccess: (data) =>
                    setMessage(`标定预览：得到 ${data.rules.length} 条规则。`),
                  onError: (error) => setMessage(`标定失败：${error.message}`),
                },
              )
            }
          >
            {calibrate.isPending ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Wand2 className="size-3.5" />
            )}
            预览
          </Button>
          <Button
            type="button"
            size="sm"
            className="h-8 rounded-full px-3.5 text-xs"
            disabled={!project.trim() || calibrate.isPending}
            onClick={() =>
              calibrate.mutate(
                { project: project.trim(), apply: true },
                {
                  onSuccess: (data) =>
                    setMessage(`已写入 ${data.rules.length} 条反推规则。`),
                  onError: (error) => setMessage(`标定失败：${error.message}`),
                },
              )
            }
          >
            预览并写入
          </Button>
        </div>
        {calibrate.data && calibrate.data.rules.length > 0 ? (
          <div className="space-y-1 rounded-xl border border-border/60 bg-muted/20 p-3 text-xs">
            {calibrate.data.rules.map((rule, index) => (
              <div key={index} className="flex justify-between tabular-nums">
                <span className="text-muted-foreground">
                  {rule.kind} · {rule.model ?? "全部"} · {rule.unit}
                </span>
                <span>
                  ¥{rule.price}
                  {rule.samples ? ` · ${rule.samples} 笔` : ""}
                </span>
              </div>
            ))}
          </div>
        ) : null}
      </section>

      {book.actual_rules?.length ? (
        <section className="space-y-2 rounded-2xl border border-border/60 bg-card p-4">
          <h2 className="text-sm font-semibold">已写入的账单反推规则</h2>
          {book.actual_rules.map((rule, index) => (
            <div key={index} className="flex justify-between text-xs tabular-nums">
              <span className="text-muted-foreground">
                {rule.kind} · {rule.model ?? "全部"} · {rule.unit}
              </span>
              <span>¥{rule.price}</span>
            </div>
          ))}
        </section>
      ) : null}

      {book.synced?.relays?.length ? (
        <section className="space-y-2 rounded-2xl border border-border/60 bg-card p-4">
          <h2 className="text-sm font-semibold">已同步的上游中转站</h2>
          {book.synced.relays.map((relay) => (
            <div key={relay.label} className="flex justify-between text-xs">
              <span className="text-muted-foreground">
                {relay.label}（{relay.models ?? 0} 个模型）
              </span>
              <span className="tabular-nums">汇率 {relay.usd_cny_rate}</span>
            </div>
          ))}
          {book.synced.errors?.length ? (
            <p className="text-xs text-warning">
              {book.synced.errors.length} 个中转站同步失败。
            </p>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}
