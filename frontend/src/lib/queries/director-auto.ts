// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { jsonWithBackendError } from "@/lib/api-errors";
import { p } from "@/lib/api-path";
import { queryKeys } from "@/lib/query-keys";
import type { ErrorResponse, OkResponse } from "@/types/api";

/**
 * 服务端持久化的「一键成片」运行（`chat/director-auto`）。
 *
 * 客户端工作室用它驱动整条生产线：启动一次项目级 deterministic run，之后轮询这个
 * 状态，遇到闸门自动确认、遇到暂停自动续跑。字段名与后端 `DirectorAutoRun`
 * 的序列化一致（见 `chat/director_auto.py`）。
 */
export type DirectorAutoStatus =
  | "manual"
  | "running"
  | "awaiting_confirmation"
  | "paused"
  | "completed";

export type DirectorAutoGate = "character_images" | "identity_images" | "cost_estimate";
export type DirectorVoicePolicy = "system" | "custom";

/**
 * 一个「做不出来、已本地降级跳过」的镜头。
 *
 * 审核反复不过 / 重试耗尽时，服务端不再停整条 run，而是用本地素材降级该镜并继续
 * （见 `director_auto._block_shot_and_continue`），同时把它登记在这里，让工作台
 * 顶部醒目报错、逐镜标记，用户可以改完提示词后重做。
 */
export interface DirectorBlockedShot {
  episode: number;
  beat: number;
  stage: string;
  code: string;
  reason: string;
  degraded: boolean;
  at: string;
}

export interface DirectorAutoRunPayload {
  status: DirectorAutoStatus;
  episode: number | null;
  run_id: string | null;
  activated_at?: string | null;
  voice_policy?: DirectorVoicePolicy | null;
  scope?: "episode" | "project";
  driver?: "agent" | "deterministic" | null;
  target_episodes?: number;
  last_error?: string | null;
  /** 暂停在一个内联图片闸门上时的闸门名。 */
  gate?: DirectorAutoGate | null;
  /** 做不出来、已本地降级跳过的镜头（工作台据此报错与逐镜标记）。 */
  blocked_shots?: DirectorBlockedShot[] | null;
}

/** 一键成片要预置的项目配置：用户在工作室里不需要回答这些问题。 */
export interface StudioRunConfig {
  spine_template?: string;
  visual_style?: string;
  narration_style?: string;
  ethnicity?: string;
  aspect_ratio?: string;
  add_subtitles?: boolean;
  [key: string]: unknown;
}

export function useDirectorAutoRun(
  project: string,
  options: { enabled?: boolean; refetchIntervalMs?: number } = {},
) {
  return useQuery({
    queryKey: queryKeys.directorAutoRun(project),
    queryFn: ({ signal }) =>
      api
        .get(p`api/v1/projects/${project}/chat/director-auto`, { signal })
        .json<OkResponse<DirectorAutoRunPayload>>(),
    enabled: !!project && (options.enabled ?? true),
    // 运行中的 run 需要跟着它走；停下来的 run 不该继续打接口。
    refetchInterval: (query) => {
      const status = query.state.data?.data?.status;
      if (status === "running" || status === "awaiting_confirmation") {
        return options.refetchIntervalMs ?? 3000;
      }
      return false;
    },
  });
}

/** 启动项目级一键成片（deterministic：按产物就绪直接派下一阶段）。 */
export function useStartProjectAutoRun(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (params: {
      voicePolicy?: DirectorVoicePolicy | null;
      targetEpisodes?: number;
      config?: StudioRunConfig;
    }) => {
      const response = await jsonWithBackendError<
        OkResponse<DirectorAutoRunPayload> | ErrorResponse
      >(
        api.post(p`api/v1/projects/${project}/chat/director-auto/start`, {
          json: {
            episode: 1,
            voice_policy: params.voicePolicy ?? null,
            scope: "project",
            target_episodes: params.targetEpisodes ?? 0,
            driver: "deterministic",
            config: params.config,
          },
          throwHttpErrors: false,
        }),
      );
      if (!response.ok) throw new Error(response.error);
      return response;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.directorAutoRun(project) });
    },
  });
}

/** 回答一个内联图片闸门（角色形象图 / 身份图）：让暂停的 run 继续。 */
export function useConfirmDirectorGate(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (params: { gate: DirectorAutoGate; episode: number }) => {
      const response = await jsonWithBackendError<
        OkResponse<DirectorAutoRunPayload> | ErrorResponse
      >(
        api.post(
          p`api/v1/projects/${project}/chat/director-auto/gates/${params.gate}/confirm`,
          { json: { episode: params.episode }, throwHttpErrors: false },
        ),
      );
      if (!response.ok) throw new Error(response.error);
      return response;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.directorAutoRun(project) });
    },
  });
}

/** 硬暂停（非闸门）后继续：带上声线策略，避免再次卡在同一步。 */
export function useContinueDirectorAutoRun(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (params: { voicePolicy?: DirectorVoicePolicy } = {}) => {
      const response = await jsonWithBackendError<
        OkResponse<DirectorAutoRunPayload> | ErrorResponse
      >(
        api.post(p`api/v1/projects/${project}/chat/director-auto/continue`, {
          json: params.voicePolicy ? { voice_policy: params.voicePolicy } : {},
          throwHttpErrors: false,
        }),
      );
      if (!response.ok) throw new Error(response.error);
      return response;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.directorAutoRun(project) });
    },
  });
}

/** 用户主动暂停（工作室里用来「先停下」）。 */
export function usePauseDirectorAutoRun(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      await api.post(p`api/v1/projects/${project}/chat/director-auto/pause`).json<unknown>();
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.directorAutoRun(project) });
    },
  });
}

/**
 * 重做某个被降级/跳过的镜头后，清掉它在 run 上的阻塞标记。
 *
 * 「重做」本身的入队由调用方显式发起（单镜视频生成）；这个 hook 只负责让工作台的
 * 错误条不再挂着那面已经重做的红牌。
 */
export function useClearBlockedShot(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (params: { episode: number; beat: number }) => {
      const response = await jsonWithBackendError<
        OkResponse<DirectorAutoRunPayload> | ErrorResponse
      >(
        api.post(
          p`api/v1/projects/${project}/chat/director-auto/blocked/${params.beat}/clear`,
          { json: { episode: params.episode }, throwHttpErrors: false },
        ),
      );
      if (!response.ok) throw new Error(response.error);
      return response;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.directorAutoRun(project) });
    },
  });
}
