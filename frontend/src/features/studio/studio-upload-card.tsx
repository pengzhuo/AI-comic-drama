// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { FileText, Loader2, Upload } from "lucide-react";

import {
  createStudioProject,
  DEFAULT_STUDIO_CHOICES,
  deriveProjectName,
  saveStudioProjectChoices,
  startStudioIngest,
  uploadStudioScript,
  type CreatedProject,
  type StudioChoices,
} from "@/features/studio/studio-api";
import { IngestFormatBlockedError, type FormatCheck } from "@/lib/queries/ingest";
import { BUILTIN_VISUAL_STYLES } from "@/lib/visual-styles";
import { useProductionEstimate } from "@/lib/queries/production-estimate";
import { formatCny } from "@/lib/cost-format";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { SpineTemplate } from "@/types/project";

/**
 * 上传剧本 →（自动建项目）→（自动导入）。
 *
 * 客户端只做一件事：选文件、写剧名、点开始。项目名从剧名派生（后端允许中文，
 * 重名自动加后缀），导入链一入队就跳到工作台页面看进度。
 */

type Phase = "idle" | "creating" | "uploading" | "queuing";

const PHASE_TEXT: Record<Phase, string> = {
  idle: "",
  creating: "正在创建项目…",
  uploading: "正在上传剧本…",
  queuing: "正在排队导入…",
};

// 选项源与内部捏料页同源：值写进 API，label 按 id 查 i18n（内置 preset 的后端
// label 是中文单语，直接渲染会让英文界面出现中文）。顺序即下拉框展示顺序。
const SPINE_TEMPLATE_OPTIONS: { value: SpineTemplate; labelKey: string }[] = [
  { value: "drama", labelKey: "ingest.projectTypes.drama" },
  { value: "narrated", labelKey: "ingest.projectTypes.narrated" },
];

const VISUAL_STYLE_OPTIONS = BUILTIN_VISUAL_STYLES;

const ETHNICITY_OPTIONS: { value: string; labelKey: string }[] = [
  { value: "Chinese", labelKey: "ingest.ethnicities.chinese" },
  { value: "Japanese", labelKey: "ingest.ethnicities.japanese" },
  { value: "Korean", labelKey: "ingest.ethnicities.korean" },
  { value: "Western", labelKey: "ingest.ethnicities.western" },
  { value: "Mixed", labelKey: "ingest.ethnicities.mixed" },
];

/** 工作室选项下拉：原生 select，样式与上方输入框保持一致。 */
function StudioSelect({
  label,
  value,
  options,
  disabled,
  onChange,
}: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  disabled?: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-muted-foreground">
        {label}
      </span>
      <select
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
        className="h-9 w-full rounded-lg border border-border bg-background px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/40 disabled:opacity-60"
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

/**
 * 上传完成后的「粗估制作成本」确认块。
 *
 * 上传与入队都已经做完了——脚本不会因为用户在这里犹豫而丢掉。这一屏只是把
 * 「这部剧大概要花多少钱」摆出来，让用户在最开始就有一个数；之后分镜写完还有一次
 * 逐项精估（`cost_estimate` 闸门）。
 */
function CoarseEstimateConfirm({
  project,
  onStart,
  onLater,
}: {
  project: CreatedProject;
  onStart: () => void;
  onLater: () => void;
}) {
  const estimateQuery = useProductionEstimate(project.id, { stage: "coarse" });
  const estimate = estimateQuery.data?.data;
  const isClient = estimate?.view === "client";
  const total = estimate?.cost?.total_cny ?? null;
  const price = estimate?.price?.cny ?? null;

  return (
    <div className="rounded-xl border border-primary/30 bg-muted/20 p-4">
      <div className="text-sm font-semibold">
        {isClient ? "这部大概要花多少" : "粗估制作成本"}
      </div>
      <p className="mt-1 text-xs leading-5 text-muted-foreground">
        剧本已经上传并排队导入，还没开跑制作。下面是按字数与经验值推的粗估值；分镜写完后
        还会再给一次逐项精估，届时请你确认。
      </p>

      <div className="mt-3 flex items-baseline justify-between gap-3 rounded-lg border border-border/60 bg-background px-3 py-2.5">
        <span className="text-xs text-muted-foreground">粗估合计</span>
        {estimateQuery.isLoading ? (
          <Loader2 className="size-4 animate-spin text-muted-foreground" />
        ) : (
          <span className="text-base font-semibold tabular-nums">
            {isClient
              ? price === null
                ? "由客服提供"
                : formatCny(price)
              : total === null
                ? "暂无单价"
                : formatCny(total)}
          </span>
        )}
      </div>
      {!isClient && estimate && estimate.unpriced_count > 0 ? (
        <p className="mt-2 text-xs leading-5 text-warning">
          有 {estimate.unpriced_count} 项还没配单价，合计只算了配好价的项。
        </p>
      ) : null}

      <div className="mt-3 flex items-center justify-end gap-2">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-8 rounded-full px-3 text-xs"
          onClick={onLater}
        >
          先看看，稍后再开始
        </Button>
        <Button type="button" size="sm" className="h-8 rounded-full px-3.5 text-xs" onClick={onStart}>
          开始制作
        </Button>
      </div>
    </div>
  );
}

export function StudioUploadCard({
  onCreated,
}: {
  onCreated: (project: CreatedProject, options?: { autostart?: boolean }) => void;
}) {
  const { t } = useTranslation();
  const [title, setTitle] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [choices, setChoices] = useState<StudioChoices>(DEFAULT_STUDIO_CHOICES);
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [advisory, setAdvisory] = useState<FormatCheck | null>(null);
  const [issues, setIssues] = useState<string[]>([]);
  const [pendingProject, setPendingProject] = useState<CreatedProject | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const busy = phase !== "idle";
  const effectiveTitle = title.trim() || (file ? deriveProjectName(file.name) : "");

  async function handleSubmit() {
    if (!file || busy) return;
    setError(null);
    setIssues([]);
    setAdvisory(null);
    try {
      setPhase("creating");
      const project = await createStudioProject(effectiveTitle || file.name);

      setPhase("uploading");
      const uploaded = await uploadStudioScript(
        project.id,
        file,
        choices.spine_template,
      );

      // 先把风格/人群落到项目配置：一键成片的自动开跑读的是项目配置，不能只放在
      // 本次请求里。项目类型交给 ingest/start 写（它带 rebuild 守卫）。
      setPhase("queuing");
      await saveStudioProjectChoices(project.id, {
        visual_style: choices.visual_style,
        narration_style: choices.narration_style,
        ethnicity: choices.ethnicity,
      });
      const started = await startStudioIngest(
        project.id,
        uploaded.filename,
        choices.spine_template,
      );
      // 精品剧缺场景头不再拦住导入：把体检结果作为提示展示。
      setAdvisory(started.format_check ?? null);

      // 剧本已经上传并入队：先给一次粗估，用户点头才跳去自动开跑。
      setPendingProject(project);
    } catch (caught) {
      if (caught instanceof IngestFormatBlockedError) {
        setError(caught.message);
        setIssues(
          (caught.formatCheck?.issues ?? [])
            .map((issue) => [issue.message, issue.fix].filter(Boolean).join(" → "))
            .filter(Boolean)
            .slice(0, 6),
        );
      } else {
        setError(caught instanceof Error ? caught.message : String(caught));
      }
    } finally {
      setPhase("idle");
    }
  }

  return (
    <section className="rounded-2xl border border-border/60 bg-card p-5 shadow-sm">
      <h2 className="text-base font-semibold">上传剧本，一键成片</h2>
      <p className="mt-1 text-sm text-muted-foreground">
        支持 .txt / .md / .docx。上传后系统会自动导入、拆分剧集并开始制作，每完成一集都能看到成片与费用。
      </p>

      <div className="mt-4 space-y-3">
        {pendingProject ? (
          <CoarseEstimateConfirm
            project={pendingProject}
            onStart={() => onCreated(pendingProject, { autostart: true })}
            onLater={() => onCreated(pendingProject, { autostart: false })}
          />
        ) : (
          <>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-muted-foreground">
                剧名（可留空，默认取文件名）
              </span>
              <input
                type="text"
                value={title}
                maxLength={64}
                disabled={busy}
                placeholder="例如：我的剧"
                onChange={(event) => setTitle(event.target.value)}
                className="h-9 w-full rounded-lg border border-border bg-background px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/40 disabled:opacity-60"
              />
            </label>

            <button
              type="button"
              disabled={busy}
              onClick={() => inputRef.current?.click()}
              className={cn(
                "flex w-full items-center gap-3 rounded-xl border border-dashed border-border px-4 py-5 text-left transition-colors",
                busy ? "opacity-60" : "hover:border-primary/60 hover:bg-muted/40",
              )}
            >
              {file ? (
                <FileText className="size-5 shrink-0 text-primary" />
              ) : (
                <Upload className="size-5 shrink-0 text-muted-foreground" />
              )}
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium">
                  {file ? file.name : "选择剧本文件"}
                </span>
                <span className="block text-xs text-muted-foreground">
                  {file
                    ? `${Math.max(1, Math.round(file.size / 1024))} KB`
                    : "点击选择 .txt / .md / .docx"}
                </span>
              </span>
            </button>

            {/* 导入剧本下方的三个选择：项目类型 / 视觉风格 / 默认未指定人群。 */}
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
              <StudioSelect
                label={t("ingest.projectType")}
                value={choices.spine_template}
                disabled={busy}
                onChange={(value) =>
                  setChoices((prev) => ({
                    ...prev,
                    spine_template: value as SpineTemplate,
                  }))
                }
                options={SPINE_TEMPLATE_OPTIONS.map((option) => ({
                  value: option.value,
                  label: t(option.labelKey),
                }))}
              />
              <StudioSelect
                label={t("ingest.visualStyle")}
                value={choices.visual_style}
                disabled={busy}
                onChange={(value) =>
                  setChoices((prev) => ({ ...prev, visual_style: value }))
                }
                options={VISUAL_STYLE_OPTIONS.map((option) => ({
                  value: option.value,
                  label: t(option.labelKey),
                }))}
              />
              <StudioSelect
                label={t("ingest.ethnicity")}
                value={choices.ethnicity}
                disabled={busy}
                onChange={(value) =>
                  setChoices((prev) => ({ ...prev, ethnicity: value }))
                }
                options={ETHNICITY_OPTIONS.map((option) => ({
                  value: option.value,
                  label: t(option.labelKey),
                }))}
              />
            </div>
          </>
        )}
        <input
          ref={inputRef}
          type="file"
          accept=".txt,.md,.docx"
          className="hidden"
          onChange={(event) => {
            const next = event.target.files?.[0] ?? null;
            setFile(next);
            setError(null);
            setAdvisory(null);
            // 允许用户重新选同一个文件。
            event.target.value = "";
          }}
        />

        {pendingProject ? null : (
          <Button
            type="button"
            size="lg"
            className="w-full"
            disabled={!file || busy}
            onClick={() => void handleSubmit()}
          >
            {busy ? <Loader2 className="size-4 animate-spin" /> : null}
            {busy ? PHASE_TEXT[phase] : "开始制作"}
          </Button>
        )}

        {error ? (
          <div className="rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            <div>{error}</div>
            {issues.length > 0 ? (
              <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs">
                {issues.map((issue, index) => (
                  <li key={`${index}-${issue}`}>{issue}</li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}

        {advisory ? (
          <div className="rounded-lg border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-warning">
            <div>
              剧本格式还能更规范，已按可处理的方式继续导入；系统会在剧本处理阶段整理成标准分场。
            </div>
            {advisory.summary ? (
              <div className="mt-1 text-xs opacity-90">{advisory.summary}</div>
            ) : null}
            {(advisory.issues ?? []).length > 0 ? (
              <ul className="mt-1 list-disc space-y-0.5 pl-5 text-xs opacity-90">
                {(advisory.issues ?? [])
                  .map((issue) =>
                    [issue.message, issue.fix].filter(Boolean).join(" → "),
                  )
                  .filter(Boolean)
                  .slice(0, 6)
                  .map((issue, index) => (
                    <li key={`${index}-${issue}`}>{issue}</li>
                  ))}
              </ul>
            ) : null}
          </div>
        ) : null}
      </div>
    </section>
  );
}
