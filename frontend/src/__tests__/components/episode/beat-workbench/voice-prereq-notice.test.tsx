// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { I18nextProvider, initReactI18next } from "react-i18next";
import i18next from "i18next";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { VoicePrereqNotice } from "@/components/episode/beat-workbench/voice-prereq-notice";
import { useDirectorPrefillStore } from "@/features/superchat/director-prefill";
import { useAppStore } from "@/stores/app-store";

const navigateMock = vi.hoisted(() => vi.fn());

vi.mock("@tanstack/react-router", () => ({
  useNavigate: () => navigateMock,
}));

const i18n = i18next.createInstance();

beforeAll(async () => {
  await i18n.use(initReactI18next).init({
    lng: "zh",
    fallbackLng: "zh",
    interpolation: { escapeValue: false },
    resources: {
      zh: {
        translation: {
          episode: {
            workbench: {
              voicePrereq: {
                titleCharacters: "配音被阻断：角色声线缺失",
                titleVoices: "配音被阻断：解说声线缺失",
                charactersMissing: "缺少声线的角色：{{characters}}",
                moreErrors: "另有 {{count}} 条同类提示",
                configureAction: "去配置声线",
                askDirectorAction: "让捏导配系统声线",
                askDirectorRequest:
                  "请用系统声线补齐本集缺失的角色声线（角色：{{characters}}），补齐后继续配音。",
                askDirectorRequestNoCharacters:
                  "请用系统声线补齐本集缺失的声线，补齐后继续配音。",
              },
            },
          },
        },
      },
    },
  });
});

function renderNotice(errors: string[], project = "demo") {
  return render(
    <I18nextProvider i18n={i18n}>
      <VoicePrereqNotice errors={errors} project={project} />
    </I18nextProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
  useDirectorPrefillStore.setState({ pending: null });
  useAppStore.setState({ aiAssistantOpen: false });
});

describe("VoicePrereqNotice", () => {
  it("dedupes the characters and offers the jump to voice configuration", async () => {
    const user = userEvent.setup();
    renderNotice([
      "Beat 08 角色声线缺失：伊卡",
      "Beat 13 角色声线缺失：伊卡",
      "Beat 10 角色声线缺失：布·蒂妮",
    ]);

    expect(screen.getByText("配音被阻断：角色声线缺失")).toBeInTheDocument();
    expect(screen.getByText("缺少声线的角色：伊卡、布·蒂妮")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "去配置声线" }));

    expect(window.localStorage.getItem("supertale-asset-tab:demo")).toBe("characters");
    expect(navigateMock).toHaveBeenCalledWith({
      to: "/projects/$project/characters",
      params: { project: "demo" },
    });
  });

  it("asks the director to fill the missing character voices in", async () => {
    const user = userEvent.setup();
    renderNotice(["Beat 08 角色声线缺失：伊卡", "Beat 10 角色声线缺失：布·蒂妮"]);

    await user.click(screen.getByRole("button", { name: "让捏导配系统声线" }));

    expect(useDirectorPrefillStore.getState().pending).toBe(
      "请用系统声线补齐本集缺失的角色声线（角色：伊卡、布·蒂妮），补齐后继续配音。",
    );
    expect(useAppStore.getState().aiAssistantOpen).toBe(true);
  });

  it("keeps narrator voice errors on the voices tab and caps the listed errors", () => {
    renderNotice([
      "Beat 01 解说声线缺失：项目解说人声线未配置",
      "Beat 02 解说声线缺失：TTS 服务暂不可用",
      "Beat 03 解说声线缺失：声线文件损坏",
    ]);

    expect(screen.getByText("配音被阻断：解说声线缺失")).toBeInTheDocument();
    expect(
      screen.getByText("Beat 01 解说声线缺失：项目解说人声线未配置"),
    ).toBeInTheDocument();
    expect(screen.getByText("Beat 02 解说声线缺失：TTS 服务暂不可用")).toBeInTheDocument();
    expect(
      screen.queryByText("Beat 03 解说声线缺失：声线文件损坏"),
    ).not.toBeInTheDocument();
    expect(screen.getByText("另有 1 条同类提示")).toBeInTheDocument();
    expect(window.localStorage.getItem("supertale-asset-tab:demo")).toBeNull();
  });

  it("renders nothing without errors", () => {
    const { container } = renderNotice([]);

    expect(container).toBeEmptyDOMElement();
    expect(navigateMock).not.toHaveBeenCalled();
  });
});
