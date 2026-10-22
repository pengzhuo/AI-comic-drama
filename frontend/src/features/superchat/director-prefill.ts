// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
/**
 * 工作台 → 捏导的「帮我做这件事」通道。
 *
 * 把一句话写进捏导输入框、顺手把面板打开，但**不代发**——用户点一个 CTA
 * 就悄悄起一轮对话（还可能是计费的）不是这里的意图，发送按钮留给用户按。
 *
 * 不入 localStorage：这是一次点击的意图，刷新后不该再冒出来。
 * 面板此刻没挂载时先攒在 store 里，挂载后由 SuperChatPanel 抽干（consume 是抢的，
 * 多个实例同时挂着也只有第一个拿到）。
 */
import { create } from "zustand";

import { useAppStore } from "@/stores/app-store";

interface DirectorPrefillState {
  pending: string | null;
  request: (text: string) => void;
  consume: () => string | null;
}

export const useDirectorPrefillStore = create<DirectorPrefillState>((set, get) => ({
  pending: null,
  request: (text) => set({ pending: text }),
  consume: () => {
    const pending = get().pending;
    if (pending !== null) set({ pending: null });
    return pending;
  },
}));

/** 打开捏导面板并把这句话写进输入框（等用户自己发送）。 */
export function askDirectorTo(text: string): void {
  const request = text.trim();
  if (!request) return;
  useDirectorPrefillStore.getState().request(request);
  useAppStore.getState().setAiAssistantOpen(true);
}
