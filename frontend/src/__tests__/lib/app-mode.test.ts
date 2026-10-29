// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  appMode,
  internalEntryPath,
  internalHomePath,
  isClientMode,
} from "@/lib/app-mode";

// 这些开关是构建期常量，所以只能靠 stub env 覆盖；模块在**调用时**读
// `import.meta.env`（而不是加载时缓存），stub 才生效。
describe("app mode", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("defaults to the internal app", () => {
    expect(appMode()).toBe("internal");
    expect(isClientMode()).toBe(false);
  });

  it("reads the client build flag", () => {
    vi.stubEnv("VITE_APP_MODE", "client");
    expect(isClientMode()).toBe(true);
  });

  it("treats unknown flags as the internal app", () => {
    vi.stubEnv("VITE_APP_MODE", "something-else");
    expect(isClientMode()).toBe(false);
  });
});

describe("internal entry path (dual entry instance)", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("is off by default: `/` is the internal home", () => {
    expect(internalEntryPath()).toBe("");
    expect(internalHomePath()).toBe("/");
  });

  it("turns on with VITE_INTERNAL_ENTRY_PATH and moves the internal home", () => {
    vi.stubEnv("VITE_INTERNAL_ENTRY_PATH", "/console");
    expect(internalEntryPath()).toBe("/console");
    expect(internalHomePath()).toBe("/console");
  });

  it.each([
    ["/console/", "/console"],
    ["console", "/console"],
    ["  /console  ", "/console"],
    ["/console//", "/console"],
  ])("normalizes %s to %s", (raw, expected) => {
    vi.stubEnv("VITE_INTERNAL_ENTRY_PATH", raw);
    expect(internalEntryPath()).toBe(expected);
    expect(internalHomePath()).toBe("/console");
  });

  it.each(["", "   ", "/"])(
    "treats %s as the traditional internal entry",
    (raw) => {
      vi.stubEnv("VITE_INTERNAL_ENTRY_PATH", raw);
      expect(internalEntryPath()).toBe("");
      expect(internalHomePath()).toBe("/");
    },
  );
});
