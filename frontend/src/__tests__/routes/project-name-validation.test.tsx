// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { describe, expect, it } from "vitest";

import {
  PROJECT_NAME_MAX_LENGTH,
  getProjectNameValidationKey,
} from "@/routes/_app/index";

describe("项目名称校验", () => {
  it("接受 64 字符名称", () => {
    expect(PROJECT_NAME_MAX_LENGTH).toBe(64);
    expect(getProjectNameValidationKey("a".repeat(64))).toBeNull();
  });

  it("拒绝 65 字符名称", () => {
    expect(getProjectNameValidationKey("a".repeat(65))).toBe("project.nameTooLong");
  });

  // 后端 validate_project_name 已放开 Unicode 字母/数字/下划线（含中文），
  // 仪表盘的自建项目弹窗必须与之一致，否则用户会被前端拦下。
  it("接受中文项目名（与后端一致）", () => {
    for (const name of ["末世测试", "十年离家", "七子围城", "中文_123", "Studio_01"]) {
      expect(getProjectNameValidationKey(name)).toBeNull();
    }
  });

  it("拒绝路径分隔符、通配符、点与空格", () => {
    const invalid = ["a b", "a/b", "a\\b", "a.b", "..", "a:b", "a*b", 'a"b', "a<b", "a|b"];
    for (const name of invalid) {
      expect(getProjectNameValidationKey(name)).toBe("project.nameInvalid");
    }
  });

  it("拒绝下划线开头的保留前缀", () => {
    expect(getProjectNameValidationKey("_x")).toBe("project.nameReservedPrefix");
  });
});
