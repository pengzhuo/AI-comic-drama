// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { Suspense, lazy, useEffect, useRef, useState } from "react";

import { cn } from "@/lib/utils";

const FreezoneCanvasRoot = lazy(() => import("./FreezoneCanvasRoot"));

export interface FreezoneCanvasHostProps {
  /** 要保活的项目 id；null 表示当前不在任何项目里。 */
  project: string | null;
  /** 画布是不是当前页面；false 表示被保活在后台。 */
  active: boolean;
}

/**
 * 让画布在同一个项目内保活。
 *
 * 顶栏「捏画 / 捏集」切换原本是一次普通的路由跳转，而 projects.$project 下没有
 * layout route，两边是平级兄弟；_app.tsx 又按 `/projects/$project/$section` 给
 * 动画容器算 key。结果就是每切一次，FreezoneShell + Canvas + ReactFlow 整棵子树
 * 连同它上千个节点组件一起销毁重建，卸载和挂载都落在同一次 commit 里 —— 这就是
 * 「点完按钮要停顿一下才换页」，且两个方向都停。
 *
 * 这里把画布提到被 re-key 的动画容器之外、按项目挂载一次，切到捏集时只是隐藏
 * 并置为非激活。用 visibility 而不是 display:none 是有意的：display:none 会让
 * 容器尺寸归零，ReactFlow 的 ResizeObserver 会因此重置视口，切回来画布的缩放和
 * 位置就没了。visibility:hidden 保留布局尺寸，切回来还是原样。
 *
 * 保活的代价是画布在后台仍然活着，所以要显式交出「只有前台该做的事」——这些由
 * FreezoneShell 的 active 开关负责（全局快捷键、两条轮询）。
 *
 * `project` / `active` 由调用方喂进来（内部壳用 `useFreezoneCanvasRoute()` 从
 * `/projects/<id>/freezone` 算，将来的 `/canvas/<id>` 入口自己算），宿主本身不认
 * URL —— 换一套地址形状不用动这里。
 */
export function FreezoneCanvasHost({ project, active }: FreezoneCanvasHostProps) {
  // 只有真的进过捏画才挂载：在捏集里从头到尾没开过画布的用户，不该为它付出
  // 挂载成本，也不该多打那些 canvas 请求。
  // 惰性初值而不是先 null 再由 effect 补：直接把地址栏敲成 /freezone 进来时，
  // 前者首帧就能挂上画布，后者要白等一帧。
  const [mountedProject, setMountedProject] = useState<string | null>(() =>
    active && project ? project : null,
  );
  useEffect(() => {
    if (active && project) {
      setMountedProject(project);
      return;
    }
    // 离开项目、或换到另一个项目，就把画布放掉 —— 保活只在单个项目内成立，
    // 跨项目继续留着既占内存，画的也是上一个项目的东西。
    if (!project || project !== mountedProject) {
      setMountedProject(null);
    }
  }, [active, project, mountedProject]);

  // 画布不再被卸载，正在播的视频/音频就不会跟着停 —— 切到捏集后会变成看不见的
  // 后台声音。隐藏时统一暂停一次（只暂停、不恢复：切回来是否继续播由用户决定，
  // 自动续播比静音更让人意外）。
  const containerRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (active) return;
    const container = containerRef.current;
    if (!container) return;
    container
      .querySelectorAll<HTMLMediaElement>("video, audio")
      .forEach((media) => {
        if (!media.paused) media.pause();
      });
  }, [active]);

  if (!mountedProject) return null;

  return (
    <div
      ref={containerRef}
      className={cn(
        "absolute inset-0 z-0",
        // opacity-0 是给 invisible 兜底的，不是重复：visibility 会被后代自己的
        // `visibility: visible` 顶掉（本仓库就有这种写法 —— AssetBoardView 的显隐
        // 开关、SplitText 动画结束时的 el.style.visibility = "visible"），opacity
        // 顶不掉。两个都不用 display:none，是要保住容器尺寸，否则 ReactFlow 的
        // ResizeObserver 会重置视口，切回来缩放和位置就没了。
        !active && "invisible opacity-0 pointer-events-none",
      )}
      aria-hidden={!active}
      inert={!active}
    >
      <Suspense fallback={<FreezoneChunkLoading />}>
        {/* key 必须带项目：在捏画里直接换项目（ProjectSwitcher 会保持在捏画）
            原本靠 _app.tsx 那个带 $project 的 re-key 整棵重建，画布提出来之后
            没人做这件事了。不重建的话 FreezoneShell 的一堆按项目来的状态会串台
            —— 最刺眼的是 hasRenderedCanvas 只置不清，新项目 hydrate 期间屏幕上
            还挂着上一个项目的节点。 */}
        <FreezoneCanvasRoot
          key={mountedProject}
          project={mountedProject}
          active={active}
        />
      </Suspense>
    </div>
  );
}

/** 首次进入捏画、画布 chunk 还在下载时的占位，沿用原路由的加载态。 */
function FreezoneChunkLoading() {
  return (
    <div className="flex h-full w-full items-center justify-center bg-bg-dark text-text-muted">
      <div className="h-8 w-8 animate-spin rounded-full border-4 border-accent border-t-transparent" />
    </div>
  );
}
