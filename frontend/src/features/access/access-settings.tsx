// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useState } from "react";
import {
  AlertTriangle,
  Check,
  Copy,
  KeyRound,
  Link2,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  useAccessConfig,
  useGenerateAccessLink,
  useSaveAccessConfig,
  useSetAccessGate,
  type AccessConfig,
  type AccessSecretState,
} from "@/lib/queries/access-config";

/**
 * 内部端「访问设置」页（顶栏盾牌图标进入）。
 *
 * 操作者在这里轮换客户密钥 / 内部口令、发客户链接、以及开关访问验证。
 * 三条硬规则：
 *
 * 1. 页面只显示掩码——明文永不从服务端回传；
 * 2. 随机口令由**浏览器**生成后显示在输入框里（服务端只存提交值），所以生成完
 *    必须当场保存，刷新后拿不回来；
 * 3. 关闭访问验证是危险动作：要重输当前内部口令确认，且密钥保留、可一键恢复。
 */

/** 32 位 URL-safe 随机串（24 字节 base64url），与后端默认口令同构。 */
export function randomAccessKey(): string {
  const bytes = new Uint8Array(24);
  const cryptoApi = globalThis.crypto;
  if (!cryptoApi || typeof cryptoApi.getRandomValues !== "function") {
    throw new Error("当前浏览器不支持安全随机数");
  }
  cryptoApi.getRandomValues(bytes);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function formatTimestamp(value: string | null): string {
  if (!value) return "未知";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString("zh-CN", { hour12: false });
}

function maskedText(state: AccessSecretState): string {
  if (!state.configured) return "未设置";
  return state.mask || "已设置";
}

function FeedbackBanner({ kind, text }: { kind: "ok" | "error"; text: string }) {
  if (kind === "error") {
    return (
      <div
        role="alert"
        className="flex items-start gap-2 rounded-[12px] border border-destructive/40 bg-destructive/10 px-3 py-2"
      >
        <AlertTriangle className="mt-0.5 size-4 shrink-0 text-destructive" aria-hidden="true" />
        <p className="text-sm text-foreground">{text}</p>
      </div>
    );
  }
  return (
    <div
      role="status"
      className="flex items-start gap-2 rounded-[12px] border border-border bg-muted px-3 py-2"
    >
      <Check className="mt-0.5 size-4 shrink-0 text-success" aria-hidden="true" />
      <p className="text-sm text-foreground">{text}</p>
    </div>
  );
}

function SecretCard({
  idPrefix,
  title,
  description,
  hint,
  state,
  draft,
  onDraftChange,
  fieldLabel,
  placeholder,
  pending,
  onSave,
  saveLabel,
}: {
  idPrefix: string;
  title: string;
  description: string;
  hint: string;
  state: AccessSecretState;
  draft: string;
  onDraftChange: (value: string) => void;
  fieldLabel: string;
  placeholder: string;
  pending: boolean;
  onSave: () => void;
  saveLabel: string;
}) {
  const locked = state.env_locked;
  const inputId = `access-${idPrefix}`;
  return (
    <Card size="sm">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <KeyRound className="size-4 text-muted-foreground" aria-hidden="true" />
          {title}
        </CardTitle>
        <CardDescription>
          {description}当前：
          <span className="ml-1 font-mono text-xs text-foreground">{maskedText(state)}</span>
          <span className="ml-2 text-xs">
            {state.source === "env" ? "（来自环境变量）" : state.source === "file" ? "（来自配置文件）" : "（尚未配置）"}
          </span>
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        <Label htmlFor={inputId} className="text-sm text-muted-foreground">
          {fieldLabel}
        </Label>
        <div className="flex flex-wrap items-center gap-2">
          <Input
            id={inputId}
            value={draft}
            onChange={(event) => onDraftChange(event.target.value)}
            placeholder={placeholder}
            disabled={locked}
            autoComplete="off"
            spellCheck={false}
            className="h-9 min-w-[16rem] flex-1 font-mono text-xs"
          />
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={locked}
            onClick={() => onDraftChange(randomAccessKey())}
          >
            <RefreshCw className="size-3.5" aria-hidden="true" />
            随机生成
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={locked || pending || draft.trim().length === 0}
            onClick={onSave}
          >
            {pending ? "保存中…" : saveLabel}
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">{hint}</p>
      </CardContent>
    </Card>
  );
}

function LinkRow({
  label,
  hint,
  url,
  copied,
  onCopy,
}: {
  label: string;
  hint: string;
  url: string;
  copied: boolean;
  onCopy: () => void;
}) {
  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-baseline gap-x-2">
        <span className="text-sm text-foreground">{label}</span>
        <span className="text-xs text-muted-foreground">{hint}</span>
      </div>
      <div className="flex items-start gap-2 rounded-[12px] border border-border bg-background p-2.5">
        <code className="min-w-0 flex-1 break-all font-mono text-xs text-foreground">{url}</code>
        <Button
          type="button"
          variant="ghost"
          size="xs"
          aria-label={`复制${label}`}
          onClick={onCopy}
        >
          {copied ? (
            <Check className="size-3" aria-hidden="true" />
          ) : (
            <Copy className="size-3" aria-hidden="true" />
          )}
          {copied ? "已复制" : "复制"}
        </Button>
      </div>
    </div>
  );
}

export function AccessSettings() {
  const configQuery = useAccessConfig();
  const saveMutation = useSaveAccessConfig();
  const gateMutation = useSetAccessGate();
  const linkMutation = useGenerateAccessLink();

  const [internalDraft, setInternalDraft] = useState("");
  const [clientDraft, setClientDraft] = useState("");
  const [phone, setPhone] = useState("");
  const [project, setProject] = useState("");
  const [studioLink, setStudioLink] = useState<string | null>(null);
  const [canvasLink, setCanvasLink] = useState<string | null>(null);
  const [copied, setCopied] = useState<"studio" | "canvas" | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [confirmKey, setConfirmKey] = useState("");
  const [feedback, setFeedback] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const config: AccessConfig | undefined = configQuery.data;

  const saveInternalKey = async () => {
    setFeedback(null);
    const result = await saveMutation.mutateAsync({ internalKey: internalDraft });
    if (!result.ok) {
      setFeedback({ kind: "error", text: result.error ?? "保存失败" });
      return;
    }
    setInternalDraft("");
    setFeedback({ kind: "ok", text: ["控制台console端口令已更新。", ...result.notices].join(" ") });
  };

  const saveClientSecret = async () => {
    setFeedback(null);
    const result = await saveMutation.mutateAsync({ clientSecret: clientDraft });
    if (!result.ok) {
      setFeedback({ kind: "error", text: result.error ?? "保存失败" });
      return;
    }
    setClientDraft("");
    setFeedback({ kind: "ok", text: ["APP端口令已更新。", ...result.notices].join(" ") });
  };

  const generateLink = async () => {
    setFeedback(null);
    setStudioLink(null);
    setCanvasLink(null);
    setCopied(null);
    const result = await linkMutation.mutateAsync({
      phone: phone.trim(),
      project: project.trim(),
    });
    if (!result.ok || !result.link) {
      setFeedback({ kind: "error", text: result.error ?? "生成链接失败" });
      return;
    }
    setStudioLink(result.link);
    setCanvasLink(result.canvasLink ?? null);
  };

  const copyLink = async (kind: "studio" | "canvas", url: string) => {
    try {
      await navigator.clipboard?.writeText(url);
      setCopied(kind);
    } catch {
      // 剪贴板可能被浏览器策略拒绝；链接本身已经显示在页面上，选中复制即可。
      setFeedback({ kind: "error", text: "自动复制失败，请手动选中链接复制。" });
    }
  };

  const changeGate = async (enabled: boolean) => {
    setFeedback(null);
    const result = await gateMutation.mutateAsync({
      enabled,
      confirmKey: enabled ? undefined : confirmKey,
    });
    if (!result.ok) {
      setFeedback({ kind: "error", text: result.error ?? "操作失败" });
      return;
    }
    setConfirming(false);
    setConfirmKey("");
    setFeedback({
      kind: "ok",
      text: [enabled ? "访问验证已开启。" : "访问验证已关闭。", ...result.notices].join(" "),
    });
  };

  if (configQuery.isPending) {
    return (
      <div className="mx-auto w-full max-w-3xl">
        <p className="text-sm text-muted-foreground">正在读取访问设置…</p>
      </div>
    );
  }

  if (configQuery.isError || !config) {
    return (
      <div className="mx-auto w-full max-w-3xl space-y-4">
        <h1 className="font-heading text-lg font-medium">访问设置</h1>
        <Card size="sm">
          <CardHeader>
            <CardTitle>无法读取访问设置</CardTitle>
            <CardDescription>
              访问验证可能未启用，或这台浏览器没有内部身份。先通过控制台console端口令进入一次，再回到本页。
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button type="button" variant="outline" size="sm" onClick={() => void configQuery.refetch()}>
              重试
            </Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  const saving = saveMutation.isPending;
  const gatePending = gateMutation.isPending;

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 pb-10">
      <header className="flex flex-wrap items-center gap-2">
        <ShieldCheck className="size-5 text-primary" aria-hidden="true" />
        <h1 className="font-heading text-lg font-medium">访问设置</h1>
        <span
          className={
            config.enabled
              ? "rounded-full bg-muted px-2 py-0.5 text-xs text-success"
              : "rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground"
          }
        >
          {config.enabled ? "访问验证已开启" : "访问验证已关闭"}
        </span>
      </header>
      <p className="text-sm text-muted-foreground">
        客户入口 <span className="font-mono text-xs text-foreground">/studio</span> 用专属链接进入；内部入口{" "}
        <span className="font-mono text-xs text-foreground">/console</span> 用控制台console端口令进入。改动立即生效，
        不需要重启服务。
      </p>

      {feedback ? <FeedbackBanner kind={feedback.kind} text={feedback.text} /> : null}

      <SecretCard
        idPrefix="internal-key"
        title="控制台console端口令"
        description="进入内部工作台（/console）的控制台console端口令，可随时更换。"
        hint="更新后其他设备上的控制台登录会失效；本机浏览器会自动续期。口令只在输入框里显示，请当场保存好。"
        state={config.internal_key}
        draft={internalDraft}
        onDraftChange={setInternalDraft}
        fieldLabel="新的控制台console端口令（留空表示不修改）"
        placeholder="留空则不修改"
        pending={saving}
        onSave={() => void saveInternalKey()}
        saveLabel="保存控制台console端口令"
      />

      <SecretCard
        idPrefix="client-secret"
        title="APP端口令"
        description="APP端口令是客户链接校验码的签名密钥（md5(手机号+APP端口令)）。"
        hint="更新后此前发出的所有客户链接立即失效，需要重新发送链接。"
        state={config.client_secret}
        draft={clientDraft}
        onDraftChange={setClientDraft}
        fieldLabel="新的APP端口令（留空表示不修改）"
        placeholder="留空则不修改"
        pending={saving}
        onSave={() => void saveClientSecret()}
        saveLabel="保存APP端口令"
      />

      <Card size="sm">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Link2 className="size-4 text-muted-foreground" aria-hidden="true" />
            发客户链接
          </CardTitle>
          <CardDescription>
            输入客户手机号，生成工作台与捏画两条专属链接后发送给对方。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="access-link-phone" className="text-sm text-muted-foreground">
              客户手机号（11 位）
            </Label>
            <div className="flex flex-wrap items-center gap-2">
              <Input
                id="access-link-phone"
                inputMode="numeric"
                value={phone}
                onChange={(event) => {
                  setPhone(event.target.value);
                  setStudioLink(null);
                  setCanvasLink(null);
                }}
                placeholder="13800000000"
                className="h-9 min-w-[12rem] flex-1"
              />
              <Input
                id="access-link-project"
                value={project}
                onChange={(event) => {
                  setProject(event.target.value);
                  setCanvasLink(null);
                }}
                placeholder="项目 ID（可选，捏画链接直达）"
                aria-label="项目 ID（可选）"
                autoComplete="off"
                spellCheck={false}
                className="h-9 min-w-[12rem] flex-1 font-mono text-xs"
              />
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={linkMutation.isPending || phone.trim().length === 0}
                onClick={() => void generateLink()}
              >
                {linkMutation.isPending ? "生成中…" : "生成链接"}
              </Button>
            </div>
          </div>
          {studioLink ? (
            <div className="space-y-3">
              <LinkRow
                label="工作台链接"
                hint="一键成片 / 进度 / 每集费用（/studio）"
                url={studioLink}
                copied={copied === "studio"}
                onCopy={() => void copyLink("studio", studioLink)}
              />
              {canvasLink ? (
                <LinkRow
                  label="捏画链接"
                  hint={
                    project.trim()
                      ? `直达该项目的捏画（/canvas/${project.trim()}）`
                      : "打开捏画项目列表（/canvas）"
                  }
                  url={canvasLink}
                  copied={copied === "canvas"}
                  onCopy={() => void copyLink("canvas", canvasLink)}
                />
              ) : null}
            </div>
          ) : null}
          <p className="text-xs text-muted-foreground">
            两条链接是同一个客户、同一个校验码，只有入口路径不同（/studio 与 /canvas），
            且都不绑定项目 —— 换入口或换项目，把路径改掉即可。链接永久有效、可转发；
            客户浏览器记住 7 天。开启访问验证前需要先设置APP端口令。
          </p>
        </CardContent>
      </Card>

      <Card size="sm">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <AlertTriangle
              className={config.enabled ? "size-4 text-destructive" : "size-4 text-muted-foreground"}
              aria-hidden="true"
            />
            访问验证开关
          </CardTitle>
          <CardDescription>
            {config.enabled
              ? "关闭后任何人都能直接看到全部项目（回到单用户行为）；控制台console端口令与APP端口令会保留，可随时重新开启。"
              : "当前已关闭：任何人都能直接看到全部项目。重新开启后会沿用现有的控制台console端口令与APP端口令。"}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {config.enabled ? (
            confirming ? (
              <div className="space-y-2 rounded-[12px] border border-destructive/40 bg-destructive/10 p-3">
                <Label htmlFor="access-confirm-key" className="text-sm text-foreground">
                  输入当前控制台console端口令以确认关闭
                </Label>
                <div className="flex flex-wrap items-center gap-2">
                  <Input
                    id="access-confirm-key"
                    type="password"
                    autoComplete="off"
                    value={confirmKey}
                    onChange={(event) => setConfirmKey(event.target.value)}
                    className="h-9 min-w-[14rem] flex-1 font-mono text-xs"
                  />
                  <Button
                    type="button"
                    variant="destructive"
                    size="sm"
                    disabled={gatePending || confirmKey.trim().length === 0}
                    onClick={() => void changeGate(false)}
                  >
                    {gatePending ? "处理中…" : "确认关闭"}
                  </Button>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      setConfirming(false);
                      setConfirmKey("");
                    }}
                  >
                    取消
                  </Button>
                </div>
              </div>
            ) : (
              <Button
                type="button"
                variant="destructive"
                size="sm"
                onClick={() => setConfirming(true)}
              >
                关闭访问验证…
              </Button>
            )
          ) : (
            <div className="space-y-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={gatePending || !config.client_secret.configured}
                onClick={() => void changeGate(true)}
              >
                {gatePending ? "处理中…" : "重新开启访问验证"}
              </Button>
              {config.client_secret.configured ? null : (
                <p className="text-xs text-muted-foreground">请先在上方设置APP端口令，再开启访问验证。</p>
              )}
            </div>
          )}
          <p className="text-xs text-muted-foreground">
            配置文件：<span className="font-mono text-xs">{config.file.path}</span>
            {config.file.exists
              ? `（最近更新 ${formatTimestamp(config.file.updated_at)}）`
              : "（尚未生成，保存时会自动创建）"}
            。宿主机保留只读副本，可直接读取当前值。
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
