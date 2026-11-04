// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { createFileRoute } from "@tanstack/react-router";

import { FreezoneCanvasHost } from "@/features/freezone/FreezoneCanvasHost";
import { TaskCenterProvider } from "@/task-center/provider";

export const Route = createFileRoute("/canvas/$project")({
  component: CanvasProjectPage,
});

/**
 * 客户画布：某个项目的捏画。
 *
 * 复用内部端同一份画布（`FreezoneCanvasHost` + `FreezoneCanvasRoot` → `FreezoneShell`），
 * 只是宿主不同：内部端由 `_app.tsx` 喂 `{ project, active }`，这里项目来自路由参数、
 * 永远激活。
 *
 * 外壳由 `/canvas` 的 layout 统一提供（那里按路径给 `fill`），这里只补两样宿主自己
 * 的责任：
 *
 * 1. `TaskCenterProvider` 是画布的硬依赖（`SuperChatPanel` 走它的 event bus），内部端
 *    在 `_app.tsx` 里包了一层，这里必须自己包 —— 少了它 `useEventBus` 会抛错。它只
 *    渲染 Context、不产生 DOM 包裹，所以下面的 `h-full` 仍相对外壳的 `main` 解析。
 * 2. 画布宿主是 `absolute inset-0`，需要一个相对定位的满高父容器。
 */
function CanvasProjectPage() {
  const { project } = Route.useParams();

  return (
    <TaskCenterProvider projectId={project}>
      <div className="relative h-full w-full">
        <FreezoneCanvasHost project={project} active />
      </div>
    </TaskCenterProvider>
  );
}
