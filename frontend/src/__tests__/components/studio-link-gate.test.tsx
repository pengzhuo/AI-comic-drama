// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { StudioLinkGate } from "@/features/access/studio-link-gate";

describe("<StudioLinkGate />", () => {
  it("explains that a personal link is required", () => {
    render(<StudioLinkGate />);

    expect(
      screen.getByRole("heading", { name: "需要专属访问链接" }),
    ).toBeInTheDocument();
  });

  it("shows a notice as an alert", () => {
    render(<StudioLinkGate notice="访问链接无效" />);

    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("访问链接无效");
  });

  it("renders no alert when there is no notice", () => {
    render(<StudioLinkGate />);

    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("calls onLogout when the operator ends the session", async () => {
    const user = userEvent.setup();
    const onLogout = vi.fn();
    render(<StudioLinkGate onLogout={onLogout} />);

    await user.click(screen.getByRole("button", { name: "退出当前会话" }));

    expect(onLogout).toHaveBeenCalledTimes(1);
  });

  it("renders the attribution only when provided", () => {
    const { rerender } = render(<StudioLinkGate />);
    expect(screen.queryByText("ClaymoreLab")).toBeNull();

    rerender(<StudioLinkGate attribution="ClaymoreLab" />);
    expect(screen.getByText("ClaymoreLab")).toBeInTheDocument();
  });
});
