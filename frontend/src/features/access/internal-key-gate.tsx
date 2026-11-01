// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useState } from "react";

import { activateInternalSession, type AccessActionResult } from "@/lib/access-session";

/**
 * 内部端（`/console` 及内部项目页）的口令门。
 *
 * 访问验证启用后，只持有客户 cookie（或完全没有 cookie）的浏览器看不到任何项目，
 * 所以在内部壳里先把内部会话换出来，再重载页面让路由重新判身份。
 *
 * 依赖以 props 注入，便于测试（默认走真实接口 + 整页重载）。
 */
export interface InternalKeyGateProps {
  activate?: (key: string) => Promise<AccessActionResult>;
  reload?: () => void;
  /** 客户身份误入内部端时的说明文案；为空表示当前浏览器没有任何身份。 */
  hint?: string;
}

export function InternalKeyGate({
  activate = activateInternalSession,
  reload = () => window.location.reload(),
  hint,
}: InternalKeyGateProps) {
  const [key, setKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const value = key.trim();
    if (!value || pending) return;
    setPending(true);
    setError(null);
    const result = await activate(value);
    if (!result.ok) {
      setError(result.error ?? "内部访问口令不正确");
      setPending(false);
      return;
    }
    reload();
  };

  return (
    <div className="flex min-h-dvh items-center justify-center bg-background px-4 text-foreground">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-2xl border border-border/60 bg-card p-6"
      >
        <h1 className="text-lg font-semibold tracking-tight">内部访问</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          内部工作台需要内部访问口令。验证成功后这台浏览器会记住 30 天。
        </p>
        {hint ? (
          <p className="mt-2 text-xs text-muted-foreground" data-testid="internal-key-gate-hint">
            {hint}
          </p>
        ) : null}
        <label
          htmlFor="internal-access-key"
          className="mt-4 block text-xs font-medium text-foreground"
        >
          内部访问口令
        </label>
        <input
          id="internal-access-key"
          name="internal-access-key"
          type="password"
          autoComplete="current-password"
          autoFocus
          value={key}
          onChange={(event) => setKey(event.target.value)}
          className="mt-1 w-full rounded-xl border border-input bg-background px-3 py-2 text-sm text-foreground outline-none transition-shadow focus-visible:ring-2 focus-visible:ring-ring"
        />
        {error ? (
          <p role="alert" className="mt-3 text-sm text-destructive">
            {error}
          </p>
        ) : null}
        <button
          type="submit"
          disabled={pending || key.trim().length === 0}
          className="mt-4 w-full rounded-full bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-opacity duration-150 disabled:opacity-40"
        >
          {pending ? "验证中…" : "进入"}
        </button>
      </form>
    </div>
  );
}
