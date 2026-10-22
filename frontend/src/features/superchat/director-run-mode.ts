import type { TaskState } from "@/task-center/types";

export type DirectorRunMode = "manual_confirm" | "episode_auto" | "pipeline_auto";
export type DirectorVoicePolicy = "system" | "custom";
export type DirectorAutoConfirmationStage =
  | "awaiting_start"
  | "awaiting_confirmation"
  | "awaiting_intervention"
  | "confirmed";

export interface DirectorAutoRunState {
  mode: DirectorRunMode;
  confirmationStage: DirectorAutoConfirmationStage;
  activatedAt: number;
  episode: number | null;
  handledKeys: string[];
  voicePolicy: DirectorVoicePolicy | null;
  voiceChoiceRequired: boolean;
  voicePrerequisiteErrors: string[];
}

export type DirectorAutoTaskDecision =
  | { action: "ignore" }
  | { action: "continue"; episode: number; key: string }
  | { action: "pause"; episode: number; key: string }
  | { action: "complete"; episode: number; key: string };

const STORAGE_PREFIX = "director.episodeRunMode";
const MAX_HANDLED_KEYS = 80;

const MAINLINE_EPISODE_TASK_TYPES = new Set([
  "ingest_fast",
  "build_characters",
  "build_props",
  "build_episodes",
  "identity_planner",
  "character_portrait",
  "identity_image",
  "content_rewriter",
  "literal_script_writer",
  "script_writer",
  "build_scenes",
  "episode_scene_planner",
  "episode_prop_planner",
  "scene_reference_asset",
  "scene_pano_generation",
  "prop_reference_asset",
  "sketch_grid_generation",
  "sketch_generation",
  "ai_identity_detection",
  "global_optimize_video",
  "selected_regen",
  "render_plan",
  "system_voice_setup",
  "audio_generation_indextts2",
  "single_video",
  "compose_episode",
]);

export function isDirectorAutoPipelineTask(task: TaskState): boolean {
  return MAINLINE_EPISODE_TASK_TYPES.has(task.task_type);
}

function storageKey(project: string): string {
  return `${STORAGE_PREFIX}:${project.trim() || "home"}`;
}

export function defaultDirectorRunState(): DirectorAutoRunState {
  return {
    mode: "manual_confirm",
    confirmationStage: "awaiting_start",
    activatedAt: 0,
    episode: null,
    handledKeys: [],
    voicePolicy: null,
    voiceChoiceRequired: false,
    voicePrerequisiteErrors: [],
  };
}

export function loadDirectorRunState(project: string): DirectorAutoRunState {
  if (typeof window === "undefined") return defaultDirectorRunState();
  try {
    const parsed = JSON.parse(window.localStorage.getItem(storageKey(project)) || "null") as
      | Partial<DirectorAutoRunState>
      | null;
    if (!parsed || (parsed.mode !== "episode_auto" && parsed.mode !== "pipeline_auto")) {
      return defaultDirectorRunState();
    }
    const savedConfirmationStage = parsed.confirmationStage;
    return {
      mode: parsed.mode,
      // Stored auto runs from before the confirmation gate must not bypass
      // the new explicit second-confirmation requirement after an upgrade.
      confirmationStage:
        savedConfirmationStage === "awaiting_start"
        || savedConfirmationStage === "awaiting_confirmation"
        || savedConfirmationStage === "awaiting_intervention"
        || savedConfirmationStage === "confirmed"
          ? savedConfirmationStage
          : "awaiting_start",
      activatedAt: Number.isFinite(parsed.activatedAt) ? Number(parsed.activatedAt) : Date.now(),
      episode: Number.isInteger(parsed.episode) && Number(parsed.episode) > 0
        ? Number(parsed.episode)
        : null,
      handledKeys: Array.isArray(parsed.handledKeys)
        ? parsed.handledKeys.filter((item): item is string => typeof item === "string").slice(-MAX_HANDLED_KEYS)
        : [],
      voicePolicy: parsed.voicePolicy === "system" || parsed.voicePolicy === "custom"
        ? parsed.voicePolicy
        : null,
      voiceChoiceRequired: parsed.voiceChoiceRequired === true,
      voicePrerequisiteErrors: Array.isArray(parsed.voicePrerequisiteErrors)
        ? parsed.voicePrerequisiteErrors.filter((item): item is string => typeof item === "string").slice(0, 8)
        : [],
    };
  } catch {
    return defaultDirectorRunState();
  }
}

export function saveDirectorRunState(project: string, state: DirectorAutoRunState): void {
  if (typeof window === "undefined") return;
  // 手动模式 is written explicitly instead of clearing the record: with no
  // record at all meaning 「never chose」 (the panel opens on 一键成片), removing
  // the key would silently turn an explicit 手动模式 back into the default.
  // `loadDirectorRunState` normalizes a stored manual mode back to the plain
  // default state.
  window.localStorage.setItem(storageKey(project), JSON.stringify({
    ...state,
    handledKeys: state.handledKeys.slice(-MAX_HANDLED_KEYS),
  }));
}

export function activateDirectorEpisodeAuto(): DirectorAutoRunState {
  return {
    mode: "episode_auto",
    confirmationStage: "awaiting_start",
    activatedAt: Date.now(),
    episode: null,
    handledKeys: [],
    voicePolicy: null,
    voiceChoiceRequired: false,
    voicePrerequisiteErrors: [],
  };
}

export function activateDirectorProjectAuto(): DirectorAutoRunState {
  return {
    mode: "pipeline_auto",
    confirmationStage: "awaiting_start",
    activatedAt: Date.now(),
    episode: null,
    handledKeys: [],
    voicePolicy: null,
    voiceChoiceRequired: false,
    voicePrerequisiteErrors: [],
  };
}

/**
 * The run-mode state a project opens with.
 *
 * 一键成片 is the standing default: it is what the product is for, and a mode
 * on its own never dispatches anything — the user still has to send the start
 * command *and* pass the second confirmation. A stored choice always wins, so
 * an explicit 手动模式 is never overridden.
 */
export function initialDirectorRunState(project: string): DirectorAutoRunState {
  if (typeof window === "undefined") return activateDirectorProjectAuto();
  if (window.localStorage.getItem(storageKey(project)) === null) {
    return activateDirectorProjectAuto();
  }
  return loadDirectorRunState(project);
}

/**
 * State used when a run ends (completed/paused) on its own.
 *
 * The chosen mode is deliberately kept: silently dropping back to 手动模式 made
 * the panel look like it had ignored the user's selection. Only the
 * confirmation stage resets, so the next start still needs its explicit
 * confirmation.
 */
export function settledDirectorRunState(mode: DirectorRunMode): DirectorAutoRunState {
  return { ...defaultDirectorRunState(), mode };
}

export function awaitDirectorEpisodeAutoConfirmation(
  state: DirectorAutoRunState,
): DirectorAutoRunState {
  return {
    ...state,
    confirmationStage: "awaiting_confirmation",
  };
}

export function confirmDirectorEpisodeAuto(
  state: DirectorAutoRunState,
): DirectorAutoRunState {
  return {
    ...state,
    confirmationStage: "confirmed",
    activatedAt: Date.now(),
  };
}

function isAutoRunMode(mode: DirectorRunMode): boolean {
  return mode === "episode_auto" || mode === "pipeline_auto";
}

export function isDirectorEpisodeAutoConfirmed(state: DirectorAutoRunState): boolean {
  return isAutoRunMode(state.mode) && state.confirmationStage === "confirmed";
}

export function isDirectorEpisodeAutoSession(state: DirectorAutoRunState): boolean {
  return isAutoRunMode(state.mode)
    && (state.confirmationStage === "confirmed"
      || state.confirmationStage === "awaiting_intervention");
}

const AUTO_START_INTENT_RE = /(?:开始|启动|执行|推进|继续|确认|同意|可以(?:开始)?|开始吧|继续吧|确认吧)/u;
const AUTO_NEGATED_START_RE = /(?:不要|别|暂不|先不|取消|停止|暂停).{0,4}(?:开始|启动|执行|推进|继续|确认)/u;

export function isDirectorEpisodeAutoStartIntent(text: string): boolean {
  const normalized = text.trim();
  return normalized.length > 0
    && AUTO_START_INTENT_RE.test(normalized)
    && !AUTO_NEGATED_START_RE.test(normalized);
}

export function directorAutoConfirmationTransportText(
  text: string,
  options: { voiceChoiceRequired?: boolean; voiceErrors?: string[] } = {},
): string {
  const voiceInstruction = options.voiceChoiceRequired
    ? " Before starting, the user must choose a voice policy. Explicitly present both Chinese choices: " +
      "‘1）缺失声线由捏导自动匹配系统声线；2）我会到「捏塘」上传或录制自定义声线。’ " +
      `${options.voiceErrors?.length ? `Known missing items: ${options.voiceErrors.join("；")}. ` : ""}` +
      "Ask the user to reply 系统声线 or 自定义声线; do not start yet."
    : " Then ask the user to reply 继续 or 确认 to start.";
  return `${text}\n\n[DRAMACLAW_RUN_MODE_CONFIRMATION]\n`+
    "The user selected 本集自动 but has not completed the required second confirmation. " +
    "Do not call any write, generation, or mutation tool in this turn. Reply briefly in Chinese: " +
    "state that 本集自动 will safely advance the current episode until the final composition is " +
    "complete, and will pause on failure, unmet prerequisites, destructive or ambiguous actions, " +
    `or choices that require the user.${voiceInstruction}\n` +
    "[/DRAMACLAW_RUN_MODE_CONFIRMATION]" +
    DIRECTOR_KEYWORD_FORMAT_RULE;
}

export function resolveDirectorVoicePolicy(text: string): DirectorVoicePolicy | null {
  const normalized = text.trim();
  const system = /(?:系统声(?:线|音|一)|系统配音|捏导匹配|捏塘匹配)/u.test(normalized);
  const custom = /(?:自定义声(?:线|音)|上传(?:或录制)?|录制声线|声音采样|自定义配音)/u.test(normalized);
  if (system === custom) {
    // The confirmation prompt offers "1）系统声线 2）自定义声线"; accept the
    // bare option numbers too, otherwise a reply of "1" is missent as chat.
    const numeric = normalized.match(/^[（(]?\s*([12１２])\s*[）)．.、]?/u);
    if (numeric) {
      return numeric[1] === "1" || numeric[1] === "１" ? "system" : "custom";
    }
    return null;
  }
  return system ? "system" : "custom";
}

export function emphasizeDirectorVoiceChoiceLabels(text: string): string {
  if (
    !text.includes("系统声线")
    || !text.includes("自定义声线")
    || !/(?:请回复|请选择|你选哪种|选一个)/u.test(text)
  ) {
    return text;
  }
  const labels = ["系统声线", "自定义声线"] as const;
  return labels.reduce((current, label, index) => {
    const placeholder = `\uE000director-voice-${index}\uE001`;
    return current
      .split(`**${label}**`).join(placeholder)
      .split(label).join(`**${label}**`)
      .split(placeholder).join(`**${label}**`);
  }, text);
}

export type DirectorKeywordTone = "destructive" | "warning" | "primary" | "step";

/**
 * 捏导回复里的动作关键字分色表。按「危险 → 需注意 → 常规」排序，
 * 一个词只归属第一档。色值全部复用 DESIGN.md 已有的语义色，不新增颜色。
 */
const DIRECTOR_KEYWORD_GROUPS: ReadonlyArray<readonly [DirectorKeywordTone, readonly string[]]> = [
  [
    "destructive",
    ["强制终止", "强制停止", "强制取消", "终止", "停止", "取消", "重置", "清空", "删除", "覆盖", "放弃"],
  ],
  ["warning", ["暂停", "重试", "重跑", "修改", "跳过", "回退", "撤销", "恢复"]],
  ["primary", ["开始", "确认", "继续", "执行", "生成", "提交", "保存", "应用", "启动"]],
];

const DIRECTOR_KEYWORD_INDEX: ReadonlyMap<string, DirectorKeywordTone> = new Map(
  DIRECTOR_KEYWORD_GROUPS.flatMap(([tone, words]) => words.map((word) => [word, tone] as const)),
);

/** 裸词匹配用的候选列表（长词优先，避免「终止」抢走「强制终止」）。 */
const DIRECTOR_BARE_KEYWORD_PATTERN = DIRECTOR_KEYWORD_GROUPS
  .flatMap(([, words]) => words)
  .sort((left, right) => right.length - left.length)
  .join("|");

const DIRECTOR_CJK_PATTERN = /[\u3400-\u9fff\uf900-\ufaff]/u;

/**
 * 判断一段文字（通常来自方括号标签）属于哪一档颜色。
 *
 * `bracketed` 为真时，没命中任何命令词的方括号内容按「步骤名」处理（中性高亮）；
 * 为假时返回 null，避免在自然语句里把普通词染色。
 */
export function directorKeywordTone(
  label: string,
  options: { bracketed?: boolean } = {},
): DirectorKeywordTone | null {
  const text = String(label ?? "").trim();
  if (!text) return null;
  const direct = DIRECTOR_KEYWORD_INDEX.get(text);
  if (direct) return direct;
  for (const [tone, words] of DIRECTOR_KEYWORD_GROUPS) {
    if (words.some((word) => text.includes(word))) return tone;
  }
  return options.bracketed ? "step" : null;
}

/**
 * 给助手消息里的方括号关键字加粗，让它走 `strong` 渲染器上色。
 *
 * 保留方括号本身（用户要求 `[终止]` 而不是「终止」），并且：
 * * 跳过 markdown 链接 `[文本](地址)`；
 * * 跳过已加粗的 `**[终止]**`，保证幂等；
 * * 跳过 `[x]`、`[1]` 这类英文/数字标签，避免破坏任务列表与脚注。
 */
export function emphasizeDirectorCommandKeywords(text: string): string {
  if (!text || (!text.includes("[") && !text.includes("【"))) return text;
  return text.replace(/(?<!\*)[【[]([^【】[\]\n]{1,16})[】\]](?!\()/gu, (match, label: string) => {
    const trimmed = String(label ?? "").trim();
    if (!trimmed) return match;
    if (!DIRECTOR_CJK_PATTERN.test(trimmed) && !DIRECTOR_KEYWORD_INDEX.has(trimmed)) return match;
    return `**${match}**`;
  });
}

export interface DirectorKeywordSegment {
  text: string;
  tone: DirectorKeywordTone | null;
}

/**
 * 把一段纯文本切成「普通文字 / 上色关键字」片段。
 *
 * 用于 toast、按钮、弹窗这类受控文案：`bare` 为真时连不带方括号的关键字
 * （例如按钮上的「终止当前任务」）也会被上色，方括号本身不会被去掉。
 */
export function directorKeywordSegments(
  text: string,
  options: { bare?: boolean } = {},
): DirectorKeywordSegment[] {
  const value = String(text ?? "");
  if (!value) return [];
  const pattern = options.bare
    ? new RegExp(`[【[]([^【】[\\]\\n]{1,16})[】\\]]|(${DIRECTOR_BARE_KEYWORD_PATTERN})`, "gu")
    : new RegExp("[【[]([^【】[\\]\\n]{1,16})[】\\]]", "gu");
  const segments: DirectorKeywordSegment[] = [];
  let cursor = 0;
  for (const match of value.matchAll(pattern)) {
    const index = match.index ?? 0;
    if (index > cursor) segments.push({ text: value.slice(cursor, index), tone: null });
    const bracketed = match[1] ?? null;
    const bare = match[2] ?? null;
    const label = bracketed ?? bare ?? "";
    const tone = directorKeywordTone(label, { bracketed: bracketed !== null });
    if (tone && bracketed === null && !DIRECTOR_CJK_PATTERN.test(label)) {
      segments.push({ text: match[0], tone: null });
    } else if (tone) {
      segments.push({ text: match[0], tone });
    } else {
      segments.push({ text: match[0], tone: null });
    }
    cursor = index + match[0].length;
  }
  if (cursor < value.length) segments.push({ text: value.slice(cursor), tone: null });
  return segments;
}

/**
 * 让模型把动作关键字写成方括号形式，前端才能按类型上色。
 *
 * 用户要求「模型自己生成的指令话术也要覆盖」，所以这条规则挂在每一轮对话的
 * transport 文本上，而不是只改按钮文案。
 */
export const DIRECTOR_KEYWORD_FORMAT_RULE =
  "\n\n[DRAMACLAW_KEYWORD_FORMAT]\n" +
  "回复里凡是要用户点击、确认或选择的动作关键字，一律用方括号包起来（保留方括号本身），" +
  "例如 [开始]、[确认]、[继续]、[执行]、[重试]、[暂停]、[终止]；" +
  "步骤名同样写成 [步骤名]，例如 [生成首帧草图]。不要用引号、书名号或纯加粗代替方括号。" +
  "前端会按类型上色：危险操作（[终止][停止][取消][重置] 等）显示警示色，" +
  "需注意（[暂停][重试][修改][跳过] 等）显示提醒色，常规操作显示强调色，步骤名显示中性高亮。" +
  "按钮文案不用额外加方括号，写关键字本身即可。\n" +
  "[/DRAMACLAW_KEYWORD_FORMAT]";

export function directorAutoVoiceChoiceTransportText(text: string): string {
  return `${text}\n\n[DRAMACLAW_VOICE_POLICY_REQUIRED]\n`+
    "Do not start episode auto and do not call generation tools. The user's voice policy is still " +
    "unclear. Ask only: ‘请选择：1）缺失声线由捏导自动匹配系统声线；2）我会到「捏塘」上传或录制自定义声线。’ " +
    "Ask them to reply 系统声线 or 自定义声线.\n[/DRAMACLAW_VOICE_POLICY_REQUIRED]" +
    DIRECTOR_KEYWORD_FORMAT_RULE;
}

export function markDirectorAutoHandled(
  state: DirectorAutoRunState,
  key: string,
  episode?: number,
): DirectorAutoRunState {
  return {
    ...state,
    episode: state.episode ?? (episode && episode > 0 ? episode : null),
    handledKeys: [...state.handledKeys.filter((item) => item !== key), key].slice(-MAX_HANDLED_KEYS),
  };
}

export function directorAutoRunTransportText(text: string): string {
  return `${text}\n\n[DRAMACLAW_RUN_MODE]\nmode=episode_auto\n`+
    "The user enabled 本集自动 in the outer 捏导 UI. Treat this as approval to select and start " +
    "the next safe mainline step without asking for per-step confirmation. Keep the one-write-per-turn " +
    "limit. Before any write, inspect the current artifact and the latest task for that exact step. " +
    "If the artifact is merely missing and prerequisites are ready, start its generation. Do not let " +
    "an unrelated or superseded historical failed task block a missing safe step. Pause only when the " +
    "latest attempt for the current required step failed or was cancelled. Stop for destructive " +
    "overwrite/delete, missing voice choice, ambiguity, or unmet prerequisites. Never describe one " +
    "task as failed and then silently proceed as though that same attempt succeeded. " +
    "If this user-authored message may change, redo, replace, or reconfigure project output, do not " +
    "perform that mutation yet. First call dramaclaw_control_episode_auto(action='suspend', reason=...) " +
    "and ask the user to confirm the intended change. Questions, explanations, and progress checks do " +
    "not require suspension. If the user explicitly asks to stop automatic production, call " +
    "dramaclaw_control_episode_auto(action='pause'). Never cancel an already queued or running media " +
    "task unless the user separately and explicitly confirms cancellation. This mode never " +
    "applies Agent-credit billing. The durable auto coordinator is already running now: do not merely " +
    "describe what 本集自动 will do in the future. Inspect current state, report the concrete active " +
    "task or start the next safe task, and say clearly that automatic production has started.\n" +
    "[/DRAMACLAW_RUN_MODE]" +
    DIRECTOR_KEYWORD_FORMAT_RULE;
}

export function directorPipelineAutoConfirmationTransportText(
  text: string,
  options: { voiceChoiceRequired?: boolean; voiceErrors?: string[] } = {},
): string {
  const voiceInstruction = options.voiceChoiceRequired
    ? " Before starting, the user must choose a voice policy. Explicitly present both Chinese choices: " +
      "‘1）缺失声线由捏导自动匹配系统声线；2）我会到「捏塘」上传或录制自定义声线。’ " +
      `${options.voiceErrors?.length ? `Known missing items: ${options.voiceErrors.join("；")}. ` : ""}` +
      "Ask the user to reply 系统声线 or 自定义声线; do not start yet."
    : " Then ask the user to reply 继续 or 确认 to start.";
  return `${text}\n\n[DRAMACLAW_RUN_MODE_CONFIRMATION]\n`+
    "The user selected 一键成片 but has not completed the required second confirmation. " +
    "Do not call any write, generation, or mutation tool in this turn. Reply briefly in Chinese: " +
    "state that 一键成片 will automatically prepare the whole project (characters, episodes, " +
    "portraits) and produce every unfinished episode through final composition, and will pause on " +
    "failure, unmet prerequisites, destructive or ambiguous actions, or choices that require the " +
    `user.${voiceInstruction}\n` +
    "[/DRAMACLAW_RUN_MODE_CONFIRMATION]" +
    DIRECTOR_KEYWORD_FORMAT_RULE;
}

export function directorPipelineAutoRunTransportText(text: string): string {
  return `${text}\n\n[DRAMACLAW_RUN_MODE]\nmode=pipeline_auto\n`+
    "The user enabled 一键成片 in the outer 捏导 UI. Treat this as approval to automatically produce " +
    "the whole project: complete global preparation (characters, episodes, portraits) and then every " +
    "unfinished episode through final composition, one safe mainline write per turn, without " +
    "per-step confirmation and without stopping between episodes. Pause only on a task failure or " +
    "cancellation, a missing required configuration, a missing voice policy when voice prerequisites " +
    "block audio, destructive actions, or a genuinely ambiguous choice. Keep the one-write-per-turn " +
    "limit. This mode never applies Agent-credit billing. The durable auto coordinator is already " +
    "running now: do not merely describe what 一键成片 will do in the future; inspect current state, " +
    "report the concrete active task or start the next safe task, and say clearly that automatic " +
    "production has started.\n[/DRAMACLAW_RUN_MODE]" +
    DIRECTOR_KEYWORD_FORMAT_RULE;
}

export function directorAutoUserMessageTransportText(text: string): string {
  return `${text}\n\n[DRAMACLAW_AUTO_USER_MESSAGE]\n`+
    "The durable backend already owns progression of the active outer 捏导 episode auto run. First " +
    "understand the user's actual intent; do not start the next mainline task merely because this " +
    "message arrived. For a question, explanation, result view, or progress/status request, answer or " +
    "read only and leave automatic progression running. If the run is actually paused on a blocker " +
    "the user has now cleared — missing voices, for example — call " +
    "dramaclaw_control_episode_auto(action='continue') with the user's voice_policy ('system' or " +
    "'custom') once they have chosen one, and acknowledge that automatic production resumed; a " +
    "'paused' run cannot be restarted with action='resume'. If the message may change, redo, replace, or " +
    "reconfigure project output, call dramaclaw_control_episode_auto(action='suspend', reason=...) " +
    "before asking for confirmation, and do not perform the mutation in this turn. If the user " +
    "explicitly asks to stop or switch to manual mode, call " +
    "dramaclaw_control_episode_auto(action='pause'). Never cancel a queued or running generation task " +
    "unless the user explicitly confirms cancellation of that exact task.\n" +
    "[/DRAMACLAW_AUTO_USER_MESSAGE]" +
    DIRECTOR_KEYWORD_FORMAT_RULE;
}

export function directorAutoInterventionTransportText(text: string): string {
  return `${text}\n\n[DRAMACLAW_AUTO_INTERVENTION_CONFIRMATION]\n`+
    "The outer 捏导 episode auto run is suspended while waiting for the user's answer about a possible " +
    "modification. Interpret the answer semantically. If the user declines/cancels the modification or " +
    "asks to keep the original result and continue automatically, call " +
    "dramaclaw_control_episode_auto(action='resume') and acknowledge that automatic production resumed. " +
    "If the user confirms the modification, first call " +
    "dramaclaw_control_episode_auto(action='pause') to leave automatic mode, then handle at most one " +
    "confirmed project mutation under normal one-write-per-turn rules. If the answer is ambiguous, ask " +
    "one concise follow-up " +
    "and keep the run suspended. A queued or running generation task must continue by default and must " +
    "not be cancelled unless the user explicitly confirms cancelling that exact task.\n" +
    "[/DRAMACLAW_AUTO_INTERVENTION_CONFIRMATION]" +
    DIRECTOR_KEYWORD_FORMAT_RULE;
}

function taskTimestamp(task: TaskState): number {
  const value = Date.parse(task.updated_at || task.completed_at || task.created_at || "");
  return Number.isFinite(value) ? value : 0;
}

export function decideDirectorAutoTask(
  state: DirectorAutoRunState,
  task: TaskState,
  options: {
    key?: string;
    batchTerminal?: boolean;
    batchFailed?: boolean;
  } = {},
): DirectorAutoTaskDecision {
  if (!isDirectorEpisodeAutoConfirmed(state)) return { action: "ignore" };
  if (!isDirectorAutoPipelineTask(task)) return { action: "ignore" };
  const isProjectAuto = state.mode === "pipeline_auto";
  // Episode planning is a project-level task (episode=0). Once it finishes,
  // auto mode enters the newly planned pipeline at episode 1.
  const episode = task.episode > 0
    ? task.episode
    : state.episode ?? (task.task_type === "build_episodes" ? 1 : null);
  if (!episode) return { action: "ignore" };
  // A project-scope (one-click) run spans every episode, so it must not pin to
  // a single bound episode the way an episode-scope run does.
  if (!isProjectAuto && task.episode > 0 && state.episode !== null && state.episode !== task.episode) {
    return { action: "ignore" };
  }
  if (taskTimestamp(task) < state.activatedAt) return { action: "ignore" };

  const key = options.key || `${task.status}:${task.task_key || task.task_id}`;
  if (state.handledKeys.includes(key)) return { action: "ignore" };
  if (options.batchTerminal === false) return { action: "ignore" };
  if (options.batchFailed || task.status === "failed" || task.status === "cancelled") {
    return { action: "pause", episode, key };
  }
  if (task.status !== "completed") return { action: "ignore" };
  if (task.task_type === "compose_episode") {
    // A project run keeps going into the next episode; only the server decides
    // when the whole project is complete.
    return isProjectAuto
      ? { action: "continue", episode, key }
      : { action: "complete", episode, key };
  }
  return { action: "continue", episode, key };
}
