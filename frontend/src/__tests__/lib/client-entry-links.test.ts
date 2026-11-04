// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { describe, expect, it } from "vitest";

import { clientEntryFromPath, crossEntryFor } from "@/lib/client-entry-links";

describe("clientEntryFromPath", () => {
  it("解析工作室项目页", () => {
    expect(clientEntryFromPath("/studio/01ABC")).toEqual({
      kind: "studio",
      project: "01ABC",
    });
  });

  it("解析捏画项目页（含子路径）", () => {
    expect(clientEntryFromPath("/canvas/01ABC")).toEqual({
      kind: "canvas",
      project: "01ABC",
    });
    expect(clientEntryFromPath("/canvas/01ABC/episodes/1")).toEqual({
      kind: "canvas",
      project: "01ABC",
    });
  });

  it("首页与别的区段返回 null", () => {
    expect(clientEntryFromPath("/studio")).toBeNull();
    expect(clientEntryFromPath("/studio/")).toBeNull();
    expect(clientEntryFromPath("/canvas")).toBeNull();
    expect(clientEntryFromPath("/")).toBeNull();
    expect(clientEntryFromPath("/projects/01ABC/freezone")).toBeNull();
  });

  it("对项目 id 做 decodeURIComponent", () => {
    expect(clientEntryFromPath("/canvas/a%20b")).toEqual({
      kind: "canvas",
      project: "a b",
    });
  });
});

describe("crossEntryFor", () => {
  it("工作室 → 进入捏画（/canvas/<项目>）", () => {
    expect(crossEntryFor({ kind: "studio", project: "01ABC" })).toEqual({
      to: "/canvas/$project",
      params: { project: "01ABC" },
      label: "进入捏画",
    });
  });

  it("捏画 → 返回工作台（/studio/<项目>）", () => {
    expect(crossEntryFor({ kind: "canvas", project: "01ABC" })).toEqual({
      to: "/studio/$project",
      params: { project: "01ABC" },
      label: "返回工作台",
    });
  });
});
