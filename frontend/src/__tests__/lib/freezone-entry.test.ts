// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { describe, expect, it } from "vitest";

import {
  freezoneEntryFromPath,
  freezoneLeavePathFor,
  freezoneLeaveTarget,
  freezoneNavTarget,
  freezonePathFor,
  freezoneProjectFromPath,
} from "@/lib/freezone-entry";

describe("freezone 地址方案", () => {
  describe("freezoneEntryFromPath", () => {
    it("认内部端的捏画路径", () => {
      expect(freezoneEntryFromPath("/projects/proj-a/freezone")).toEqual({
        scheme: "internal",
        project: "proj-a",
      });
      expect(freezoneEntryFromPath("/projects/proj-a/freezone/nodes")).toEqual({
        scheme: "internal",
        project: "proj-a",
      });
    });

    it("认客户画布入口", () => {
      expect(freezoneEntryFromPath("/canvas/proj-a")).toEqual({
        scheme: "canvas",
        project: "proj-a",
      });
      expect(freezoneEntryFromPath("/canvas/proj-a/nodes")).toEqual({
        scheme: "canvas",
        project: "proj-a",
      });
    });

    it("解出 URL 编码的项目名", () => {
      expect(freezoneEntryFromPath("/projects/a%20b/freezone")?.project).toBe("a b");
      expect(freezoneEntryFromPath("/canvas/a%20b")?.project).toBe("a b");
    });

    it("同项目内别的 section 不算捏画（跟进/离开都靠它 no-op）", () => {
      expect(freezoneEntryFromPath("/projects/proj-a/characters")).toBeNull();
      expect(freezoneEntryFromPath("/projects/proj-a/episodes/3/script")).toBeNull();
    });

    it("列表页与根地址不是捏画", () => {
      expect(freezoneEntryFromPath("/canvas")).toBeNull();
      expect(freezoneEntryFromPath("/canvas/")).toBeNull();
      expect(freezoneEntryFromPath("/projects/proj-a")).toBeNull();
      expect(freezoneEntryFromPath("/")).toBeNull();
    });
  });

  describe("freezoneProjectFromPath", () => {
    it("取当前项目 id，非捏画路径为 null", () => {
      expect(freezoneProjectFromPath("/projects/proj-a/freezone")).toBe("proj-a");
      expect(freezoneProjectFromPath("/canvas/proj-a")).toBe("proj-a");
      expect(freezoneProjectFromPath("/studio")).toBeNull();
      expect(freezoneProjectFromPath("/projects/proj-a/characters")).toBeNull();
    });
  });

  describe("字面量路径", () => {
    it("进入捏画：内部端带 /freezone，客户入口直接项目段", () => {
      expect(freezonePathFor("internal", "proj-a")).toBe("/projects/proj-a/freezone");
      expect(freezonePathFor("canvas", "proj-a")).toBe("/canvas/proj-a");
    });

    it("编码项目名，避免拼出坏 URL", () => {
      expect(freezonePathFor("internal", "a b")).toBe("/projects/a%20b/freezone");
      expect(freezonePathFor("canvas", "a b")).toBe("/canvas/a%20b");
    });

    it("离开捏画：内部端回导入页，客户入口回项目列表", () => {
      expect(freezoneLeavePathFor("internal", "proj-a")).toBe("/projects/proj-a/ingest");
      expect(freezoneLeavePathFor("canvas", "proj-a")).toBe("/canvas");
    });
  });

  describe("router.navigate 目标", () => {
    it("进入捏画", () => {
      expect(freezoneNavTarget("internal", "proj-a")).toEqual({
        to: "/projects/$project/freezone",
        params: { project: "proj-a" },
      });
      expect(freezoneNavTarget("canvas", "proj-a")).toEqual({
        to: "/canvas/$project",
        params: { project: "proj-a" },
      });
    });

    it("离开捏画", () => {
      expect(freezoneLeaveTarget("internal", "proj-a")).toEqual({
        to: "/projects/$project/ingest",
        params: { project: "proj-a" },
      });
      // 索引路由按父路径寻址（与 `/studio` 一致），不能写成 `/canvas/`。
      expect(freezoneLeaveTarget("canvas", "proj-a")).toEqual({ to: "/canvas" });
    });
  });
});
