// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab

/**
 * Instance-level UI hides.
 *
 * Self-hosted instances sometimes want fewer interruptions than the upstream
 * default (the release-notes popup that opens on entry, the Piko mini-game
 * launcher in the status bar). Both are pure presentation choices, so they are
 * driven by build-time env flags rather than runtime config: leaving them unset
 * keeps the shipped behavior byte for byte, and an instance build opts out by
 * setting the flag to "1".
 */

function envFlag(value: string | undefined): boolean {
  return value === "1" || value === "true";
}

/**
 * Suppress the release-notes popup that auto-opens on entry. The notification
 * center's "view update" entry still opens it on demand.
 */
export function releasePopupHidden(): boolean {
  return envFlag(import.meta.env.VITE_HIDE_RELEASE_POPUP);
}

/** Hide the Piko mini-game launcher in the task status bar. */
export function pikoEntryHidden(): boolean {
  return envFlag(import.meta.env.VITE_HIDE_PIKO_ENTRY);
}

/**
 * Hide the official gateway channels ("官方" / "本地 + 官方混合") in Settings →
 * 模型与渠道, leaving the local NewAPI channel ("自定义") as the only offer.
 */
export function officialChannelsHidden(): boolean {
  return envFlag(import.meta.env.VITE_HIDE_OFFICIAL_CHANNELS);
}

/**
 * Hide the notification center ("公告中心"): the account-panel entry, the unread
 * dot it drives, and the drawer behind it.
 */
export function notificationCenterHidden(): boolean {
  return envFlag(import.meta.env.VITE_HIDE_NOTIFICATION_CENTER);
}

/**
 * Hide the "配置手册" link in Settings → 模型与渠道, which points at the upstream
 * model-configuration guide.
 */
export function modelGuideHidden(): boolean {
  return envFlag(import.meta.env.VITE_HIDE_MODEL_GUIDE);
}
