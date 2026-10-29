// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
/**
 * Instance-level attribution text for the bottom task status bar, e.g.
 * "基于(Dramaclaw)开发" on this deployment.
 *
 * Deliberately kept out of `ui-hide-flags.ts`: those are booleans that only ever
 * *remove* upstream UI, whereas this carries content that upstream never wants
 * to show. Unset (the default, and the whole upstream history) makes the status
 * bar render exactly as before; an instance opts in from `.env.ce.local`, which
 * is git-ignored and only loaded by `npm run build:ce`.
 */

declare global {
  interface ImportMetaEnv {
    /**
     * Attribution text shown at the right end of the bottom status bar.
     * Unset or blank → nothing renders (upstream parity).
     */
    readonly VITE_BRAND_ATTRIBUTION?: string;
  }
}

export function brandAttribution(): string | null {
  // Whitespace-only counts as unset so a stray blank `.env` entry cannot leave
  // an empty gap in the status bar.
  const value = import.meta.env.VITE_BRAND_ATTRIBUTION?.trim();
  return value ? value : null;
}
