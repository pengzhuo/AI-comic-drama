// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useRouterState } from "@tanstack/react-router";
import { ArrowLeft, Check, ChevronDown, Clapperboard, Sparkles } from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  PROJECT_SECTION_ROUTES,
  projectModeFromPath,
  projectSectionFromPath,
} from "@/components/layout/project-navigation-routes";
import { normalizeLastEpisodeLocation, useEpisodeWorkbenchStore } from "@/stores/episode-workbench-store";
import { isRememberedSection, useProjectNavStore } from "@/stores/project-nav-store";
import { useAllProjectSummaries } from "@/lib/queries/projects";
import { getProjectCover } from "@/lib/project-cover";
import { beginRouteSwitch } from "@/lib/route-switch-transition";
import { cn } from "@/lib/utils";
import { surfaceAccess, useProductSurfaces } from "@/lib/queries/product-surfaces";
import { SlidingTabs, type SlidingTabItem } from "@/components/nav/sliding-tabs";

const XIAJI_DEFAULT_ROUTE = PROJECT_SECTION_ROUTES.ingest;

const xiajiMenuItems = [
  { labelKey: "nav.ingest", to: PROJECT_SECTION_ROUTES.ingest },
  { labelKey: "nav.assets", to: PROJECT_SECTION_ROUTES.characters },
  {
    labelKey: "nav.episodes",
    to: PROJECT_SECTION_ROUTES.episodes,
    rememberKey: "episodes",
  },
  { labelKey: "nav.aiAssistant", to: PROJECT_SECTION_ROUTES.assistant },
  { labelKey: "nav.styles", to: PROJECT_SECTION_ROUTES.styles },
  { labelKey: "nav.reports", to: PROJECT_SECTION_ROUTES.reports },
] as const;

function ProjectAvatar({ name }: { name: string }) {
  const { gradient, initial } = useMemo(() => getProjectCover(name), [name]);
  return (
    <span
      className="flex size-5 shrink-0 items-center justify-center rounded text-xs font-bold text-foreground/95"
      style={{ background: gradient }}
    >
      {initial}
    </span>
  );
}

export function ProjectSwitcher({ current }: { current: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { data: summaries } = useAllProjectSummaries();
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  const targetSection = projectSectionFromPath(pathname) ?? "freezone";
  const productSurfaces = useProductSurfaces();
  const mainlineAvailable =
    surfaceAccess(productSurfaces.data, "mainline")?.available ?? productSurfaces.isPending;
  const freezoneAvailable =
    surfaceAccess(productSurfaces.data, "freezone")?.available ?? productSurfaces.isPending;
  const assistantAvailable = surfaceAccess(productSurfaces.data, "assistant")?.available ?? false;
  const availableTargetSection =
    targetSection === "freezone" && !freezoneAvailable
      ? "ingest"
      : targetSection === "assistant" && !assistantAvailable
        ? mainlineAvailable
          ? "ingest"
          : "freezone"
        : targetSection !== "freezone" && !mainlineAvailable
          ? "freezone"
          : targetSection;
  const [open, setOpen] = useState(false);
  const closeTimerRef = useRef<number | null>(null);
  const projects = useMemo(
    () =>
      (summaries ?? [])
        .filter((project) => project.status === "active")
        .map((project) => ({ id: project.id || project.name, name: project.name })),
    [summaries],
  );
  const currentSummary = useMemo(
    () =>
      projects.find((project) => project.id === current) ??
      projects.find((project) => project.name === current),
    [current, projects],
  );
  const currentName = currentSummary?.name ?? current;

  const cancelClose = () => {
    if (closeTimerRef.current === null) return;
    window.clearTimeout(closeTimerRef.current);
    closeTimerRef.current = null;
  };
  const openMenu = () => {
    cancelClose();
    setOpen(true);
  };
  const scheduleClose = () => {
    cancelClose();
    closeTimerRef.current = window.setTimeout(() => {
      setOpen(false);
      closeTimerRef.current = null;
    }, 120);
  };

  useEffect(() => cancelClose, []);

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger
        onMouseEnter={openMenu}
        onMouseLeave={scheduleClose}
        className="inline-flex h-8 max-w-[156px] cursor-pointer items-center gap-1.5 bg-transparent px-1 text-left text-[13px] leading-none text-sidebar-foreground/90 transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sidebar-ring"
      >
        <span className="min-w-0 truncate leading-none">{currentName}</span>
        <ChevronDown className="size-3.5 shrink-0 translate-y-px text-muted-foreground" />
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="start"
        sideOffset={8}
        onMouseEnter={openMenu}
        onMouseLeave={scheduleClose}
        className="w-56 rounded-md border border-black/10 bg-popover p-1 shadow-xl shadow-black/20 ring-0"
      >
        <DropdownMenuGroup>
          <DropdownMenuItem
            onClick={() => navigate({ to: "/" })}
            className="min-h-8 gap-2 rounded-sm px-2 py-1.5 text-xs focus:bg-black/8 focus:text-current"
          >
            <ArrowLeft className="size-3.5" />
            {t("project.dashboardReturn")}
          </DropdownMenuItem>
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuGroup>
          <DropdownMenuLabel className="px-2 py-1.5 text-xs font-medium text-muted-foreground">
            {t("nav.switchProject")}
          </DropdownMenuLabel>
          {projects.map((project) => (
            <DropdownMenuItem
              key={project.id}
              onClick={() =>
                navigate({
                  to: PROJECT_SECTION_ROUTES[availableTargetSection],
                  params: { project: project.id },
                })
              }
              className="min-h-8 gap-2 rounded-sm px-2 py-1.5 text-xs focus:bg-black/8 focus:text-current"
            >
              <ProjectAvatar name={project.name} />
              <span className="flex-1 truncate">{project.name}</span>
              {project.id === current ? (
                <Check className="size-3.5 text-primary" aria-hidden />
              ) : null}
            </DropdownMenuItem>
          ))}
        </DropdownMenuGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** 子页之间的水平间距，对齐 nav 上的 gap-3。 */
const XIAJI_SUBNAV_GAP = 12;
/** 「更多」按钮的兜底宽度（探针量不到时用），对齐 size-7。 */
const XIAJI_MORE_WIDTH_FALLBACK = 28;
/** 中间组与两侧内容之间额外留的安全缝。 */
const XIAJI_HEADER_SAFETY_GAP = 16;

type XiajiMenuItem = (typeof xiajiMenuItems)[number];

/**
 * 子页导航的溢出结果。null = 量不到布局（jsdom / 容器被隐藏），按全量渲染。
 * - visibleCount：直接铺在顶栏里的子页数
 * - showMore：尾部放不下的项收进「更多」下拉
 * - hideAll：连「更多」都放不下，整条子页隐掉，只留捏画/捏集胶囊
 */
type XiajiSubNavFit = {
  visibleCount: number;
  showMore: boolean;
  hideAll: boolean;
};

/**
 * 「捏集」子页导航（捏料 / 捏塘 / 捏镜 / 捏导 / 捏格）。内联在顶栏「捏集」胶囊右侧、
 * 与胶囊同处一行 —— 外面那层 nav 是绝对居中的，胶囊 + 子页作为一整组一起居中。
 * 切到「捏画」时 mode 变成 xiahua，父组件直接不渲染它，菜单随之收起。
 *
 * 顶栏是「左 品牌+项目切换 / 中 绝对居中 / 右 操作」三栏结构，中间这组变宽会压到
 * 两侧。这里按两侧内容的实际边缘量出可用宽度，放不下的尾部子页收进「更多」下拉，
 * 再放不下就连子页一起隐藏。必须实测而不是套 CSS 断点：两侧宽度随项目名、语言、
 * 字号设置而变。
 */
function ProjectXiajiSubNav({ project }: { project: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const rememberedEpisodeLocation = useEpisodeWorkbenchStore(
    (state) => state.lastEpisodeLocationByProject[project],
  );
  const productSurfaces = useProductSurfaces();
  const assistantAvailable = surfaceAccess(productSurfaces.data, "assistant")?.available ?? false;

  const probeRef = useRef<HTMLDivElement | null>(null);
  const navRef = useRef<HTMLElement | null>(null);
  const [fit, setFit] = useState<XiajiSubNavFit | null>(null);

  const visibleMenuItems = useMemo(
    () =>
      xiajiMenuItems.filter(
        (item) => item.to !== PROJECT_SECTION_ROUTES.assistant || assistantAvailable,
      ),
    [assistantAvailable],
  );

  const resolveTarget = (item: XiajiMenuItem) =>
    "rememberKey" in item && rememberedEpisodeLocation
      ? normalizeLastEpisodeLocation(project, rememberedEpisodeLocation) ?? item.to
      : item.to;

  // 高亮按栏目自身的路由判断：target 可能是带 ?query 的剧集深链，
  // 拿它比 pathname 永远不相等（捏镜里就不会高亮）。
  const isActive = (item: XiajiMenuItem) => {
    const sectionPath = item.to.replace("$project", encodeURIComponent(project));
    return pathname === sectionPath || pathname.startsWith(`${sectionPath}/`);
  };

  useLayoutEffect(() => {
    const probe = probeRef.current;
    const nav = navRef.current;
    const headerEl = nav?.closest("header");
    const group = nav?.closest("[data-header-nav-group]");
    if (!probe || !nav || !headerEl || !group) return undefined;

    let frame = 0;
    const schedule = () => {
      if (frame) return;
      // 观察器回调里直接读 rect 会撞上浏览器同一帧内的多次布局，攒到下一帧统一量。
      frame = requestAnimationFrame(() => {
        frame = 0;
        measure();
      });
    };

    const measure = () => {
      const pill = group.querySelector<HTMLElement>(".sliding-tabs--project-modes");
      if (!pill) return;
      const headerRect = headerEl.getBoundingClientRect();
      // jsdom 与 display:none 的宿主一律全零，量不出东西就别改状态（否则会误判成放不下）。
      if (headerRect.width <= 0) {
        setFit(null);
        return;
      }

      const styles = window.getComputedStyle(headerEl);
      const padLeft = Number.parseFloat(styles.paddingLeft) || 0;
      const padRight = Number.parseFloat(styles.paddingRight) || 0;
      const columnGap = Number.parseFloat(styles.columnGap || styles.gap) || 0;
      const innerLeft = headerRect.left + padLeft;
      const innerRight = headerRect.right - padRight;

      // 两侧容器是 flex-1，量它们本身拿到的是「分到的一半」，不是内容用掉的宽。
      // 左栏内容左对齐 → 看它最后一个孩子的右缘；右栏内容右对齐 → 看第一个孩子的左缘。
      const leftContent = headerEl.querySelector('[data-header-slot="left"]')?.lastElementChild;
      const rightContent = headerEl.querySelector('[data-header-slot="right"]')?.firstElementChild;
      const usedLeft = leftContent
        ? Math.max(0, leftContent.getBoundingClientRect().right - innerLeft) + columnGap
        : 0;
      const usedRight = rightContent
        ? Math.max(0, innerRight - rightContent.getBoundingClientRect().left) + columnGap
        : 0;

      const pillRect = pill.getBoundingClientRect();
      // 胶囊右缘 → 子页 nav 左缘（ml-3 + 竖分隔 + gap-3）。分隔符常驻，这个偏移是恒定的，
      // 不会因为收起了几个子页而变，测量才不会来回震荡。
      const leading = Math.max(0, nav.getBoundingClientRect().left - pillRect.right);
      const budget =
        innerRight -
        innerLeft -
        usedLeft -
        usedRight -
        XIAJI_HEADER_SAFETY_GAP -
        pillRect.width -
        leading;

      const itemWidths = Array.from(
        probe.querySelectorAll<HTMLElement>("[data-xiaji-probe]"),
      ).map((node) => node.offsetWidth);
      const moreWidth =
        probe.querySelector<HTMLElement>("[data-xiaji-more-probe]")?.offsetWidth ??
        XIAJI_MORE_WIDTH_FALLBACK;
      const count = itemWidths.length;

      const fits = (visibleCount: number) => {
        let total = 0;
        for (let index = 0; index < visibleCount; index += 1) {
          total += index === 0 ? itemWidths[index] ?? 0 : XIAJI_SUBNAV_GAP + (itemWidths[index] ?? 0);
        }
        if (visibleCount < count) {
          total += (visibleCount > 0 ? XIAJI_SUBNAV_GAP : 0) + moreWidth;
        }
        return total <= budget;
      };

      let visibleCount = count;
      while (visibleCount > 0 && !fits(visibleCount)) visibleCount -= 1;
      const showMore = visibleCount < count;
      const hideAll = showMore && !fits(0);

      setFit((previous) =>
        previous &&
        previous.visibleCount === visibleCount &&
        previous.showMore === showMore &&
        previous.hideAll === hideAll
          ? previous
          : { visibleCount, showMore, hideAll },
      );
    };

    schedule();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", schedule);
      return () => {
        if (frame) cancelAnimationFrame(frame);
        window.removeEventListener("resize", schedule);
      };
    }
    const observer = new ResizeObserver(schedule);
    [group, headerEl, probe, nav].forEach((node) => observer.observe(node));
    // 两侧文案（项目名 / 语言）变了不一定动顶栏本身的尺寸，得单独盯住。
    const leftContent = headerEl.querySelector('[data-header-slot="left"]')?.lastElementChild;
    const rightContent = headerEl.querySelector('[data-header-slot="right"]')?.firstElementChild;
    if (leftContent) observer.observe(leftContent);
    if (rightContent) observer.observe(rightContent);
    return () => {
      if (frame) cancelAnimationFrame(frame);
      observer.disconnect();
    };
    // 文案集合变了要重新分配谁进「更多」。
  }, [visibleMenuItems]);

  const visibleCount = fit?.visibleCount ?? visibleMenuItems.length;
  const renderedItems = visibleMenuItems.slice(0, visibleCount);
  const collapsedItems = visibleMenuItems.slice(visibleCount);
  const showMore = fit?.showMore ?? false;
  const hideAll = fit?.hideAll ?? false;

  return (
    <>
      {/* 量宽度用的探针：文案与内边距跟真实子项一致，脱离布局、不进无障碍树。 */}
      <div
        ref={probeRef}
        aria-hidden="true"
        className="pointer-events-none invisible absolute left-0 top-0 flex items-center gap-3 whitespace-nowrap"
      >
        {visibleMenuItems.map((item) => (
          <span
            key={item.labelKey}
            data-xiaji-probe=""
            className="flex h-7 items-center px-1.5 text-xs font-semibold"
          >
            {t(item.labelKey)}
          </span>
        ))}
        <span data-xiaji-more-probe="" className="flex size-7 items-center justify-center">
          <ChevronDown className="size-3.5" aria-hidden />
        </span>
      </div>

      <div className={cn("ml-3 flex items-center gap-3", hideAll && "invisible")}>
        <span aria-hidden="true" className="h-4 w-px bg-black/[0.08]" />
        <nav
          ref={navRef}
          aria-label={t("nav.xiajiMenu")}
          className="flex items-center gap-3 whitespace-nowrap text-sidebar-foreground"
        >
          {renderedItems.map((item) => {
            const target = resolveTarget(item);
            const active = isActive(item);
            return (
              <Link
                key={item.labelKey}
                to={target}
                params={{ project }}
                className={cn(
                  "flex h-7 items-center rounded-md px-1.5 text-xs font-semibold transition-colors duration-150 ease-[var(--ease-out-quint)] hover:bg-black/[0.05]",
                  active ? "text-foreground" : "text-muted-foreground hover:text-foreground",
                )}
                aria-current={active ? "page" : undefined}
                // 保留 <Link> 是为了留住 href（中键 / ⌘ 点开新标签页、右键复制链接），
                // 只把普通左键那一条接管过来，先亮 loading 再跳 —— 换页是同步长任务，
                // 不这样切一刀的话中间一帧都画不出来。
                onClick={(event) => {
                  if (active || event.defaultPrevented) return;
                  if (
                    event.button !== 0 ||
                    event.metaKey ||
                    event.ctrlKey ||
                    event.shiftKey ||
                    event.altKey
                  ) {
                    return;
                  }
                  event.preventDefault();
                  beginRouteSwitch(() => navigate({ to: target, params: { project } }));
                }}
              >
                {t(item.labelKey)}
              </Link>
            );
          })}
          {showMore ? (
            <DropdownMenu>
              <DropdownMenuTrigger
                aria-label={t("nav.more")}
                className="flex size-7 cursor-pointer items-center justify-center rounded-md text-muted-foreground transition-colors duration-150 ease-[var(--ease-out-quint)] hover:bg-black/[0.05] hover:text-foreground data-popup-open:bg-black/[0.05] data-popup-open:text-foreground"
              >
                <ChevronDown className="size-3.5" aria-hidden />
              </DropdownMenuTrigger>
              <DropdownMenuContent
                align="end"
                sideOffset={8}
                className="w-32 rounded-md border border-black/10 bg-popover p-1 shadow-xl shadow-black/20 ring-0"
              >
                <DropdownMenuGroup>
                  {/* 收进来的项不再是 <Link>：下拉里没保住 href，只保证左键能跳。 */}
                  {collapsedItems.map((item) => {
                    const target = resolveTarget(item);
                    const active = isActive(item);
                    return (
                      <DropdownMenuItem
                        key={item.labelKey}
                        className="min-h-8 gap-2 rounded-sm px-2 py-1.5 text-xs focus:bg-black/8 focus:text-current"
                        onClick={() => {
                          if (active) return;
                          beginRouteSwitch(() => navigate({ to: target, params: { project } }));
                        }}
                      >
                        <span className="flex-1 truncate">{t(item.labelKey)}</span>
                        {active ? <Check className="size-3.5 text-primary" aria-hidden /> : null}
                      </DropdownMenuItem>
                    );
                  })}
                </DropdownMenuGroup>
              </DropdownMenuContent>
            </DropdownMenu>
          ) : null}
        </nav>
      </div>
    </>
  );
}

export function ProjectHeaderNavigation({ project }: { project: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const activeMode = projectModeFromPath(pathname);
  const rememberedEpisodeLocation = useEpisodeWorkbenchStore(
    (state) => state.lastEpisodeLocationByProject[project],
  );
  const setLastEpisodeLocation = useEpisodeWorkbenchStore((state) => state.setLastEpisodeLocation);
  const clearLastEpisodeLocation = useEpisodeWorkbenchStore((state) => state.clearLastEpisodeLocation);
  const rememberSection = useProjectNavStore((state) => state.rememberSection);
  const lastXiajiSection = useProjectNavStore(
    (state) => state.lastXiajiSectionByProject[project],
  );
  const productSurfaces = useProductSurfaces();
  const mainlineAvailable =
    surfaceAccess(productSurfaces.data, "mainline")?.available ?? productSurfaces.isPending;
  const freezoneAvailable =
    surfaceAccess(productSurfaces.data, "freezone")?.available ?? productSurfaces.isPending;

  // 记住当前停留的区块（捏画 / 捏集子页），进项目和切「捏集」时按此恢复。
  useEffect(() => {
    const section = projectSectionFromPath(pathname);
    if (isRememberedSection(section)) {
      rememberSection(project, section);
    }
  }, [pathname, project, rememberSection]);

  useEffect(() => {
    const episodesRoot = `/projects/${encodeURIComponent(project)}/episodes`;
    if (pathname === episodesRoot) {
      clearLastEpisodeLocation(project);
      return;
    }
    const match = pathname.match(/^\/projects\/([^/]+)\/episodes\/(\d+)(?:\/|$)/);
    if (!match || decodeURIComponent(match[1]) !== project) return;
    setLastEpisodeLocation(project, `${pathname}${window.location.search}`);
  }, [clearLastEpisodeLocation, pathname, project, setLastEpisodeLocation]);

  const changeMode = (mode: "xiahua" | "xiaji") => {
    if ((mode === "xiahua" && !freezoneAvailable) || (mode === "xiaji" && !mainlineAvailable)) {
      return;
    }
    if (mode === activeMode) return;
    if (mode === "xiahua") {
      beginRouteSwitch(() =>
        navigate({ to: PROJECT_SECTION_ROUTES.freezone, params: { project } }),
      );
      return;
    }
    // 切「捏集」时回到上次停留的子页（默认捏料）；上次在捏镜且有剧集深链则直达。
    let target: string = lastXiajiSection
      ? PROJECT_SECTION_ROUTES[lastXiajiSection]
      : XIAJI_DEFAULT_ROUTE;
    if (lastXiajiSection === "episodes" && rememberedEpisodeLocation) {
      target =
        normalizeLastEpisodeLocation(project, rememberedEpisodeLocation) ?? target;
    }
    beginRouteSwitch(() => navigate({ to: target, params: { project } }));
  };

  if (!mainlineAvailable && !freezoneAvailable) return null;

  const creationModes: SlidingTabItem<"xiahua" | "xiaji">[] = [
    ...(freezoneAvailable
      ? [{
          value: "xiahua" as const,
          label: t("nav.freezone"),
          icon: Sparkles,
        }]
      : []),
    ...(mainlineAvailable
      ? [{
          value: "xiaji" as const,
          label: t("nav.xiaji"),
          icon: Clapperboard,
        }]
      : []),
  ];

  return (
    <nav
      aria-label={t("nav.creationMode")}
      data-header-nav-group=""
      className="absolute left-1/2 top-1/2 z-30 flex -translate-x-1/2 -translate-y-1/2 items-center"
    >
      <SlidingTabs
        items={creationModes}
        value={activeMode}
        onValueChange={changeMode}
        className="sliding-tabs--project-modes"
        animateActiveIcon
        aria-label={t("nav.creationMode")}
      />
      {activeMode === "xiaji" && mainlineAvailable ? (
        <ProjectXiajiSubNav project={project} />
      ) : null}
      {/* superchat 搜索浮层的锚点。原来挂在「捏集」第二行底部，第二行取消后挪到这里，
          落点仍在顶栏正下方居中。全仓没有 getElementById("superchat-search-anchor") 的
          消费者（superchat-panel 用的是 #superchat-header-controls），保留是为了不动这个
          对外契约。 */}
      <div
        id="superchat-search-anchor"
        className="pointer-events-none absolute left-1/2 top-full z-50 mt-2 -translate-x-1/2"
      />
    </nav>
  );
}
