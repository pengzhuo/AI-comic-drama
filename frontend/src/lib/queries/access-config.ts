// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { queryKeys } from "@/lib/query-keys";

/**
 * 访问设置（内部端「盾牌」页）的数据层。
 *
 * 后端只回**掩码**，永不回明文密钥；写入走同一份配置文件的热更新（`/data/studio-access.secret`），
 * 不需要重启服务。这里只做「取状态 / 改密钥 / 发链接 / 开关门禁」四件事，并把
 * 服务端的校验文案原样带回给页面显示。
 */

export type AccessSecretSource = "env" | "file" | "none";

export interface AccessSecretState {
  /** 是否已配置（未配置时页面按「未设置」展示）。 */
  configured: boolean;
  /** 形如 `L8OE…45ww`；后端保证不含明文。 */
  mask: string;
  /** 取值来源：环境变量 / 配置文件 / 未配置。 */
  source: AccessSecretSource;
  /** 由环境变量提供时为 true——改文件不生效，页面据此禁用输入。 */
  env_locked: boolean;
}

export interface AccessConfig {
  /** 访问验证是否生效（客户密钥存在且没有被 `gate=off` 关掉）。 */
  enabled: boolean;
  /** 配置文件里写了 `gate=off`。 */
  gate_disabled: boolean;
  internal_key: AccessSecretState;
  client_secret: AccessSecretState;
  file: {
    path: string;
    exists: boolean;
    updated_at: string | null;
  };
}

export interface AccessConfigResult {
  ok: boolean;
  error?: string;
  config?: AccessConfig;
  /** 服务端对本次修改的影响说明，原样展示。 */
  notices: string[];
}

export interface AccessLinkResult {
  ok: boolean;
  error?: string;
  phone?: string;
  /** 工作台入口 `/studio` 专属链接。 */
  link?: string;
  /** 捏画入口 `/canvas` 专属链接（同一 code，只换路径）。 */
  canvasLink?: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseSecretState(value: unknown): AccessSecretState {
  const record = isRecord(value) ? value : {};
  const source = record.source;
  return {
    configured: record.configured === true,
    mask: typeof record.mask === "string" ? record.mask : "",
    source: source === "env" || source === "file" ? source : "none",
    env_locked: record.env_locked === true,
  };
}

function parseNotices(body: unknown): string[] {
  if (!isRecord(body) || !Array.isArray(body.notices)) return [];
  return body.notices.filter((entry): entry is string => typeof entry === "string");
}

/** 后端 schema 不认识就抛错，免得页面拿着 `undefined` 渲染出误导信息。 */
export function parseAccessConfig(body: unknown): AccessConfig {
  const data = isRecord(body) && isRecord(body.data) ? body.data : null;
  if (!data || typeof data.enabled !== "boolean") {
    throw new Error("访问设置响应格式不正确");
  }
  const file = isRecord(data.file) ? data.file : {};
  return {
    enabled: data.enabled,
    gate_disabled: data.gate_disabled === true,
    internal_key: parseSecretState(data.internal_key),
    client_secret: parseSecretState(data.client_secret),
    file: {
      path: typeof file.path === "string" ? file.path : "",
      exists: file.exists === true,
      updated_at: typeof file.updated_at === "string" ? file.updated_at : null,
    },
  };
}

function errorMessage(error: unknown, fallback: string): string {
  // ky 2.x pre-parses the error body into `error.data` (and consumes the
  // response stream), so `error.response.json()` is not usable.
  const data = (error as { data?: unknown } | undefined)?.data;
  if (data && typeof data === "object") {
    const record = data as { error?: unknown; detail?: unknown };
    if (typeof record.error === "string" && record.error.trim()) return record.error;
    if (typeof record.detail === "string" && record.detail.trim()) return record.detail;
  }
  if (typeof data === "string" && data.trim()) return data;
  return fallback;
}

export async function fetchAccessConfig(): Promise<AccessConfig> {
  const body = await api.get("/api/v1/access/config", { retry: 0 }).json<unknown>();
  return parseAccessConfig(body);
}

export function useAccessConfig(enabled = true) {
  return useQuery({
    queryKey: queryKeys.accessConfig(),
    queryFn: fetchAccessConfig,
    enabled,
    // 设置页永远要看最新状态：轮换过密钥后旧缓存会误导操作者。
    staleTime: 0,
    retry: false,
  });
}

export interface SaveAccessConfigInput {
  /** 留空表示不改这一项。 */
  internalKey?: string;
  clientSecret?: string;
}

function useConfigMutation<TInput>(request: (input: TInput) => Promise<unknown>) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: TInput): Promise<AccessConfigResult> => {
      try {
        const body = await request(input);
        return {
          ok: true,
          config: parseAccessConfig(body),
          notices: parseNotices(body),
        };
      } catch (error) {
        return { ok: false, error: errorMessage(error, "保存失败"), notices: [] };
      }
    },
    onSuccess: (result) => {
      // The write response already carries the new status: seed the cache with
      // it instead of paying for another round trip.
      if (result.config) queryClient.setQueryData(queryKeys.accessConfig(), result.config);
    },
  });
}

export function useSaveAccessConfig() {
  return useConfigMutation((input: SaveAccessConfigInput) =>
    api
      .post("/api/v1/access/config", {
        json: {
          internal_key: input.internalKey ?? "",
          client_secret: input.clientSecret ?? "",
        },
      })
      .json<unknown>(),
  );
}

export interface SetAccessGateInput {
  enabled: boolean;
  /** 关闭门禁时必填：当前内部口令。 */
  confirmKey?: string;
  /** 重新开启且尚无客户密钥时可一并提交。 */
  internalKey?: string;
  clientSecret?: string;
}

export function useSetAccessGate() {
  return useConfigMutation((input: SetAccessGateInput) =>
    api
      .post("/api/v1/access/gate", {
        json: {
          enabled: input.enabled,
          confirm_key: input.confirmKey ?? "",
          internal_key: input.internalKey ?? "",
          client_secret: input.clientSecret ?? "",
        },
      })
      .json<unknown>(),
  );
}

export interface GenerateAccessLinkInput {
  phone: string;
  /** 可选：捏画链接直达的项目 id（留空则落到 `/canvas` 项目列表）。 */
  project?: string;
}

export function useGenerateAccessLink() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: GenerateAccessLinkInput): Promise<AccessLinkResult> => {
      try {
        const body = await api
          .post("/api/v1/access/config/link", {
            json: { phone: input.phone, project: input.project ?? "" },
          })
          .json<unknown>();
        const data = isRecord(body) && isRecord(body.data) ? body.data : null;
        const link = data && typeof data.link === "string" ? data.link : "";
        if (!link) return { ok: false, error: "服务端未返回链接" };
        return {
          ok: true,
          link,
          canvasLink: data && typeof data.canvas_link === "string" ? data.canvas_link : undefined,
          phone: typeof data?.phone === "string" ? data.phone : input.phone,
        };
      } catch (error) {
        return { ok: false, error: errorMessage(error, "生成链接失败") };
      }
    },
    onSuccess: (result) => {
      // 生成链接不改配置，但顺手让状态新鲜一点（例如刚被轮换过密钥）。
      if (result.ok) void queryClient.invalidateQueries({ queryKey: queryKeys.accessConfig() });
    },
  });
}
