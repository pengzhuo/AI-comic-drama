// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Sheet, SheetContent } from "@/components/ui/sheet";

describe("Freezone 捏导抽屉保活", () => {
  it("keepMounted 的抽屉关闭后仍保留面板挂载", () => {
    render(
      <Sheet open={false} onOpenChange={() => {}}>
        <SheetContent keepMounted>
          <div data-testid="agent-panel-body">捏导面板</div>
        </SheetContent>
      </Sheet>,
    );

    expect(screen.getByTestId("agent-panel-body")).toBeTruthy();
  });

  it("默认抽屉关闭即卸载面板", () => {
    render(
      <Sheet open={false} onOpenChange={() => {}}>
        <SheetContent>
          <div data-testid="agent-panel-body">捏导面板</div>
        </SheetContent>
      </Sheet>,
    );

    expect(screen.queryByTestId("agent-panel-body")).toBeNull();
  });

  it("移动端抽屉把 busy 守卫接到 keepMounted 上", () => {
    const shell = readFileSync(
      resolve(process.cwd(), "src/features/freezone/FreezoneShell.tsx"),
      "utf8",
    );

    // 关抽屉=卸载面板=断 WS，进行中的这一轮会被服务端当成「用户走了」杀掉；
    // busy 期间必须保留挂载。
    expect(shell).toContain("keepMounted={shouldKeepPanelMounted}");
    expect(shell).toContain("setShouldRenderPanel(open || shouldKeepPanelMounted)");
  });
});
