// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { beforeEach, describe, expect, it } from "vitest";

import {
  askDirectorTo,
  useDirectorPrefillStore,
} from "@/features/superchat/director-prefill";
import { useAppStore } from "@/stores/app-store";

describe("director prefill", () => {
  beforeEach(() => {
    useDirectorPrefillStore.setState({ pending: null });
    useAppStore.setState({ aiAssistantOpen: false });
  });

  it("keeps the request and opens the assistant panel", () => {
    askDirectorTo("  请用系统声线补齐本集缺失的声线  ");

    expect(useDirectorPrefillStore.getState().pending).toBe(
      "请用系统声线补齐本集缺失的声线",
    );
    expect(useAppStore.getState().aiAssistantOpen).toBe(true);
  });

  it("ignores a blank request", () => {
    askDirectorTo("   ");

    expect(useDirectorPrefillStore.getState().pending).toBeNull();
    expect(useAppStore.getState().aiAssistantOpen).toBe(false);
  });

  it("hands the request to exactly one consumer", () => {
    askDirectorTo("补齐声线");

    expect(useDirectorPrefillStore.getState().consume()).toBe("补齐声线");
    expect(useDirectorPrefillStore.getState().pending).toBeNull();
    expect(useDirectorPrefillStore.getState().consume()).toBeNull();
  });
});
