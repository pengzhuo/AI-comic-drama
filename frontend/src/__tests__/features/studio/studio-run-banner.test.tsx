// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { StudioRunBanner } from "@/features/studio/studio-run";
import type { DirectorAutoRunPayload } from "@/lib/queries/director-auto";

/**
 * 停在收费闸门时横幅不能再给「继续制作」。
 *
 * 线上事故：报价卡出来后，横幅上还有一个可点的「继续制作」，点它走的是
 * `/director-auto/continue`，绕过了伴点预扣直接开跑花钱的阶段。
 */
const GATE_MESSAGE = "已完成分镜与制作报价，请确认报价后再继续生成画面与视频。";

const PAUSED_RUN: DirectorAutoRunPayload = {
  status: "paused",
  episode: 1,
  run_id: "run-1",
  gate: "cost_estimate",
};

function renderBanner(props: {
  resumeBlocked?: boolean;
  needsHuman?: string | null;
} = {}) {
  const onResume = vi.fn();
  render(
    <StudioRunBanner
      run={PAUSED_RUN}
      needsHuman={props.needsHuman === undefined ? GATE_MESSAGE : props.needsHuman}
      recoveries={0}
      onResume={onResume}
      resumePending={false}
      resumeBlocked={props.resumeBlocked}
    />,
  );
  return { onResume };
}

describe("StudioRunBanner", () => {
  it("收费闸门未确认时不渲染「继续制作」", () => {
    renderBanner({ resumeBlocked: true });

    expect(screen.queryByRole("button", { name: /继续制作/ })).toBeNull();
    // 也别再说「需要人工介入」——这一步是等客户确认报价，不是出故障了。
    expect(screen.getByText("已暂停")).toBeTruthy();
    expect(screen.queryByText("需要人工介入")).toBeNull();
  });

  it("非收费闸门（或闸门已答）照常给「继续制作」并回调", () => {
    const { onResume } = renderBanner({ resumeBlocked: false });

    const button = screen.getByRole("button", { name: /继续制作/ });
    fireEvent.click(button);
    expect(onResume).toHaveBeenCalledTimes(1);
  });

  it("没有 needsHuman 时本来就没有按钮", () => {
    renderBanner({ needsHuman: null });

    expect(screen.queryByRole("button", { name: /继续制作/ })).toBeNull();
  });
});

const MANUAL_RUN: DirectorAutoRunPayload = {
  status: "manual",
  episode: null,
  run_id: null,
};

/**
 * 「开始制作」是 `manual` 项目唯一的入口。
 *
 * 从「我的剧」进项目不带上传页的 `?autostart=1`，以前页面就只剩一句「待开始」、
 * 没有任何可点的东西——客户只能干等。这个按钮把出口补上：`manual` 且父级认为可以
 * 启动时渲染，点击回调。
 */
describe("StudioRunBanner 的「开始制作」", () => {
  function renderStart(props: { onStart?: () => void; startPending?: boolean } = {}) {
    render(
      <StudioRunBanner
        run={MANUAL_RUN}
        needsHuman={null}
        recoveries={0}
        onResume={vi.fn()}
        resumePending={false}
        onStart={props.onStart}
        startPending={props.startPending}
      />,
    );
  }

  it("manual 且有 onStart → 渲染「开始制作」并回调", () => {
    const onStart = vi.fn();
    renderStart({ onStart });

    const button = screen.getByRole("button", { name: /开始制作/ });
    fireEvent.click(button);
    expect(onStart).toHaveBeenCalledTimes(1);
    expect(screen.getByText("待开始")).toBeTruthy();
  });

  it("没给 onStart（导入在飞 / 已有 run）→ 不渲染按钮", () => {
    renderStart();

    expect(screen.queryByRole("button", { name: /开始制作/ })).toBeNull();
  });

  it("startPending → 按钮禁用，防重复启动", () => {
    renderStart({ onStart: vi.fn(), startPending: true });

    const button = screen.getByRole("button", { name: /开始制作/ });
    expect((button as HTMLButtonElement).disabled).toBe(true);
  });
});
