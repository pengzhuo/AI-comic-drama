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

import {
  characterNeedsPortrait,
  DirectorCharacterImagesCard,
} from "@/features/superchat/director-character-images-card";
import type { Character } from "@/types/character";

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

function character(overrides: Partial<Character> = {}): Character {
  return { name: "秦", face_prompt: "冷面", is_main: true, ...overrides };
}

describe("characterNeedsPortrait", () => {
  it("mirrors the backend rule: face prompt + (main or owns identities)", () => {
    expect(characterNeedsPortrait(character())).toBe(true);
    expect(characterNeedsPortrait(character({ is_main: false }))).toBe(false);
    expect(
      characterNeedsPortrait(character({ is_main: false, identity_ids: ["秦_默认"] })),
    ).toBe(true);
    expect(characterNeedsPortrait(character({ face_prompt: "  " }))).toBe(false);
    expect(characterNeedsPortrait(character({ face_prompt: undefined, is_main: true }))).toBe(false);
  });
});

describe("DirectorCharacterImagesCard", () => {
  it("lists only characters that need a portrait and confirms the gate", async () => {
    const confirmed: Array<{ url: string; body: unknown }> = [];
    server.use(
      http.get("http://localhost:3000/api/v1/projects/demo/characters", () =>
        HttpResponse.json({
          ok: true,
          data: [character({ name: "秦" }), character({ name: "路人", is_main: false })],
        }),
      ),
      http.post(
        "http://localhost:3000/api/v1/projects/demo/chat/director-auto/gates/character_images/confirm",
        async ({ request }) => {
          confirmed.push({ url: request.url, body: await request.json() });
          return HttpResponse.json({ ok: true, data: { status: "running" } });
        },
      ),
    );
    const onResolved = vi.fn();

    render(
      <DirectorCharacterImagesCard project="demo" gate="character_images" episode={1} onResolved={onResolved} />,
      { wrapper },
    );

    expect(await screen.findByText("秦")).toBeTruthy();
    expect(screen.queryByText("路人")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /继续制作/ }));

    await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(1));
    expect(confirmed).toHaveLength(1);
    expect(confirmed[0]?.body).toEqual({ episode: 1 });
  });

  it("reads the episode's missing identities from pipeline status", async () => {
    const confirmed: unknown[] = [];
    server.use(
      http.get("http://localhost:3000/api/v1/projects/demo/pipeline/status", () =>
        HttpResponse.json({
          ok: true,
          data: {
            project: "demo",
            global: { ingested: true, configured: true, characters: 1, episodes: 1, portraits_done: true },
            current_episode: 3,
            episode_status: {
              identity_plan: true,
              identities_missing: [
                { character: "秦", identity_id: "秦_少年", identity_name: "少年" },
              ],
            },
            next_step: "identity_images",
            next_step_name: "身份图生成",
          },
        }),
      ),
      http.post(
        "http://localhost:3000/api/v1/projects/demo/chat/director-auto/gates/identity_images/confirm",
        async ({ request }) => {
          confirmed.push(await request.json());
          return HttpResponse.json({ ok: true, data: { status: "running" } });
        },
      ),
    );

    render(
      <DirectorCharacterImagesCard
        project="demo"
        gate="identity_images"
        episode={3}
        onResolved={() => undefined}
      />,
      { wrapper },
    );

    expect(await screen.findByText(/少年/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /跳过/ }));

    await waitFor(() => expect(confirmed).toHaveLength(1));
    expect(confirmed[0]).toEqual({ episode: 3 });
  });
});
