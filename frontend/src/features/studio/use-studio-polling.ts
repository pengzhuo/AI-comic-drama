// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { queryKeys } from "@/lib/query-keys";

/**
 * 工作室的进度轮询。
 *
 * 直接把相关查询标脏，让 react-query 去重拉一次；比给每个 hook 塞 refetchInterval
 * 更省事，也让「导入中 / 制作中」这两种截然不同的阶段共用同一条脉搏。
 */
export function useStudioPolling(
  project: string,
  { enabled, intervalMs = 4000 }: { enabled: boolean; intervalMs?: number },
): void {
  const queryClient = useQueryClient();
  useEffect(() => {
    if (!enabled || !project) return;
    const tick = () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.pipelineStatus(project) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.episodes(project) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.projectSummaries() });
    };
    const id = window.setInterval(tick, intervalMs);
    return () => window.clearInterval(id);
  }, [enabled, intervalMs, project, queryClient]);
}
