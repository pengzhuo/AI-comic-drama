// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useEffect } from "react";

// DramaClaw is hard-locked to a single light theme. The dark/light toggle and
// the `.dark` token block are intentionally retired; `theme-provider` only ever
// applies `.light`, so a stale persisted theme can never darken the UI.
function applyTheme() {
  const root = document.documentElement;
  root.classList.remove("light", "dark");
  root.classList.add("light");
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  useEffect(() => {
    applyTheme();
  }, []);

  return children;
}

export function useResolvedTheme(): "light" | "dark" {
  return "light";
}
