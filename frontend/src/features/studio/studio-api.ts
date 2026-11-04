// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { api, uploadApi } from "@/lib/api";
import { jsonWithBackendError } from "@/lib/api-errors";
import { p } from "@/lib/api-path";
import {
  IngestFormatBlockedError,
  type FormatCheck,
  type UploadResult,
} from "@/lib/queries/ingest";
import type { ErrorResponse, OkResponse, TaskResponse } from "@/types/api";
import type { StudioRunConfig } from "@/lib/queries/director-auto";
import type { Task } from "@/types/task";
import type { SpineTemplate } from "@/types/project";

/**
 * 工作室的剧本导入链路（与内部捏料页同源，但不复用它的表单状态机——工作室要先
 * 「建项目 → 上传 → 导入」一气呵成，项目 id 是运行期才拿到的，用不了需要 project
 * 参数的 mutation hook）。
 *
 *   POST /projects              建项目（重名自动加后缀）
 *   POST .../ingest/upload      上传剧本（顺带拿到章节预览与格式体检）
 *   POST .../ingest/start       入队 ingest_fast（它自己会串 预处理 → 剧本处理）
 */

export interface CreatedProject {
  id: string;
  name: string;
}

/**
 * 把剧名/文件名派生成合法项目名。
 *
 * 项目名同时是**目录名**（`OUTPUT_DIR/<user>/<name>`），后端只接受 Unicode 字母、
 * 数字、下划线。URL 用的是项目 id，所以中文名是安全的，其它字符则必须清掉。
 */
export function deriveProjectName(raw: string): string {
  const withoutExtension = String(raw ?? "").replace(/\.[^./\\]+$/, "");
  const cleaned = withoutExtension.replace(/[^\p{L}\p{N}_]+/gu, "").slice(0, 56);
  if (cleaned && !cleaned.startsWith("_")) return cleaned;
  const fallback = cleaned.replace(/^_+/, "");
  if (fallback) return fallback;
  const now = new Date();
  const stamp =
    `${now.getFullYear()}${String(now.getMonth() + 1).padStart(2, "0")}` +
    `${String(now.getDate()).padStart(2, "0")}` +
    `${String(now.getHours()).padStart(2, "0")}` +
    `${String(now.getMinutes()).padStart(2, "0")}`;
  return `studio_${stamp}`;
}

async function createProjectOnce(name: string): Promise<CreatedProject> {
  return jsonWithBackendError<OkResponse<CreatedProject>>(
    api.post("api/v1/projects", { json: { name }, throwHttpErrors: false }),
  ).then((response) => response.data);
}

/**
 * 建项目；重名时按 `_2`、`_3`… 顺延。
 *
 * 后端对重名返回 409（项目名唯一），而客户端只给了剧名——同一个剧名重开一次工作
 * 室是很自然的动作，不该撞死在 409 上。
 */
export async function createStudioProject(
  rawName: string,
  { attempts = 9 }: { attempts?: number } = {},
): Promise<CreatedProject> {
  const base = deriveProjectName(rawName).slice(0, 60);
  let lastError: unknown = new Error("创建项目失败");
  for (let index = 1; index <= attempts; index += 1) {
    const candidate = index === 1 ? base : `${base}_${index}`;
    try {
      return await createProjectOnce(candidate);
    } catch (error) {
      lastError = error;
      const message = error instanceof Error ? error.message : String(error);
      // 只有「已存在」才顺延；其它错误（鉴权、磁盘、名称非法）立刻抛出。
      if (!/already exists|已存在/i.test(message)) throw error;
    }
  }
  throw lastError;
}

export async function uploadStudioScript(
  project: string,
  file: File,
  spineTemplate: SpineTemplate,
): Promise<UploadResult> {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("spine_template", spineTemplate);
  const response = await jsonWithBackendError<OkResponse<UploadResult> | ErrorResponse>(
    uploadApi.post(p`api/v1/projects/${project}/ingest/upload`, { body: formData }),
  );
  if (!response.ok) throw new Error(response.error);
  return response.data;
}

interface IngestStartBlocked extends ErrorResponse {
  error_type?: string;
  format_check?: FormatCheck;
}

/**
 * `ingest/start` 成功时的响应。
 *
 * `format_policy="warn"` 下后端会把「精品剧缺场景头」的体检结果一并带回
 * （`format_check`），供工作室作为提示展示，而不是当成错误拦住导入。
 */
export interface StudioIngestStarted extends TaskResponse {
  format_check?: FormatCheck | null;
}

export interface StartStudioIngestOptions {
  /**
   * 「精品剧」缺场景头时的策略。`warn`（默认）放行导入并把体检结果带回来展示，
   * `block` 交给后端拦住（内部捏料页的严格门禁）。
   */
  formatPolicy?: "block" | "warn";
}

export async function startStudioIngest(
  project: string,
  filename: string,
  spineTemplate: SpineTemplate,
  { formatPolicy = "warn" }: StartStudioIngestOptions = {},
): Promise<StudioIngestStarted> {
  // `ingest/start` 的失败是 HTTP 200 + `{ok:false}`（格式闸门、文件缺失…），所以
  // 必须看 body 里的 ok，而不是等 ky 抛错。
  const response = await jsonWithBackendError<
    StudioIngestStarted | IngestStartBlocked
  >(
    api.post(p`api/v1/projects/${project}/ingest/start`, {
      // 与内部导入页一致：带 ``spine_template`` 就必须带 ``rebuild``。后端
      // （``ingest.py`` 的「项目类型只能在重新导入时修改」守卫）只放行 rebuild 的
      // 改型请求，否则整条导入直接被拒、``ingest_fast`` 永远不入队，预处理也就
      // 从来没有跑的机会。工作室每次都是新建项目后首导，rebuild 是空操作。
      json: {
        filename,
        spine_template: spineTemplate,
        rebuild: true,
        // 缺场景头不拦：把格式问题降级为提示，导入照常入队并跑预处理。
        format_policy: formatPolicy,
      },
      throwHttpErrors: false,
    }),
  );
  if (!response.ok) {
    throw new IngestFormatBlockedError(
      response.error,
      response.error_type === "screenplay_format"
        ? response.format_check ?? null
        : null,
    );
  }
  return response;
}

/**
 * 工作室的三个选项（项目类型 / 视觉风格 / 解说风格 / 默认未指定人群）。
 *
 * 这些值先经 `PATCH /projects/{id}` 落到项目配置，再由工作台的自动开跑读回：
 * `visual_style` 决定画面风格；`narration_style` / `ethnicity` 会让后端认为项目
 * 「已配置」（`pipeline_status._user_has_configured`），否则自动流程会卡在
 * 「等待配置」上。取值与内部导入页的选项一致。
 */
export interface StudioChoices {
  spine_template: SpineTemplate;
  visual_style: string;
  narration_style: string;
  ethnicity: string;
}

/** 默认预设：用户不改也能直接开跑（项目类型默认精品剧）。 */
export const DEFAULT_STUDIO_CHOICES: StudioChoices = {
  spine_template: "drama",
  visual_style: "chinese_period_drama",
  narration_style: "first_person",
  ethnicity: "Chinese",
};

/**
 * 把用户选择（或项目配置）补成 `director-auto/start` 需要的运行配置；缺项回落
 * 到默认预设。
 */
export function studioRunConfig(
  choices: Partial<StudioChoices> | null | undefined,
): StudioRunConfig {
  return {
    spine_template: choices?.spine_template ?? DEFAULT_STUDIO_CHOICES.spine_template,
    visual_style: choices?.visual_style ?? DEFAULT_STUDIO_CHOICES.visual_style,
    narration_style:
      choices?.narration_style ?? DEFAULT_STUDIO_CHOICES.narration_style,
    ethnicity: choices?.ethnicity ?? DEFAULT_STUDIO_CHOICES.ethnicity,
  };
}

/**
 * 把工作室选择的视觉风格 / 解说风格 / 人群写进项目配置。
 *
 * `spine_template` 与 `aspect_ratio` 留给 `ingest/start` 写（它带 `rebuild` 守卫），
 * 这里只落「非导入型」设置，避免与那条守卫打架。
 */
export async function saveStudioProjectChoices(
  project: string,
  choices: Partial<StudioChoices>,
): Promise<void> {
  const body: Record<string, string> = {};
  if (choices.visual_style !== undefined) body.visual_style = choices.visual_style;
  if (choices.narration_style !== undefined) {
    body.narration_style = choices.narration_style;
  }
  if (choices.ethnicity !== undefined) body.ethnicity = choices.ethnicity;
  if (Object.keys(body).length === 0) return;
  await jsonWithBackendError<OkResponse<unknown>>(
    api.patch(p`api/v1/projects/${project}`, {
      json: body,
      throwHttpErrors: false,
    }),
  );
}

/** 导入链（ingest → 剧本预处理 → 剧本处理）是否已走完，可以开始一键成片。 */
export function isImportSettled(
  global:
    | {
        ingested?: boolean;
        screenplay_preprocess_pending?: boolean;
        script_process_pending?: boolean;
      }
    | null
    | undefined,
): boolean {
  if (!global?.ingested) return false;
  return !global.screenplay_preprocess_pending && !global.script_process_pending;
}

/**
 * 重做单个镜头：入队单镜视频生成。
 *
 * 服务端在一镜反复审核不过 / 重试耗尽时会**本地降级**该镜并继续（工作台顶部报错、
 * 逐镜标记）。客户改完提示词后点「重做该镜」，就用这个显式重跑那一镜。
 */
export async function regenerateStudioBeatVideo(
  project: string,
  episode: number,
  beat: number,
): Promise<{ taskId?: string; previousTaskId?: string }> {
  // 记下重做前该镜的任务 id，用来把「旧的那次失败」和「这次重做」区分开。
  const previousTaskId = (await fetchStudioBeatTask(project, episode, beat))?.task_id;
  const response = await jsonWithBackendError<OkResponse<unknown> | ErrorResponse>(
    api.post(
      p`api/v1/projects/${project}/episodes/${episode}/beats/${beat}/video`,
      { json: {}, throwHttpErrors: false },
    ),
  );
  if (!response.ok) throw new Error(response.error);
  const taskId = (response as { task_id?: string }).task_id;
  return { taskId, previousTaskId };
}

/** 查该镜当前的 `single_video` 任务（没有则返回 null）。 */
export async function fetchStudioBeatTask(
  project: string,
  episode: number,
  beat: number,
): Promise<Task | null> {
  const response = await api
    .get(p`api/v1/projects/${project}/tasks/single_video/${episode}`, {
      searchParams: { beat_num: String(beat) },
    })
    .json<OkResponse<Task | null>>();
  return response.data ?? null;
}

/**
 * 等这次重做的单镜任务跑到终态。
 *
 * 前置背景：`director-auto` 在一镜过不了时会把该镜**本地降级**并跳过、登记进
 * `blocked_shots`。客户点「重做该镜」后，我们**只在这镜确实生成成功时才清掉
 * 标记**——否则（又失败 / 超时）标记保留，避免「标记先清了、结果还是坏镜头」。
 *
 * - 只认 `single_video` 且（若有）任务 id 不等于重做前那个的任务，避开旧的那次失败。
 * - `completed` → 成功；`failed`/`cancelled` → 失败并带回错误原因。
 */
export async function waitForStudioBeatVideo(
  project: string,
  episode: number,
  beat: number,
  {
    taskId,
    previousTaskId,
    timeoutMs = 20 * 60 * 1000,
    intervalMs = 4000,
  }: {
    taskId?: string;
    previousTaskId?: string;
    timeoutMs?: number;
    intervalMs?: number;
  } = {},
): Promise<{ ok: true } | { ok: false; error: string }> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
    let task: Task | null = null;
    try {
      task = await fetchStudioBeatTask(project, episode, beat);
    } catch {
      continue; // 网络抖动：继续轮询
    }
    if (!task) continue;
    // 只看这次重做对应的任务：优先按 task_id 精确匹配，退而求其次要求「不是旧任务」。
    if (taskId) {
      if (task.task_id !== taskId) continue;
    } else if (previousTaskId && task.task_id === previousTaskId) {
      continue;
    }
    if (task.status === "completed") return { ok: true };
    if (task.status === "failed" || task.status === "cancelled") {
      return { ok: false, error: task.error || `该镜任务已${task.status}` };
    }
  }
  return { ok: false, error: "等待该镜生成超时，标记保留，请稍后刷新查看" };
}
