// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import ky from "ky";
import type { ReactNode } from "react";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", () => ({
  api: ky.create({ baseUrl: "http://localhost:3000/" }),
  uploadApi: ky.create({ baseUrl: "http://localhost:3000/" }),
}));

// 角色声线面板会拉 i18n / 弹窗，这里只验证「自定义时列出角色」，用一个桩替掉。
vi.mock("@/components/assets/character-voice-panel", () => ({
  CharacterVoicePanel: ({ character }: { character: { name: string } }) => (
    <div data-testid="voice-panel">{character.name}</div>
  ),
}));

import { StudioVoiceGateCard } from "@/features/studio/studio-voice-gate-card";

const server = setupServer();

beforeAll(() => server.listen());
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function stubContinue() {
  const bodies: unknown[] = [];
  server.use(
    http.post(
      "http://localhost:3000/api/v1/projects/demo/chat/director-auto/continue",
      async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json({ ok: true, data: { status: "running" } });
      },
    ),
  );
  return bodies;
}

describe("StudioVoiceGateCard", () => {
  it("默认选系统声线：继续时带 voice_policy=system", async () => {
    const bodies = stubContinue();

    render(<StudioVoiceGateCard project="demo" episode={3} />, { wrapper });

    expect(screen.getByText("配音声线")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /^系统声线/ }).getAttribute("aria-pressed"),
    ).toBe("true");

    fireEvent.click(screen.getByRole("button", { name: /继续制作/ }));

    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toEqual({ voice_policy: "system" });
  });

  it("选自定义声线后列出角色，继续时带 voice_policy=custom", async () => {
    const bodies = stubContinue();
    server.use(
      http.get("http://localhost:3000/api/v1/projects/demo/characters", () =>
        HttpResponse.json({
          ok: true,
          data: [
            { name: "秦", face_prompt: "冷面", is_main: true },
            { name: "路人", face_prompt: "群演", is_main: false },
          ],
        }),
      ),
    );

    render(<StudioVoiceGateCard project="demo" episode={3} />, { wrapper });

    fireEvent.click(screen.getByRole("button", { name: /^自定义声线/ }));

    const panels = await screen.findAllByTestId("voice-panel");
    expect(panels.map((node) => node.textContent)).toEqual(["秦", "路人"]);

    fireEvent.click(screen.getByRole("button", { name: /继续制作/ }));

    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toEqual({ voice_policy: "custom" });
  });
});
