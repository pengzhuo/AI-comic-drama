// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
/**
 * 「配音被阻断」常驻提示。
 *
 * 声线缺失不是一句 toast 能交代完的事：得说清缺的是谁，点得到「去配置声线」，
 * 还要留一条「让捏导配系统声线」的路——系统声线准备是 agent-only 的，
 * 界面刻意不提供「一键生成」，所以这里只把话递到捏导输入框。
 */
import { useTranslation } from "react-i18next";
import { useNavigate } from "@tanstack/react-router";
import { AlertTriangle, Settings2, Sparkles } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  assetTabStorageKey,
  isCharacterVoiceMissing,
  missingCharacterNames,
  voiceConfigTarget,
} from "@/lib/voice-prereq";
import { cn } from "@/lib/utils";
import { askDirectorTo } from "@/features/superchat/director-prefill";

interface VoicePrereqNoticeProps {
  errors: readonly string[];
  project: string;
  className?: string;
}

export function VoicePrereqNotice({
  errors,
  project,
  className,
}: VoicePrereqNoticeProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const messages = errors
    .map((error) => String(error || "").trim())
    .filter((message) => message !== "");
  if (messages.length === 0) return null;

  const characterNames = missingCharacterNames(messages);
  const target = messages.map(voiceConfigTarget).find((item) => item !== null) ?? null;
  const otherMessages = [
    ...new Set(messages.filter((message) => !isCharacterVoiceMissing(message))),
  ];
  const shownOthers = otherMessages.slice(0, 2);
  const hiddenOthers = otherMessages.length - shownOthers.length;

  const openConfig = () => {
    if (!target) return;
    window.localStorage.setItem(assetTabStorageKey(project), target);
    navigate({ to: "/projects/$project/characters", params: { project } });
  };

  const askDirector = () => {
    askDirectorTo(
      characterNames.length > 0
        ? t("episode.workbench.voicePrereq.askDirectorRequest", {
            characters: characterNames.join("、"),
          })
        : t("episode.workbench.voicePrereq.askDirectorRequestNoCharacters"),
    );
  };

  return (
    <div
      className={cn(
        "rounded-lg border border-warning/40 bg-warning/[0.06] p-2",
        className,
      )}
    >
      <div className="flex items-start gap-1.5">
        <AlertTriangle className="mt-px size-3 shrink-0 text-warning" />
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <span className="text-xs font-medium text-warning">
            {characterNames.length > 0
              ? t("episode.workbench.voicePrereq.titleCharacters")
              : t("episode.workbench.voicePrereq.titleVoices")}
          </span>
          {characterNames.length > 0 && (
            <span className="text-xs leading-5 text-muted-foreground">
              {t("episode.workbench.voicePrereq.charactersMissing", {
                characters: characterNames.join("、"),
              })}
            </span>
          )}
          {shownOthers.map((message) => (
            <span
              key={message}
              className="break-words text-xs leading-5 text-muted-foreground"
            >
              {message}
            </span>
          ))}
          {hiddenOthers > 0 && (
            <span className="text-xs text-muted-foreground/70">
              {t("episode.workbench.voicePrereq.moreErrors", { count: hiddenOthers })}
            </span>
          )}
          <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
            {target && (
              <Button variant="outline" size="xs" onClick={openConfig}>
                <Settings2 className="size-3" />
                {t("episode.workbench.voicePrereq.configureAction")}
              </Button>
            )}
            <Button variant="outline" size="xs" onClick={askDirector}>
              <Sparkles className="size-3" />
              {t("episode.workbench.voicePrereq.askDirectorAction")}
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
}
