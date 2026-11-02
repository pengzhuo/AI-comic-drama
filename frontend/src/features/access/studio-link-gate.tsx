// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
/**
 * 客户入口（`/studio`）无有效会话时的门禁提示页。
 *
 * 纯展示组件：不碰路由、不发请求，方便单独测试。
 */
export interface StudioLinkGateProps {
  /** 一次性提示，例如「访问链接无效」。 */
  notice?: string | null;
  /** 实例署名文本；未设置（上游构建）时不渲染页脚。 */
  attribution?: string | null;
  /** 退出当前会话；不传则不渲染退出按钮。 */
  onLogout?: () => void;
}

export function StudioLinkGate({ notice, attribution, onLogout }: StudioLinkGateProps) {
  return (
    <div className="flex h-dvh flex-col overflow-hidden bg-background text-foreground">
      <main
        id="main-content"
        tabIndex={-1}
        className="ui-scrollbar flex min-h-0 flex-1 items-center justify-center overflow-y-auto px-4 py-10 focus:outline-none"
      >
        <div className="w-full max-w-md rounded-2xl border border-border/60 bg-card p-6">
          <h1 className="text-lg font-semibold tracking-tight">需要专属访问链接</h1>
          <p className="mt-2 text-sm text-muted-foreground">
            本工作台只对持有专属访问链接的用户开放。请用我们发给你的链接打开（链接里带着手机号与校验码），
            打开成功后这台浏览器会记住你 7 天，期间可以直接访问。
          </p>
          {notice ? (
            <p
              role="alert"
              className="mt-4 rounded-xl border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
            >
              {notice}
            </p>
          ) : null}
          {onLogout ? (
            <button
              type="button"
              className="mt-4 text-xs text-muted-foreground transition-colors hover:text-foreground"
              onClick={onLogout}
            >
              退出当前会话
            </button>
          ) : null}
        </div>
      </main>
      {attribution ? (
        <footer className="shrink-0 border-t border-border/60">
          <div className="mx-auto w-full max-w-5xl px-4 py-3 text-right text-xs text-muted-foreground">
            {attribution}
          </div>
        </footer>
      ) : null}
    </div>
  );
}
