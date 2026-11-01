// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useSyncExternalStore } from "react";

import { api } from "@/lib/api";

/**
 * 工作台访问验证（客户端 `/studio` / 内部 `/console`）。
 *
 * 身份由后端签发的 HttpOnly cookie 承载，前端只负责：
 * 1. 用链接里的 `?phone=&code=` 换 cookie；
 * 2. 探测当前浏览器持有什么身份（`GET /api/v1/access/session`）；
 * 3. 未启用访问验证的实例（`enabled === false`）维持原行为。
 *
 * 校验码只在服务端比对，前端拿不到、也不需要 SECRET。
 */
export type AccessSessionKind = "open" | "none" | "client" | "internal";

export interface AccessSession {
  /** 实例是否启用了访问验证（后端配置了 client secret）。 */
  enabled: boolean;
  kind: AccessSessionKind;
  /** 客户身份的手机号；内部/匿名时为空串。 */
  phone: string;
}

export interface AccessActionResult {
  ok: boolean;
  error?: string;
}

const OPEN_SESSION: AccessSession = { enabled: false, kind: "open", phone: "" };

let cached: AccessSession | null = null;
let inflight: Promise<AccessSession> | null = null;
// 一次性提示（例如链接无效），读取后即清空，避免污染地址栏。
let notice: string | null = null;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of [...listeners]) listener();
}

function setSnapshot(session: AccessSession | null): void {
  if (cached === session) return;
  cached = session;
  emit();
}

export function subscribeAccessSession(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function accessSessionSnapshot(): AccessSession | null {
  return cached;
}

/**
 * 组件里同步读取访问身份。`beforeLoad` 在渲染前已经探测过，所以正常路径下这里
 * 直接拿到结果；探测未跑完（或直接渲染组件）时为 `null`，按「未启用」处理。
 */
export function useAccessSession(): AccessSession | null {
  return useSyncExternalStore(
    subscribeAccessSession,
    accessSessionSnapshot,
    accessSessionSnapshot,
  );
}

export function resetAccessSessionCache(): void {
  inflight = null;
  setSnapshot(null);
}

export function setAccessNotice(value: string | null): void {
  notice = value;
}

export function takeAccessNotice(): string | null {
  const value = notice;
  notice = null;
  return value;
}

function normalizeKind(raw: unknown, enabled: boolean): AccessSessionKind {
  if (raw === "client" || raw === "internal" || raw === "none") return raw;
  return enabled ? "none" : "open";
}

export async function fetchAccessSession(): Promise<AccessSession> {
  if (cached) return cached;
  if (inflight) return inflight;
  inflight = (async () => {
    try {
      const body = await api
        .get("/api/v1/access/session")
        .json<{ ok?: boolean; data?: { enabled?: unknown; kind?: unknown; phone?: unknown } }>();
      const data = body?.data ?? {};
      const enabled = Boolean(data.enabled);
      const session: AccessSession = {
        enabled,
        kind: normalizeKind(data.kind, enabled),
        phone: typeof data.phone === "string" ? data.phone : "",
      };
      setSnapshot(session);
      return session;
    } catch {
      // 探测失败（旧后端 404、网络抖动）按「未启用」处理：宁可放过，不要
      // 把用户永久挡在门外；真正的隔离在服务端。
      setSnapshot(OPEN_SESSION);
      return OPEN_SESSION;
    } finally {
      inflight = null;
    }
  })();
  return inflight;
}

function errorMessage(error: unknown, fallback: string): string {
  // ky 2.x pre-parses the error body into `error.data` (and consumes the
  // response stream), so `error.response.json()`/`.clone()` is not usable.
  const data = (error as { data?: unknown } | undefined)?.data;
  if (data && typeof data === "object") {
    const record = data as { error?: unknown; detail?: unknown };
    if (typeof record.error === "string" && record.error.trim()) return record.error;
    if (typeof record.detail === "string" && record.detail.trim()) return record.detail;
  }
  if (typeof data === "string" && data.trim()) return data;
  return fallback;
}

/** 用专属链接里的手机号 + 校验码换取客户会话 cookie。 */
export async function activateClientSession(
  phone: string,
  code: string,
): Promise<AccessActionResult> {
  try {
    await api
      .post("/api/v1/access/client-session", { json: { phone, code } })
      .json();
    resetAccessSessionCache();
    return { ok: true };
  } catch (error) {
    return { ok: false, error: errorMessage(error, "访问链接无效") };
  }
}

/** 用内部访问口令换取内部会话 cookie。 */
export async function activateInternalSession(key: string): Promise<AccessActionResult> {
  try {
    await api.post("/api/v1/access/internal-session", { json: { key } }).json();
    resetAccessSessionCache();
    return { ok: true };
  } catch (error) {
    return { ok: false, error: errorMessage(error, "内部访问口令不正确") };
  }
}

export async function logoutAccessSession(): Promise<void> {
  try {
    await api.post("/api/v1/access/logout");
  } catch {
    // 退出是恢复动作：网络失败也必须清掉前端状态。
  }
  resetAccessSessionCache();
}

/** `13800000000` → `138****0000`；非 11 位原样返回。 */
export function maskPhone(phone: string): string {
  const text = String(phone ?? "").trim();
  if (text.length !== 11) return text;
  return `${text.slice(0, 3)}****${text.slice(-4)}`;
}
