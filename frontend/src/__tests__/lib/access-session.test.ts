// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import ky from "ky";

import { server } from "@/__mocks__/msw/server";

// The repo convention for ky-backed code: give the shared client an absolute
// base (jsdom cannot resolve the production `prefix: "/"` for `new Request`),
// then serve the endpoints with MSW.
vi.mock("@/lib/api", () => ({
  api: ky.create({ baseUrl: "http://localhost:3000/" }),
  uploadApi: ky.create({ baseUrl: "http://localhost:3000/" }),
}));

import {
  accessSessionSnapshot,
  activateClientSession,
  activateInternalSession,
  fetchAccessSession,
  logoutAccessSession,
  maskPhone,
  resetAccessSessionCache,
  setAccessNotice,
  takeAccessNotice,
} from "@/lib/access-session";

const BASE = "http://localhost:3000/api/v1";
const SESSION_URL = `${BASE}/access/session`;
const CLIENT_URL = `${BASE}/access/client-session`;
const INTERNAL_URL = `${BASE}/access/internal-session`;
const LOGOUT_URL = `${BASE}/access/logout`;

beforeEach(() => {
  resetAccessSessionCache();
  setAccessNotice(null);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("maskPhone", () => {
  it("masks the middle of an 11-digit phone", () => {
    expect(maskPhone("13800000000")).toBe("138****0000");
  });

  it("passes through anything that is not an 11-digit phone", () => {
    expect(maskPhone("")).toBe("");
    expect(maskPhone("local")).toBe("local");
    expect(maskPhone("138")).toBe("138");
  });
});

describe("access notices", () => {
  it("is consumed exactly once", () => {
    setAccessNotice("访问链接无效");
    expect(takeAccessNotice()).toBe("访问链接无效");
    expect(takeAccessNotice()).toBeNull();
  });
});

describe("fetchAccessSession", () => {
  it("reports the disabled gate once, then serves the cached probe", async () => {
    let calls = 0;
    server.use(
      http.get(SESSION_URL, () => {
        calls += 1;
        return HttpResponse.json({ ok: true, data: { enabled: false, kind: "open" } });
      }),
    );

    await expect(fetchAccessSession()).resolves.toEqual({
      enabled: false,
      kind: "open",
      phone: "",
    });
    await fetchAccessSession();

    expect(calls).toBe(1);
  });

  it("reports a client session with the phone", async () => {
    server.use(
      http.get(SESSION_URL, () =>
        HttpResponse.json({
          ok: true,
          data: { enabled: true, kind: "client", phone: "13800000000" },
        }),
      ),
    );

    await expect(fetchAccessSession()).resolves.toEqual({
      enabled: true,
      kind: "client",
      phone: "13800000000",
    });
    // The snapshot backs `useAccessSession()` for the gate components.
    expect(accessSessionSnapshot()).toMatchObject({ enabled: true, kind: "client" });
  });

  it("reports an internal session without a phone", async () => {
    server.use(
      http.get(SESSION_URL, () =>
        HttpResponse.json({ ok: true, data: { enabled: true, kind: "internal" } }),
      ),
    );

    await expect(fetchAccessSession()).resolves.toEqual({
      enabled: true,
      kind: "internal",
      phone: "",
    });
  });

  it("falls back to the disabled gate when the probe fails", async () => {
    server.use(http.get(SESSION_URL, () => new HttpResponse(null, { status: 500 })));

    await expect(fetchAccessSession()).resolves.toEqual({
      enabled: false,
      kind: "open",
      phone: "",
    });
  });
});

describe("activateClientSession", () => {
  it("posts the phone and code, then drops the cached probe", async () => {
    const seen: { method?: string; body?: unknown } = {};
    server.use(
      http.get(SESSION_URL, () =>
        HttpResponse.json({ ok: true, data: { enabled: true, kind: "none" } }),
      ),
      http.post(CLIENT_URL, async ({ request }) => {
        seen.method = request.method;
        seen.body = await request.clone().json();
        return HttpResponse.json({ ok: true, data: { kind: "client" } });
      }),
    );

    await fetchAccessSession();
    await expect(activateClientSession("13800000000", "a".repeat(32))).resolves.toEqual({
      ok: true,
    });

    expect(seen.method).toBe("POST");
    expect(seen.body).toEqual({ phone: "13800000000", code: "a".repeat(32) });
  });

  it("surfaces the server error for a bad link", async () => {
    server.use(
      http.post(CLIENT_URL, () =>
        HttpResponse.json(
          { ok: false, error: "访问链接无效，请使用收到的专属链接" },
          { status: 400 },
        ),
      ),
    );

    await expect(activateClientSession("13800000000", "0".repeat(32))).resolves.toEqual({
      ok: false,
      error: "访问链接无效，请使用收到的专属链接",
    });
  });
});

describe("activateInternalSession", () => {
  it("posts the internal key", async () => {
    const seen: { body?: unknown } = {};
    server.use(
      http.post(INTERNAL_URL, async ({ request }) => {
        seen.body = await request.clone().json();
        return HttpResponse.json({ ok: true, data: { kind: "internal" } });
      }),
    );

    await expect(activateInternalSession("secret-key")).resolves.toEqual({ ok: true });
    expect(seen.body).toEqual({ key: "secret-key" });
  });

  it("reports a wrong key without echoing the key", async () => {
    server.use(
      http.post(INTERNAL_URL, () =>
        HttpResponse.json({ ok: false, error: "内部访问口令不正确" }, { status: 400 }),
      ),
    );

    await expect(activateInternalSession("nope")).resolves.toEqual({
      ok: false,
      error: "内部访问口令不正确",
    });
  });
});

describe("logoutAccessSession", () => {
  it("clears the session cookie and the cached probe", async () => {
    let logoutCalls = 0;
    server.use(
      http.post(LOGOUT_URL, () => {
        logoutCalls += 1;
        return HttpResponse.json({ ok: true });
      }),
    );

    await expect(logoutAccessSession()).resolves.toBeUndefined();
    expect(logoutCalls).toBe(1);
    expect(accessSessionSnapshot()).toBeNull();
  });

  it("still clears local state when the request fails", async () => {
    server.use(http.post(LOGOUT_URL, () => new HttpResponse(null, { status: 502 })));

    await expect(logoutAccessSession()).resolves.toBeUndefined();

    server.use(
      http.get(SESSION_URL, () =>
        HttpResponse.json({
          ok: true,
          data: { enabled: true, kind: "client", phone: "13800000000" },
        }),
      ),
    );
    await expect(fetchAccessSession()).resolves.toMatchObject({ kind: "client" });
  });
});
