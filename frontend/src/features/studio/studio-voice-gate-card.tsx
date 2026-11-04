// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useState } from "react";
import { Loader2, Play, Volume2 } from "lucide-react";
import { toast } from "sonner";

import { CharacterVoicePanel } from "@/components/assets/character-voice-panel";
import { Button } from "@/components/ui/button";
import { useContinueDirectorAutoRun } from "@/lib/queries/director-auto";
import { useCharacters } from "@/lib/queries/characters";
import type { DirectorVoicePolicy } from "@/lib/queries/director-auto";
import { cn } from "@/lib/utils";

/**
 * 客户端工作室的「配音声线」闸门卡。
 *
 * 服务端在下一步是 `tts`、声线前置缺失且**没有选定声线策略**时把一键成片停在这里
 * （`VOICE_POLICY_REQUIRED_ERROR`，不带头部 tag）。客户二选一：
 *
 * - **系统声线**：直接用平台预置音色，零配置继续；
 * - **自定义声线**：为本集需要的角色上传/录制声音样本（复用角色编辑器的
 *   `CharacterVoicePanel`）；没传的角色照旧由系统声线兜底，**不硬卡**。
 *
 * 选完调 `/chat/director-auto/continue` 带上 `voice_policy`，服务端据此继续。
 */
const OPTIONS: { value: DirectorVoicePolicy; title: string; hint: string }[] = [
  {
    value: "system",
    title: "系统声线",
    hint: "直接使用平台预置音色，无需额外配置。",
  },
  {
    value: "custom",
    title: "自定义声线",
    hint: "为角色上传或录制声音样本；没提供声音的角色由系统声线兜底。",
  },
];

export function StudioVoiceGateCard({
  project,
  episode,
}: {
  project: string;
  episode?: number;
}) {
  const continueRun = useContinueDirectorAutoRun(project);
  const [policy, setPolicy] = useState<DirectorVoicePolicy>("system");
  const charactersQuery = useCharacters(project, policy === "custom");
  const characters = charactersQuery.data?.data ?? [];

  const submit = () =>
    continueRun.mutate(
      { voicePolicy: policy },
      {
        onError: (error) =>
          toast.error(
            error instanceof Error && error.message ? error.message : "继续制作失败，请稍后重试",
          ),
      },
    );

  return (
    <section className="space-y-3 rounded-2xl border border-primary/30 bg-card p-4">
      <div className="min-w-0">
        <div className="flex items-center gap-1.5 text-sm font-semibold">
          <Volume2 className="size-4 shrink-0 text-primary" />
          配音声线
        </div>
        <p className="mt-1 text-xs leading-5 text-muted-foreground">
          {episode ? `第 ${episode} 集` : "本集"}
          需要配音，请选择声线来源。选择后就开始生成配音。
        </p>
      </div>

      <div className="grid gap-2 sm:grid-cols-2">
        {OPTIONS.map((option) => {
          const active = policy === option.value;
          return (
            <button
              key={option.value}
              type="button"
              aria-pressed={active}
              onClick={() => setPolicy(option.value)}
              className={cn(
                "rounded-2xl border px-3 py-2.5 text-left transition-colors",
                active
                  ? "border-primary/50 bg-primary/[0.06]"
                  : "border-border/60 bg-card hover:bg-black/[0.04] dark:hover:bg-white/[0.04]",
              )}
            >
              <div
                className={cn(
                  "text-sm font-medium",
                  active ? "text-foreground" : "text-foreground/90",
                )}
              >
                {option.title}
              </div>
              <div className="mt-0.5 text-xs leading-5 text-muted-foreground">
                {option.hint}
              </div>
            </button>
          );
        })}
      </div>

      {policy === "custom" ? (
        <div className="space-y-3">
          <div className="flex items-start gap-2 rounded-xl border border-border/60 bg-muted/30 px-3 py-2 text-xs leading-5 text-muted-foreground">
            <span>为需要自定义声音的角色上传或录制声线样本；未提供的角色由系统声线兜底。</span>
          </div>
          {charactersQuery.isLoading ? (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Loader2 className="size-3.5 animate-spin" />
              正在读取角色…
            </div>
          ) : characters.length === 0 ? (
            <p className="text-xs text-muted-foreground">暂无可配置的角色。</p>
          ) : (
            <div className="max-h-80 space-y-3 overflow-y-auto pr-1">
              {characters.map((character) => (
                <div key={character.name} className="space-y-1.5">
                  <div className="text-xs font-medium text-muted-foreground">
                    {character.name}
                  </div>
                  <CharacterVoicePanel project={project} character={character} />
                </div>
              ))}
            </div>
          )}
        </div>
      ) : null}

      <div className="flex items-center justify-end">
        <Button
          type="button"
          size="sm"
          className="h-8 rounded-full px-3.5 text-xs"
          disabled={continueRun.isPending}
          onClick={submit}
        >
          {continueRun.isPending ? (
            <Loader2 className="size-3.5 animate-spin" />
          ) : (
            <Play className="size-3.5" />
          )}
          继续制作
        </Button>
      </div>
    </section>
  );
}
