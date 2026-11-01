// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { InternalKeyGate } from "@/features/access/internal-key-gate";

describe("<InternalKeyGate />", () => {
  it("keeps the submit button disabled until a key is typed", async () => {
    const user = userEvent.setup();
    render(<InternalKeyGate activate={vi.fn()} reload={vi.fn()} />);

    const button = screen.getByRole("button", { name: "进入" });
    expect(button).toBeDisabled();

    await user.type(screen.getByLabelText("内部访问口令"), "abc");
    expect(button).toBeEnabled();
  });

  it("reports a wrong key as an alert and does not reload", async () => {
    const user = userEvent.setup();
    const activate = vi.fn().mockResolvedValue({ ok: false, error: "内部访问口令不正确" });
    const reload = vi.fn();
    render(<InternalKeyGate activate={activate} reload={reload} />);

    await user.type(screen.getByLabelText("内部访问口令"), "wrong");
    await user.click(screen.getByRole("button", { name: "进入" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("内部访问口令不正确");
    expect(activate).toHaveBeenCalledWith("wrong");
    expect(reload).not.toHaveBeenCalled();
  });

  it("reloads the page after a correct key", async () => {
    const user = userEvent.setup();
    const activate = vi.fn().mockResolvedValue({ ok: true });
    const reload = vi.fn();
    render(<InternalKeyGate activate={activate} reload={reload} />);

    await user.type(screen.getByLabelText("内部访问口令"), "  good-key  ");
    await user.click(screen.getByRole("button", { name: "进入" }));

    expect(activate).toHaveBeenCalledWith("good-key");
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("shows the client-identity hint when provided", () => {
    render(<InternalKeyGate activate={vi.fn()} reload={vi.fn()} hint="客户身份" />);

    expect(screen.getByTestId("internal-key-gate-hint")).toHaveTextContent("客户身份");
  });
});
