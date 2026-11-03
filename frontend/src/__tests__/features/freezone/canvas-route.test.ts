// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { describe, expect, it } from "vitest";

import {
  freezoneCanvasRouteForPath,
  projectSectionForPath,
} from "@/features/freezone/canvas-route";

// 画布宿主换成 props 之后，`/projects/<id>/<section>` 的解析就落在这一层。
// 这些用例钉住它：捏画算 active，同一项目别的 section 保持同一个 project（宿主
// 靠它保活），项目之外一律 null。
describe("projectSectionForPath", () => {
  it("splits the project and section", () => {
    expect(projectSectionForPath("/projects/p1/freezone")).toEqual({
      project: "p1",
      section: "freezone",
    });
  });

  it("leaves the section null on the project root", () => {
    expect(projectSectionForPath("/projects/p1")).toEqual({
      project: "p1",
      section: null,
    });
  });

  it("ignores non-project paths", () => {
    expect(projectSectionForPath("/")).toEqual({ project: null, section: null });
    expect(projectSectionForPath("/studio/p1")).toEqual({
      project: null,
      section: null,
    });
  });

  it("decodes the project id", () => {
    expect(projectSectionForPath("/projects/p%201/freezone").project).toBe("p 1");
  });
});

describe("freezoneCanvasRouteForPath", () => {
  it("is active on the freezone section", () => {
    expect(freezoneCanvasRouteForPath("/projects/p1/freezone")).toEqual({
      project: "p1",
      active: true,
    });
  });

  it("keeps the project but goes inactive on a sibling section", () => {
    expect(freezoneCanvasRouteForPath("/projects/p1/ingest")).toEqual({
      project: "p1",
      active: false,
    });
  });

  it("is inactive with no project outside a project path", () => {
    expect(freezoneCanvasRouteForPath("/")).toEqual({
      project: null,
      active: false,
    });
  });

  it("treats a deeper freezone path as the freezone section", () => {
    // 与旧宿主同一套正则：只取两段，第三段不影响「是不是捏画」。
    expect(freezoneCanvasRouteForPath("/projects/p1/freezone/anything")).toEqual({
      project: "p1",
      active: true,
    });
  });
});
