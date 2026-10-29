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

export async function startStudioIngest(
  project: string,
  filename: string,
  spineTemplate: SpineTemplate,
): Promise<TaskResponse> {
  // `ingest/start` 的失败是 HTTP 200 + `{ok:false}`（格式闸门、文件缺失…），所以
  // 必须看 body 里的 ok，而不是等 ky 抛错。
  const response = await jsonWithBackendError<
    TaskResponse | IngestStartBlocked
  >(
    api.post(p`api/v1/projects/${project}/ingest/start`, {
      json: { filename, spine_template: spineTemplate },
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
 * 一键成片的预设：用户不需要回答「风格/语种/叙事人称」这类问题。
 *
 * 这几个键同时承担两个作用：`visual_style` 决定画面风格；`narration_style` /
 * `ethnicity` 会让后端认为项目「已配置」（`pipeline_status._user_has_configured`），
 * 否则自动流程会卡在「等待配置」上。取值与内部导入页的默认选项一致。
 */
export const STUDIO_RUN_CONFIG = {
  spine_template: "drama",
  visual_style: "chinese_period_drama",
  narration_style: "first_person",
  ethnicity: "Chinese",
} as const;

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
