// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import ky from "ky";
import type { ReactNode } from "react";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/api", () => ({
  api: ky.create({ baseUrl: "http://localhost:3000/" }),
  uploadApi: ky.create({ baseUrl: "http://localhost:3000/" }),
}));

import { AccessSettings, randomAccessKey } from "@/features/access/access-settings";

const server = setupServer();

const CONFIG_URL = "http://localhost:3000/api/v1/access/config";
const LINK_URL = "http://localhost:3000/api/v1/access/config/link";
const GATE_URL = "http://localhost:3000/api/v1/access/gate";

// 掩码刻意与真实密钥同形：页面只该显示它，明文永远不出现在 DOM 里。
const PLAINTEXT_INTERNAL_KEY = "0123456789abcdef0123456789abcdef";
const PLAINTEXT_CLIENT_SECRET = "test-client-secret";

function configPayload(overrides: Record<string, unknown> = {}) {
  return {
    ok: true,
    data: {
      enabled: true,
      gate_disabled: false,
      internal_key: {
        configured: true,
        mask: "0123…cdef",
        source: "file",
        env_locked: false,
      },
      client_secret: {
        configured: true,
        mask: "test…cret",
        source: "file",
        env_locked: false,
      },
      file: {
        path: "/data/studio-access.secret",
        exists: true,
        updated_at: "2026-11-01T15:13:00+08:00",
      },
      ...overrides,
    },
  };
}

function renderSettings() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  }
  return render(<AccessSettings />, { wrapper });
}

beforeAll(() => server.listen());
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("AccessSettings", () => {
  it("shows the masked keys and never the plaintext ones", async () => {
    server.use(http.get(CONFIG_URL, () => HttpResponse.json(configPayload())));

    renderSettings();

    expect(await screen.findByText("0123…cdef")).toBeTruthy();
    expect(screen.getByText("test…cret")).toBeTruthy();
    expect(screen.getByText("访问验证已开启")).toBeTruthy();
    // 后端只回掩码；页面上不该出现任何明文密钥。
    expect(document.body.textContent).not.toContain(PLAINTEXT_INTERNAL_KEY);
    expect(document.body.textContent).not.toContain(PLAINTEXT_CLIENT_SECRET);
    expect(document.body.textContent).toContain("/data/studio-access.secret");
  });

  it("rotates the internal key and surfaces the backend notice", async () => {
    const seen: { body?: unknown } = {};
    server.use(
      http.get(CONFIG_URL, () => HttpResponse.json(configPayload())),
      http.post(CONFIG_URL, async ({ request }) => {
        seen.body = await request.clone().json();
        return HttpResponse.json({
          ...configPayload({ internal_key: { configured: true, mask: "zyxw…7654", source: "file", env_locked: false } }),
          notices: ["内部口令已更新：其他设备上的内部登录已失效，本页面已自动续期。"],
        });
      }),
    );
    const user = userEvent.setup();

    renderSettings();
    const input = await screen.findByLabelText("新的内部口令（留空表示不修改）");
    await user.type(input, "zyxwvutsrqponmlkjihgfedcba987654");
    await user.click(screen.getByRole("button", { name: "保存口令" }));

    await waitFor(() => expect(seen.body).toEqual({
      internal_key: "zyxwvutsrqponmlkjihgfedcba987654",
      client_secret: "",
    }));
    expect(await screen.findByRole("status")).toHaveTextContent("其他设备上的内部登录已失效");
    // 保存成功后输入框清空，避免误以为还要点第二次。
    expect((input as HTMLInputElement).value).toBe("");
  });

  it("fills the draft with a 32-character URL-safe random key", async () => {
    server.use(http.get(CONFIG_URL, () => HttpResponse.json(configPayload())));
    const user = userEvent.setup();

    renderSettings();
    const input = await screen.findByLabelText("新的内部口令（留空表示不修改）");
    await user.click(screen.getAllByRole("button", { name: "随机生成" })[0]);

    expect((input as HTMLInputElement).value).toMatch(/^[A-Za-z0-9_-]{32}$/);
    expect(randomAccessKey()).not.toBe(randomAccessKey());
  });

  it("requires the current internal key before closing the gate", async () => {
    const seen: { body?: unknown } = {};
    server.use(
      http.get(CONFIG_URL, () => HttpResponse.json(configPayload())),
      http.post(GATE_URL, async ({ request }) => {
        seen.body = await request.clone().json();
        return HttpResponse.json({
          ...configPayload({ enabled: false, gate_disabled: true }),
          notices: ["访问验证已关闭：任何人都能看到全部项目，内部口令与客户密钥已保留。"],
        });
      }),
    );
    const user = userEvent.setup();

    renderSettings();
    await user.click(await screen.findByRole("button", { name: "关闭访问验证…" }));

    const confirmButton = screen.getByRole("button", { name: "确认关闭" });
    expect(confirmButton).toBeDisabled();
    await user.type(screen.getByLabelText("输入当前内部口令以确认关闭"), PLAINTEXT_INTERNAL_KEY);
    expect(confirmButton).not.toBeDisabled();
    await user.click(confirmButton);

    await waitFor(() => expect(seen.body).toEqual({
      enabled: false,
      confirm_key: PLAINTEXT_INTERNAL_KEY,
      internal_key: "",
      client_secret: "",
    }));
    expect(await screen.findByText("访问验证已关闭")).toBeTruthy();
    expect(screen.getByRole("status")).toHaveTextContent("任何人都能看到全部项目");
  });

  it("blocks re-enabling until a client secret exists", async () => {
    server.use(
      http.get(CONFIG_URL, () =>
        HttpResponse.json(
          configPayload({
            enabled: false,
            gate_disabled: true,
            client_secret: { configured: false, mask: "", source: "none", env_locked: false },
          }),
        ),
      ),
    );

    renderSettings();

    const enable = await screen.findByRole("button", { name: "重新开启访问验证" });
    expect(enable).toBeDisabled();
    expect(screen.getByText("请先在上方设置客户链接密钥，再开启访问验证。")).toBeTruthy();
  });

  it("disables editing when the key comes from an environment variable", async () => {
    server.use(
      http.get(CONFIG_URL, () =>
        HttpResponse.json(
          configPayload({
            internal_key: {
              configured: true,
              mask: "env-…-key",
              source: "env",
              env_locked: true,
            },
          }),
        ),
      ),
    );

    renderSettings();

    const input = await screen.findByLabelText("新的内部口令（留空表示不修改）");
    expect((input as HTMLInputElement).disabled).toBe(true);
    expect(screen.getAllByRole("button", { name: "保存口令" })[0]).toBeDisabled();
    expect(screen.getAllByText("（来自环境变量）").length).toBeGreaterThan(0);
  });

  it("generates a client link for an 11-digit phone number", async () => {
    const seen: { body?: unknown } = {};
    server.use(
      http.get(CONFIG_URL, () => HttpResponse.json(configPayload())),
      http.post(LINK_URL, async ({ request }) => {
        seen.body = await request.clone().json();
        return HttpResponse.json({
          ok: true,
          data: {
            phone: "13800000000",
            code: "21f736a7c309a30a8f66a85cf1c1f5cb",
            link: "https://aistory.nanie168.com/studio?phone=13800000000&code=21f736a7c309a30a8f66a85cf1c1f5cb",
          },
        });
      }),
    );
    const user = userEvent.setup();

    renderSettings();
    await user.type(await screen.findByLabelText("客户手机号（11 位）"), "13800000000");
    await user.click(screen.getByRole("button", { name: "生成链接" }));

    await waitFor(() => expect(seen.body).toEqual({ phone: "13800000000" }));
    expect(
      await screen.findByText(
        "https://aistory.nanie168.com/studio?phone=13800000000&code=21f736a7c309a30a8f66a85cf1c1f5cb",
      ),
    ).toBeTruthy();
  });

  it("shows a helpful card when the settings cannot be read", async () => {
    server.use(http.get(CONFIG_URL, () => new HttpResponse(null, { status: 404 })));

    renderSettings();

    expect(await screen.findByText("无法读取访问设置")).toBeTruthy();
    expect(screen.getByRole("button", { name: "重试" })).toBeTruthy();
  });
});
