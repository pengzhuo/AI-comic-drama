// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { afterEach, describe, expect, it, vi } from "vitest";
import { githubStarsHidden, modelGuideHidden, notificationCenterHidden, officialChannelsHidden, pikoEntryHidden, releasePopupHidden } from "@/lib/ui-hide-flags";

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("ui hide flags", () => {
  it("keeps both surfaces visible when the instance flags are unset", () => {
    vi.stubEnv("VITE_HIDE_RELEASE_POPUP", "");
    vi.stubEnv("VITE_HIDE_PIKO_ENTRY", "");
    vi.stubEnv("VITE_HIDE_OFFICIAL_CHANNELS", "");
    vi.stubEnv("VITE_HIDE_NOTIFICATION_CENTER", "");
    vi.stubEnv("VITE_HIDE_MODEL_GUIDE", "");
    vi.stubEnv("VITE_HIDE_GITHUB_STARS", "");
    expect(releasePopupHidden()).toBe(false);
    expect(pikoEntryHidden()).toBe(false);
    expect(officialChannelsHidden()).toBe(false);
    expect(notificationCenterHidden()).toBe(false);
    expect(modelGuideHidden()).toBe(false);
    expect(githubStarsHidden()).toBe(false);
  });

  it("hides a surface for either truthy spelling", () => {
    vi.stubEnv("VITE_HIDE_PIKO_ENTRY", "1");
    vi.stubEnv("VITE_HIDE_RELEASE_POPUP", "true");
    vi.stubEnv("VITE_HIDE_OFFICIAL_CHANNELS", "1");
    vi.stubEnv("VITE_HIDE_NOTIFICATION_CENTER", "1");
    vi.stubEnv("VITE_HIDE_MODEL_GUIDE", "1");
    vi.stubEnv("VITE_HIDE_GITHUB_STARS", "1");
    expect(pikoEntryHidden()).toBe(true);
    expect(releasePopupHidden()).toBe(true);
    expect(officialChannelsHidden()).toBe(true);
    expect(notificationCenterHidden()).toBe(true);
    expect(modelGuideHidden()).toBe(true);
    expect(githubStarsHidden()).toBe(true);
  });
});
