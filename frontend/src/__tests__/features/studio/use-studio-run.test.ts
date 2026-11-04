// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { describe, expect, it } from "vitest";

import { isBlockedPause, isVoicePolicyPause } from "@/features/studio/use-studio-run";

/**
 * 阻塞型暂停不能被当成普通硬暂停自动续跑。
 *
 * 现场 ``nn_1104_1``：确定性派发找不到可做的 stage，agent 也没写出新任务；客户端每
 * 2.5s 自动 ``/continue`` 一次，服务端把阶段重试预算清零后继续空转——客户只看到
 * 「制作中」，进度永远不动。识别出这类原因后必须停手，把原因交给客户。
 */
describe("isBlockedPause", () => {
  it("识别「未产生写任务」暂停", () => {
    expect(isBlockedPause("自动续跑未产生写任务")).toBe(true);
    expect(isBlockedPause("自动续跑未产生写任务（最后一次工具调用：x）")).toBe(true);
  });

  it("识别「未产生新的任务」暂停", () => {
    expect(isBlockedPause("自动续跑未产生新的任务")).toBe(true);
  });

  it("普通失败 / 空原因不算阻塞", () => {
    expect(isBlockedPause("")).toBe(false);
    expect(isBlockedPause(null)).toBe(false);
    expect(isBlockedPause(undefined)).toBe(false);
    expect(isBlockedPause("有任务失败：Seedance2 请求超时")).toBe(false);
  });
});

/**
 * 声线策略暂停没有 `[tag]`，只能靠 ``last_error`` 的特征子串识别；命中后工作室要摆出
 * 声线选择卡，**不能**自动续跑（否则会用默认策略把客户的选择权绕过去）。
 */
describe("isVoicePolicyPause", () => {
  it("识别后端 VOICE_POLICY_REQUIRED_ERROR", () => {
    expect(
      isVoicePolicyPause("缺少配音声线，且未选择声线策略（系统声线／自定义声线）"),
    ).toBe(true);
  });

  it("普通暂停 / 角色声线缺失不算声线策略暂停", () => {
    expect(isVoicePolicyPause("")).toBe(false);
    expect(isVoicePolicyPause(null)).toBe(false);
    expect(isVoicePolicyPause("Beat 08 角色声线缺失：伊卡")).toBe(false);
    expect(isVoicePolicyPause("自动续跑未产生写任务")).toBe(false);
  });
});

/**
 * 审核未过 / 卡住 / 前置补不齐：过去这类暂停会「暂停↔续跑」死循环。
 *
 * 现在**服务端**负责不再假死：单镜做不出来就用本地素材降级该镜并跳过继续
 * （`director_auto._block_shot_and_continue`），每次续跑都真的会往前走。所以客户端
 * 恢复「暂停即自动 /continue」，并靠服务端暴露的 `blocked_shots` 做顶部报错 + 逐镜
 * 标记——不再在客户端按关键词硬停（原先的 `isHumanDecisionPause` 已移除）。
 */
describe("hard-paused runs resume automatically", () => {
  it("审核类暂停不再被客户端当成「必须人工」而停手", () => {
    // 关键词直到后端都会命中，但客户端不据此停：只要不是阻塞型暂停 / 声线策略 / 闸门，
    // 就走自动续跑分支。这里用「不是 blocked、也不是 voice policy」间接断言。
    expect(isBlockedPause("内容审核未通过，需要人工处理")).toBe(false);
    expect(isVoicePolicyPause("内容审核未通过，需要人工处理")).toBe(false);
  });
});
