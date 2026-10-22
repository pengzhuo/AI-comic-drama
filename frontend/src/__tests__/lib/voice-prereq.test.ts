// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { describe, expect, it } from "vitest";

import {
  assetTabStorageKey,
  isCharacterVoiceMissing,
  missingCharacterNames,
  voiceConfigTarget,
  voicePrereqMessage,
} from "@/lib/voice-prereq";

describe("voice-prereq", () => {
  it("sends narrator voice errors to the voices asset tab", () => {
    expect(
      voiceConfigTarget("Beat 01 解说声线缺失：项目解说人声线未配置，请上传或录制解说人音频"),
    ).toBe("voices");
  });

  it("sends first-person narrator protagonist errors to the characters tab", () => {
    expect(
      voiceConfigTarget("Beat 01 解说声线缺失：解说主角声线缺失，请到角色工作台上传角色声线"),
    ).toBe("characters");
  });

  it("sends per-character voice errors to the characters tab", () => {
    expect(voiceConfigTarget("Beat 08 角色声线缺失：伊卡")).toBe("characters");
  });

  it("leaves unrelated failures alone", () => {
    expect(voiceConfigTarget("TTS 服务暂不可用")).toBeNull();
    expect(voicePrereqMessage("TTS 服务暂不可用", (key) => `[${key}]`)).toBe(
      "TTS 服务暂不可用",
    );
  });

  it("appends the hint that matches the error kind", () => {
    expect(voicePrereqMessage("Beat 08 角色声线缺失：伊卡", (key) => `[${key}]`)).toBe(
      "Beat 08 角色声线缺失：伊卡[episode.workbench.audio.prereqHintCharacterVoices]",
    );
    expect(voicePrereqMessage("Beat 01 解说声线缺失：x", (key) => `[${key}]`)).toBe(
      "Beat 01 解说声线缺失：x[episode.workbench.audio.prereqHintVoices]",
    );
  });

  it("collects and dedupes the characters that lack a voice", () => {
    expect(
      missingCharacterNames([
        "Beat 08 角色声线缺失：伊卡",
        "Beat 10 角色声线缺失：布·蒂妮",
        "Beat 13 角色声线缺失：伊卡",
        "Beat 15 解说声线缺失：项目解说人声线未配置",
        "角色声线缺失（speaker=<空>）：未找到声线文件",
      ]),
    ).toEqual(["伊卡", "布·蒂妮", "<空>"]);
  });

  it("ignores character errors it cannot read a name out of", () => {
    expect(missingCharacterNames(["角色声线缺失", "Beat 08 角色声线缺失："])).toEqual([]);
  });

  it("detects character voice errors", () => {
    expect(isCharacterVoiceMissing("Beat 08 角色声线缺失：伊卡")).toBe(true);
    expect(isCharacterVoiceMissing("Beat 08 解说声线缺失：x")).toBe(false);
  });

  it("keeps the asset tab storage key byte-for-byte compatible", () => {
    expect(assetTabStorageKey("demo")).toBe("supertale-asset-tab:demo");
    expect(assetTabStorageKey("demo project")).toBe("supertale-asset-tab:demo%20project");
  });
});
