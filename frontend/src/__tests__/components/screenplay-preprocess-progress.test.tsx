// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ScreenplayPreprocessProgress } from "@/components/ingest/ScreenplayPreprocessProgress";

const hoisted = vi.hoisted(() => ({
  calls: [] as Array<{ enabled: boolean; pollWhileWaitingMs: number }>,
  starts: [] as Array<{ force?: boolean }>,
  payload: undefined as
    | {
        has_result?: boolean;
        task?: { status: string; progress: number; current_task: string };
        result?: {
          changed?: boolean;
          scene_headings?: number;
          stats?: { split_headings_merged?: number };
          episodes_updated?: number;
        };
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
    useScreenplayPreprocess: (
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
    useStartScreenplayPreprocess: () => ({
      mutate: (params: { force?: boolean }) => hoisted.starts.push(params),
      isPending: false,
      isError: false,
    }),
  };
});

describe("ScreenplayPreprocessProgress", () => {
  const lastCall = () => hoisted.calls[hoisted.calls.length - 1];

  beforeEach(() => {
    hoisted.calls.length = 0;
    hoisted.starts.length = 0;
    hoisted.payload = undefined;
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("reports the live stage with its percentage and current step", () => {
    hoisted.payload = {
      has_result: false,
      task: { status: "running", progress: 0.35, current_task: "规范化场景头" },
    };

    render(<ScreenplayPreprocessProgress project="p1" />);

    expect(
      screen.getByText("ingest.screenplayPreprocessTitle"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("ingest.screenplayPreprocessRunning"),
    ).toBeInTheDocument();
    expect(screen.getByText("35%")).toBeInTheDocument();
    expect(screen.getByText("规范化场景头")).toBeInTheDocument();
  });

  it("summarizes the finished stage", () => {
    hoisted.payload = {
      has_result: true,
      task: { status: "completed", progress: 1, current_task: "" },
      result: {
        changed: true,
        scene_headings: 180,
        stats: { split_headings_merged: 180 },
      },
    };

    render(<ScreenplayPreprocessProgress project="p1" />);

    expect(
      screen.getByText("ingest.screenplayPreprocessDone"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        'ingest.screenplayPreprocessSummary {"merged":180,"headings":180}',
      ),
    ).toBeInTheDocument();
  });

  it("says so when the screenplay was already canonical", () => {
    hoisted.payload = {
      has_result: true,
      task: { status: "completed", progress: 1, current_task: "" },
      result: { changed: false, scene_headings: 60 },
    };

    render(<ScreenplayPreprocessProgress project="p1" />);

    expect(
      screen.getByText("ingest.screenplayPreprocessUnchanged"),
    ).toBeInTheDocument();
  });

  it("runs the stage manually, forcing a re-run once a result exists", async () => {
    hoisted.payload = {
      has_result: true,
      task: { status: "completed", progress: 1, current_task: "" },
      result: { changed: true, scene_headings: 3 },
    };

    render(<ScreenplayPreprocessProgress project="p1" />);
    await userEvent.click(
      screen.getByRole("button", { name: "ingest.screenplayPreprocessRerun" }),
    );

    expect(hoisted.starts).toEqual([{ force: true }]);
  });

  it("only pokes the status endpoint during the post-import waiting window", async () => {
    render(<ScreenplayPreprocessProgress project="p1" watchMs={300_000} />);
    expect(lastCall()?.pollWhileWaitingMs).toBe(3000);

    hoisted.payload = {
      has_result: false,
      task: { status: "running", progress: 0.1, current_task: "读取剧本" },
    };
    render(<ScreenplayPreprocessProgress project="p1" watchMs={300_000} />);

    await waitFor(() => expect(lastCall()?.pollWhileWaitingMs).toBe(0));
  });

  it("hides the progress bar when idle", () => {
    render(<ScreenplayPreprocessProgress project="p1" />);
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
  });
});
