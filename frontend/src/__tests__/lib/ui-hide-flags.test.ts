// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { afterEach, describe, expect, it, vi } from "vitest";
import { pikoEntryHidden, releasePopupHidden } from "@/lib/ui-hide-flags";

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("ui hide flags", () => {
  it("keeps both surfaces visible when the instance flags are unset", () => {
    vi.stubEnv("VITE_HIDE_RELEASE_POPUP", "");
    vi.stubEnv("VITE_HIDE_PIKO_ENTRY", "");
    expect(releasePopupHidden()).toBe(false);
    expect(pikoEntryHidden()).toBe(false);
  });

  it("hides a surface for either truthy spelling", () => {
    vi.stubEnv("VITE_HIDE_PIKO_ENTRY", "1");
    vi.stubEnv("VITE_HIDE_RELEASE_POPUP", "true");
    expect(pikoEntryHidden()).toBe(true);
    expect(releasePopupHidden()).toBe(true);
  });
});
