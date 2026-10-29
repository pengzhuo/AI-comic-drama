// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useRef, useState } from "react";
import { FileText, Loader2, Upload } from "lucide-react";

import {
  createStudioProject,
  deriveProjectName,
  startStudioIngest,
  uploadStudioScript,
  STUDIO_RUN_CONFIG,
  type CreatedProject,
} from "@/features/studio/studio-api";
import { IngestFormatBlockedError } from "@/lib/queries/ingest";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

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

export function StudioUploadCard({
  onCreated,
}: {
  onCreated: (project: CreatedProject) => void;
}) {
  const [title, setTitle] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [issues, setIssues] = useState<string[]>([]);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const busy = phase !== "idle";
  const effectiveTitle = title.trim() || (file ? deriveProjectName(file.name) : "");

  async function handleSubmit() {
    if (!file || busy) return;
    setError(null);
    setIssues([]);
    try {
      setPhase("creating");
      const project = await createStudioProject(effectiveTitle || file.name);

      setPhase("uploading");
      const uploaded = await uploadStudioScript(
        project.id,
        file,
        STUDIO_RUN_CONFIG.spine_template,
      );

      setPhase("queuing");
      await startStudioIngest(
        project.id,
        uploaded.filename,
        STUDIO_RUN_CONFIG.spine_template,
      );

      onCreated(project);
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
        <input
          ref={inputRef}
          type="file"
          accept=".txt,.md,.docx"
          className="hidden"
          onChange={(event) => {
            const next = event.target.files?.[0] ?? null;
            setFile(next);
            setError(null);
            // 允许用户重新选同一个文件。
            event.target.value = "";
          }}
        />

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
      </div>
    </section>
  );
}
