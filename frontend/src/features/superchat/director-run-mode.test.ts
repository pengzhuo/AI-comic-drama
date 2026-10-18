import { beforeEach, describe, expect, it, vi } from "vitest";

import type { TaskState } from "@/task-center/types";
import {
  activateDirectorEpisodeAuto,
  activateDirectorProjectAuto,
  awaitDirectorEpisodeAutoConfirmation,
  confirmDirectorEpisodeAuto,
  decideDirectorAutoTask,
  defaultDirectorRunState,
  directorAutoConfirmationTransportText,
  directorAutoInterventionTransportText,
  directorAutoRunTransportText,
  directorAutoUserMessageTransportText,
  directorAutoVoiceChoiceTransportText,
  directorPipelineAutoConfirmationTransportText,
  directorPipelineAutoRunTransportText,
  directorKeywordSegments,
  directorKeywordTone,
  emphasizeDirectorCommandKeywords,
  emphasizeDirectorVoiceChoiceLabels,
  initialDirectorRunState,
  isDirectorEpisodeAutoSession,
  isDirectorEpisodeAutoStartIntent,
  loadDirectorRunState,
  markDirectorAutoHandled,
  resolveDirectorVoicePolicy,
  saveDirectorRunState,
  settledDirectorRunState,
} from "./director-run-mode";

function task(overrides: Partial<TaskState> = {}): TaskState {
  return {
    task_key: "single_video/project/8/1",
    task_id: "task-1",
    task_type: "single_video",
    username: "alice",
    project: "demo",
    episode: 8,
    beat_num: 1,
    scope: null,
    status: "completed",
    progress: 1,
    current_task: "完成",
    result: null,
    error: null,
    logs: [],
    created_at: "2026-08-13T10:00:00Z",
    updated_at: "2026-08-13T10:05:00Z",
    completed_at: "2026-08-13T10:05:00Z",
    ...overrides,
  };
}

describe("director episode auto mode", () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.spyOn(Date, "now").mockReturnValue(Date.parse("2026-08-13T10:01:00Z"));
  });

  it("persists auto mode per project and stores an explicit manual choice", () => {
    const active = activateDirectorEpisodeAuto();
    saveDirectorRunState("project-a", active);
    expect(loadDirectorRunState("project-a").mode).toBe("episode_auto");
    expect(loadDirectorRunState("project-b").mode).toBe("manual_confirm");

    saveDirectorRunState("project-a", defaultDirectorRunState());
    expect(loadDirectorRunState("project-a").mode).toBe("manual_confirm");
  });

  it("opens a project on 一键成片 until the user chooses otherwise", () => {
    expect(initialDirectorRunState("project-a").mode).toBe("pipeline_auto");
    // A stored choice always wins — an explicit 手动模式 is never overridden.
    saveDirectorRunState("project-a", defaultDirectorRunState());
    expect(initialDirectorRunState("project-a").mode).toBe("manual_confirm");
    saveDirectorRunState("project-b", activateDirectorEpisodeAuto());
    expect(initialDirectorRunState("project-b").mode).toBe("episode_auto");
  });

  it("keeps the selected mode when a run ends", () => {
    const settled = settledDirectorRunState("pipeline_auto");
    expect(settled.mode).toBe("pipeline_auto");
    // The next start still needs its explicit second confirmation.
    expect(settled.confirmationStage).toBe("awaiting_start");
    expect(settledDirectorRunState("manual_confirm").mode).toBe("manual_confirm");
  });

  it("continues a completed mainline task and binds its episode", () => {
    const state = confirmDirectorEpisodeAuto(activateDirectorEpisodeAuto());
    const decision = decideDirectorAutoTask(state, task());
    expect(decision.action).toBe("continue");
    if (decision.action !== "continue") return;
    const next = markDirectorAutoHandled(state, decision.key, decision.episode);
    expect(next.episode).toBe(8);
    expect(decideDirectorAutoTask(next, task()).action).toBe("ignore");
  });

  it("continues from project-level episode planning into episode one", () => {
    const state = confirmDirectorEpisodeAuto(activateDirectorEpisodeAuto());
    const decision = decideDirectorAutoTask(state, task({
      task_type: "build_episodes",
      episode: 0,
      task_key: "build_eps/project/0/0",
    }));
    expect(decision).toMatchObject({ action: "continue", episode: 1 });
  });

  it("pauses on failure and completes after episode composition", () => {
    const state = confirmDirectorEpisodeAuto(activateDirectorEpisodeAuto());
    expect(decideDirectorAutoTask(state, task({ status: "failed" })).action).toBe("pause");
    expect(decideDirectorAutoTask(
      state,
      task({ task_type: "compose_episode" }),
    ).action).toBe("complete");
  });

  it("ignores stale, unrelated, and different-episode tasks", () => {
    const state = {
      ...confirmDirectorEpisodeAuto(activateDirectorEpisodeAuto()),
      episode: 8,
    };
    expect(decideDirectorAutoTask(state, task({ task_type: "stage_asset" })).action).toBe("ignore");
    expect(decideDirectorAutoTask(state, task({ episode: 9 })).action).toBe("ignore");
    expect(decideDirectorAutoTask(
      state,
      task({ updated_at: "2026-08-13T09:59:00Z" }),
    ).action).toBe("ignore");
  });

  it("accepts episode-zero asset tasks only after the auto run is bound to an episode", () => {
    const unbound = confirmDirectorEpisodeAuto(activateDirectorEpisodeAuto());
    const assetTask = task({ task_type: "identity_image", episode: 0 });
    expect(decideDirectorAutoTask(unbound, assetTask).action).toBe("ignore");

    const bound = { ...unbound, episode: 8 };
    const decision = decideDirectorAutoTask(bound, assetTask);
    expect(decision).toMatchObject({ action: "continue", episode: 8 });
  });

  it("continues after a character portrait finishes for the bound episode", () => {
    const state = {
      ...confirmDirectorEpisodeAuto(activateDirectorEpisodeAuto()),
      episode: 1,
    };
    const decision = decideDirectorAutoTask(state, task({
      task_type: "character_portrait",
      episode: 0,
      scope: "character:苏糖:portrait",
      task_key: "character_portrait/project/0/苏糖",
    }));
    expect(decision).toMatchObject({ action: "continue", episode: 1 });
  });

  it("marks UI auto approval without mentioning Agent credits", () => {
    const text = directorAutoRunTransportText("继续下一步");
    expect(text).toContain("mode=episode_auto");
    expect(text).toContain("one-write-per-turn");
    expect(text).toContain("never applies Agent-credit billing");
    expect(text).toContain("already running now");
    expect(text).toContain("do not merely describe");
  });

  it("requires a start request and then a second confirmation before running", () => {
    const selected = activateDirectorEpisodeAuto();
    expect(selected.confirmationStage).toBe("awaiting_start");
    expect(decideDirectorAutoTask(selected, task()).action).toBe("ignore");

    const awaitingConfirmation = awaitDirectorEpisodeAutoConfirmation(selected);
    expect(awaitingConfirmation.confirmationStage).toBe("awaiting_confirmation");
    expect(decideDirectorAutoTask(awaitingConfirmation, task()).action).toBe("ignore");

    const confirmed = confirmDirectorEpisodeAuto(awaitingConfirmation);
    expect(confirmed.confirmationStage).toBe("confirmed");
    expect(decideDirectorAutoTask(confirmed, task()).action).toBe("continue");
  });

  it("recognizes positive start phrases but rejects negated ones", () => {
    expect(isDirectorEpisodeAutoStartIntent("开始生成第 1 集")).toBe(true);
    expect(isDirectorEpisodeAutoStartIntent("确认")).toBe(true);
    expect(isDirectorEpisodeAutoStartIntent("继续吧")).toBe(true);
    expect(isDirectorEpisodeAutoStartIntent("先不要开始")).toBe(false);
    expect(isDirectorEpisodeAutoStartIntent("看看当前状态")).toBe(false);
  });

  it("uses a confirmation-only transport instruction before activation", () => {
    const text = directorAutoConfirmationTransportText("开始");
    expect(text).toContain("required second confirmation");
    expect(text).toContain("Do not call any write, generation, or mutation tool");
    expect(text).toContain("继续 or 确认");
  });

  it("keeps a suspended intervention inside the same auto session", () => {
    const suspended = {
      ...confirmDirectorEpisodeAuto(activateDirectorEpisodeAuto()),
      confirmationStage: "awaiting_intervention" as const,
    };
    saveDirectorRunState("project-a", suspended);

    expect(loadDirectorRunState("project-a").confirmationStage).toBe("awaiting_intervention");
    expect(isDirectorEpisodeAutoSession(suspended)).toBe(true);
    expect(decideDirectorAutoTask(suspended, task()).action).toBe("ignore");
  });

  it("asks the agent to resume after a declined modification without cancelling tasks", () => {
    const text = directorAutoInterventionTransportText("不改了，继续自动");
    expect(text).toContain("action='resume'");
    expect(text).toContain("action='pause'");
    expect(text).toContain("must not be cancelled");
  });

  it("requires the agent to suspend before asking about a possible modification", () => {
    const text = directorAutoRunTransportText("这个镜头换成夜景呢");
    expect(text).toContain("action='suspend'");
    expect(text).toContain("do not perform that mutation yet");
  });

  it("does not treat ordinary user messages as automatic next-step commands", () => {
    const text = directorAutoUserMessageTransportText("现在做到哪了");
    expect(text).toContain("do not start the next mainline task merely because this message arrived");
    expect(text).toContain("read only");
    expect(text).toContain("action='suspend'");
  });

  it("requires an explicit voice policy when preflight finds missing voices", () => {
    const text = directorAutoConfirmationTransportText("开始", {
      voiceChoiceRequired: true,
      voiceErrors: ["解说声线缺失"],
    });
    expect(text).toContain("系统声线");
    expect(text).toContain("自定义声线");
    expect(text).toContain("解说声线缺失");
    expect(directorAutoVoiceChoiceTransportText("确认")).toContain("Do not start episode auto");
  });

  it("recognizes an explicit system or custom voice choice", () => {
    expect(resolveDirectorVoicePolicy("使用系统声线继续")).toBe("system");
    expect(resolveDirectorVoicePolicy("系统声一")).toBe("system");
    expect(resolveDirectorVoicePolicy("使用系统声音")).toBe("system");
    expect(resolveDirectorVoicePolicy("我去捏塘上传或录制")).toBe("custom");
    expect(resolveDirectorVoicePolicy("使用自定义声音")).toBe("custom");
    expect(resolveDirectorVoicePolicy("你说系统还是自定义？")).toBeNull();
  });

  it("persists and confirms the project-scope one-click run", () => {
    const active = activateDirectorProjectAuto();
    saveDirectorRunState("project-a", active);
    expect(loadDirectorRunState("project-a").mode).toBe("pipeline_auto");

    const confirmed = confirmDirectorEpisodeAuto(active);
    expect(isDirectorEpisodeAutoSession(confirmed)).toBe(true);
  });

  it("keeps a project-scope run spanning multiple episodes instead of pinning one", () => {
    const state = {
      ...confirmDirectorEpisodeAuto(activateDirectorProjectAuto()),
      episode: 8,
    };
    expect(decideDirectorAutoTask(state, task({ episode: 9 })).action).toBe("continue");
    // Composition ends the episode, not the whole project.
    expect(decideDirectorAutoTask(
      state,
      task({ task_type: "compose_episode", episode: 9 }),
    ).action).toBe("continue");
    // Global preparation tasks are also tracked.
    expect(decideDirectorAutoTask(
      state,
      task({ task_type: "build_characters", episode: 0 }),
    ).action).toBe("continue");
  });

  it("describes one-click production across global prep and every episode", () => {
    const text = directorPipelineAutoRunTransportText("开始");
    expect(text).toContain("mode=pipeline_auto");
    expect(text).toContain("without stopping between episodes");
    expect(text).toContain("one-write-per-turn");
    expect(text).not.toContain("mode=episode_auto");

    const confirmText = directorPipelineAutoConfirmationTransportText("开始");
    expect(confirmText).toContain("一键成片");
    expect(confirmText).toContain("Do not call any write, generation, or mutation tool");
  });

  it("accepts bare option numbers as a voice choice", () => {
    expect(resolveDirectorVoicePolicy("1")).toBe("system");
    expect(resolveDirectorVoicePolicy("1）")).toBe("system");
    expect(resolveDirectorVoicePolicy("（1）")).toBe("system");
    expect(resolveDirectorVoicePolicy("２")).toBe("custom");
    expect(resolveDirectorVoicePolicy("2. ")).toBe("custom");
  });

  it("emphasizes only the two labels inside a voice-choice prompt", () => {
    expect(emphasizeDirectorVoiceChoiceLabels(
      "1）系统声线；2）自定义声线。请回复 系统声线 或 自定义声线。",
    )).toBe(
      "1）**系统声线**；2）**自定义声线**。请回复 **系统声线** 或 **自定义声线**。",
    );
    expect(emphasizeDirectorVoiceChoiceLabels("当前使用系统声线。"))
      .toBe("当前使用系统声线。");
  });

  it("classifies bracketed keywords by risk level", () => {
    expect(directorKeywordTone("[终止]", { bracketed: true })).toBe("destructive");
    expect(directorKeywordTone("强制终止", { bracketed: true })).toBe("destructive");
    expect(directorKeywordTone("[暂停]", { bracketed: true })).toBe("warning");
    expect(directorKeywordTone("[重试]", { bracketed: true })).toBe("warning");
    expect(directorKeywordTone("[开始]", { bracketed: true })).toBe("primary");
    expect(directorKeywordTone("[生成首帧草图]", { bracketed: true })).toBe("primary");
    expect(directorKeywordTone("[系统声线]", { bracketed: true })).toBe("step");
    // 非方括号场景不做裸词染色，避免自然语句被误伤。
    expect(directorKeywordTone("终止", { bracketed: false })).toBe("destructive");
    expect(directorKeywordTone("今天天气不错", { bracketed: false })).toBeNull();
  });

  it("wraps bracketed keywords in markdown bold and keeps the brackets", () => {
    expect(emphasizeDirectorCommandKeywords("请回复 [继续] 或 [终止]。")).toBe(
      "请回复 **[继续]** 或 **[终止]**。",
    );
    expect(emphasizeDirectorCommandKeywords("【暂停】本集自动已暂停")).toBe(
      "**【暂停】**本集自动已暂停",
    );
    // 幂等：已加粗的不重复加粗。
    expect(emphasizeDirectorCommandKeywords("请回复 **[继续]**。")).toBe("请回复 **[继续]**。");
    // 不破坏 markdown 链接、任务列表与数字引用。
    expect(emphasizeDirectorCommandKeywords("见 [文档](https://example.com/a)。")).toBe(
      "见 [文档](https://example.com/a)。",
    );
    expect(emphasizeDirectorCommandKeywords("- [x] 已完成")).toBe("- [x] 已完成");
    expect(emphasizeDirectorCommandKeywords("引用 [1] 与 [2]。")).toBe("引用 [1] 与 [2]。");
  });

  it("splits controlled copy into colored keyword segments", () => {
    const segments = directorKeywordSegments("已终止当前任务", { bare: true });
    expect(segments).toEqual([
      { text: "已", tone: null },
      { text: "终止", tone: "destructive" },
      { text: "当前任务", tone: null },
    ]);
    expect(directorKeywordSegments("已选择本集自动；发送 [开始] 指令", { bare: true })).toEqual([
      { text: "已选择本集自动；发送 ", tone: null },
      { text: "[开始]", tone: "primary" },
      { text: " 指令", tone: null },
    ]);
    expect(directorKeywordSegments("没有任何关键字", { bare: true })).toEqual([
      { text: "没有任何关键字", tone: null },
    ]);
    expect(directorKeywordSegments("强制终止", { bare: true })).toEqual([
      { text: "强制终止", tone: "destructive" },
    ]);
  });

  it("tells the model to write keywords in brackets", () => {
    for (const text of [
      directorAutoRunTransportText("开始"),
      directorPipelineAutoRunTransportText("开始"),
      directorAutoUserMessageTransportText("怎么样了"),
    ]) {
      expect(text).toContain("[DRAMACLAW_KEYWORD_FORMAT]");
      expect(text).toContain("[终止]");
    }
  });
});
