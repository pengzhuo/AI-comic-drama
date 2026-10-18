// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ScriptProcessProgress } from "@/components/ingest/ScriptProcessProgress";

const hoisted = vi.hoisted(() => ({
  calls: [] as Array<{ enabled: boolean; pollWhileWaitingMs: number }>,
  payload: undefined as
    | {
        has_result?: boolean;
        task?: { status: string; progress: number; current_task: string };
        result?: { characters: unknown[]; scenes: unknown[]; gaps: unknown[]; degraded_chunks?: number };
      }
    | undefined,
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, values?: Record<string, unknown>) =>
      values ? `${key} ${JSON.stringify(values)}` : key,
  }),
}));

vi.mock("@/lib/queries/ingest", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/queries/ingest")>();
  return {
    ...actual,
    useScriptProcess: (
      _project: string,
      enabled = true,
      options: { pollWhileWaitingMs?: number } = {},
    ) => {
      hoisted.calls.push({
        enabled,
        pollWhileWaitingMs: options.pollWhileWaitingMs ?? 0,
      });
      return { data: hoisted.payload };
    },
  };
});

describe("ScriptProcessProgress", () => {
  /** The polling options the component last passed to the status hook. */
  const lastCall = () => hoisted.calls[hoisted.calls.length - 1];

  beforeEach(() => {
    hoisted.calls.length = 0;
    hoisted.payload = undefined;
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("reports the live stage with its percentage and current step", () => {
    hoisted.payload = {
      has_result: false,
      task: { status: "running", progress: 0.62, current_task: "全文读审" },
    };

    render(<ScriptProcessProgress project="p1" />);

    expect(screen.getByText("ingest.scriptProcessTitle")).toBeInTheDocument();
    expect(screen.getByText("ingest.scriptProcessRunning")).toBeInTheDocument();
    expect(screen.getByText("62%")).toBeInTheDocument();
    expect(screen.getByText("全文读审")).toBeInTheDocument();
  });

  it("summarizes the finished stage", () => {
    hoisted.payload = {
      has_result: true,
      task: { status: "completed", progress: 1, current_task: "" },
      result: {
        characters: [1, 2, 3],
        scenes: [1, 2],
        gaps: [],
        degraded_chunks: 0,
      },
    };

    render(<ScriptProcessProgress project="p1" />);

    expect(screen.getByText("ingest.scriptProcessDone")).toBeInTheDocument();
    expect(
      screen.getByText(
        'ingest.scriptProcessSummary {"characters":3,"scenes":2,"gaps":0}',
      ),
    ).toBeInTheDocument();
  });

  it("only pokes the status endpoint during the post-import waiting window", async () => {
    // Nothing exists yet: the import chains the stage, so this is the gap the
    // waiting window covers.
    render(<ScriptProcessProgress project="p1" watchMs={300_000} />);
    expect(lastCall()?.pollWhileWaitingMs).toBe(3000);

    // As soon as the stage shows up the hook's own live polling takes over.
    hoisted.payload = {
      has_result: false,
      task: { status: "running", progress: 0.1, current_task: "读取剧本" },
    };
    render(<ScriptProcessProgress project="p1" watchMs={300_000} />);

    await waitFor(() => expect(lastCall()?.pollWhileWaitingMs).toBe(0));
  });

  it("hides the progress bar when idle", () => {
    render(<ScriptProcessProgress project="p1" />);
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });
});
