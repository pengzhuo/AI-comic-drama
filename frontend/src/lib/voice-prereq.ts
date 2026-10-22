// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
/**
 * 「声线缺失」前置错误的识别、跳转目标与文案。
 *
 * 这里比对的都是后端报错原文（`indextts2_beat_audio_task.py` 拼出来的那些），
 * 不是界面文案——跟着界面语言翻一遍就认不出来了，所以下面的字面量保持中文原样。
 * 单 beat 重配（audio-pane）和批量配音（batch-panel）共用这一份判断，避免两边漂移。
 */
// i18n-exempt-start
export type VoiceConfigTarget = "characters" | "voices";

const ASSET_TAB_STORAGE_KEY_PREFIX = "supertale-asset-tab:";

/** 「资产」页读这个键决定落在哪个页签（见 routes/_app/projects.$project/characters.lazy.tsx）。 */
export function assetTabStorageKey(project: string): string {
  return `${ASSET_TAB_STORAGE_KEY_PREFIX}${encodeURIComponent(project)}`;
}

/** 后端报错里的缺声线角色名：`Beat 08 角色声线缺失：伊卡` / `角色声线缺失（speaker=伊卡）`。 */
const CHARACTER_NAME_PATTERNS: readonly RegExp[] = [
  /角色声线缺失[：:]\s*([^\s，,、；;（()）]+)/u,
  /角色声线缺失（speaker=([^）)]*)）/u,
];

export function isCharacterVoiceMissing(error: string): boolean {
  return String(error || "").includes("角色声线缺失");
}

/** 「去配置声线」该落到哪个页签；null = 这条报错不靠上传／录制声线解决。 */
export function voiceConfigTarget(error: string): VoiceConfigTarget | null {
  const message = String(error || "").trim();
  if (!message.includes("声线缺失")) return null;
  if (message.includes("角色声线缺失")) return "characters";
  if (message.includes("解说声线缺失")) {
    if (message.includes("解说主角") || message.includes("角色工作台")) return "characters";
    return "voices";
  }
  // 只说「声线缺失」的旧报错：去声线页签最接近可自助的修法。
  return "voices";
}

/** 追加到报错后面的那句指点（键名见 locales 的 episode.workbench.audio.*）。 */
export function voicePrereqHintKey(error: string): string | null {
  const message = String(error || "");
  if (!message.includes("声线缺失")) return null;
  if (message.includes("角色声线缺失")) {
    return "episode.workbench.audio.prereqHintCharacterVoices";
  }
  if (message.includes("解说主角") || message.includes("角色工作台")) {
    return "episode.workbench.audio.prereqHintCharacters";
  }
  return "episode.workbench.audio.prereqHintVoices";
}

export function voicePrereqMessage(
  error: string,
  t: (key: string) => string,
): string {
  const message = String(error || "").trim();
  const hintKey = voicePrereqHintKey(message);
  return hintKey ? `${message}${t(hintKey)}` : message;
}

/** 去重后的缺声线角色名，保持后端返回顺序。 */
export function missingCharacterNames(errors: readonly string[]): string[] {
  const names: string[] = [];
  for (const raw of errors) {
    const message = String(raw || "");
    if (!message.includes("角色声线缺失")) continue;
    for (const pattern of CHARACTER_NAME_PATTERNS) {
      const name = pattern.exec(message)?.[1]?.trim();
      if (name && !names.includes(name)) names.push(name);
    }
  }
  return names;
}
// i18n-exempt-end
