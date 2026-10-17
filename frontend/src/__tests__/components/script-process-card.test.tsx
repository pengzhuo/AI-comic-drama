// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({
  status: undefined as Record<string, unknown> | undefined,
  running: false,
  mutate: vi.fn(),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, values?: Record<string, unknown>) =>
      values ? `${key}:${JSON.stringify(values)}` : key,
  }),
}));

vi.mock("@/lib/queries/ingest", () => ({
  isScriptProcessRunning: () => state.running,
  useScriptProcess: () => ({
    data: state.status,
    isLoading: false,
    isError: false,
  }),
  useStartScriptProcess: () => ({
    mutate: state.mutate,
    isPending: false,
    error: null,
  }),
}));

import { ScriptProcessCard } from "@/components/ingest/ScriptProcessCard";

afterEach(() => {
  state.status = undefined;
  state.running = false;
  state.mutate.mockClear();
});

describe("ScriptProcessCard", () => {
  it("offers a first run before any result exists", () => {
    state.status = {
      has_result: false,
      task: { status: "idle", progress: 0, current_task: "" },
      result: null,
    };

    render(<ScriptProcessCard project="demo" />);

    expect(screen.getByText("ingest.scriptProcessTitle")).toBeInTheDocument();
    expect(screen.getByText("ingest.scriptProcessIdle")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /ingest\.scriptProcessStart/ }),
    ).toBeInTheDocument();
  });

  it("summarises the result and re-runs with force", async () => {
    state.status = {
      has_result: true,
      task: { status: "completed", progress: 1, current_task: "" },
      result: {
        characters: [{ name: "林昭" }],
        scenes: [{ name: "雨巷" }, { name: "老宅" }],
        gaps: ["年龄未明确"],
        degraded_chunks: 1,
      },
    };

    render(<ScriptProcessCard project="demo" />);

    expect(screen.getByText("ingest.scriptProcessDone")).toBeInTheDocument();
    expect(
      screen.getByText(/ingest\.scriptProcessSummary:{"characters":1,"scenes":2,"gaps":1}/),
    ).toBeInTheDocument();
    expect(screen.getByText(/ingest\.scriptProcessDegraded/)).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: /ingest\.scriptProcessRerun/ }),
    );
    expect(state.mutate).toHaveBeenCalledWith({ force: true });
  });

  it("disables the action and shows progress while the stage runs", () => {
    state.running = true;
    state.status = {
      has_result: false,
      task: { status: "running", progress: 0.4, current_task: "处理第 2/3 段" },
      result: null,
    };

    render(<ScriptProcessCard project="demo" />);

    expect(screen.getByText("ingest.scriptProcessRunning")).toBeInTheDocument();
    expect(screen.getByText("40%")).toBeInTheDocument();
    expect(screen.getByText("处理第 2/3 段")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /ingest\.scriptProcessStart/ }),
    ).toBeDisabled();
  });

  it("surfaces a failed run", () => {
    state.status = {
      has_result: false,
      task: { status: "failed", progress: 0, current_task: "" },
      result: null,
    };

    render(<ScriptProcessCard project="demo" />);

    expect(screen.getByText("ingest.scriptProcessFailed")).toBeInTheDocument();
  });
});
