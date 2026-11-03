// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
/**
 * 客户首页「我的剧」列表（`/studio` 与 `/canvas` 同构）。
 *
 * 删除为软删除（`POST /projects/{id}/delete`）：项目进回收站、从客户列表消失，
 * 内部端仍可恢复。`useAllProjectSummaries` 会把 `status=all` 的项目都带回来，
 * 所以这里按状态过滤掉已删除项，避免删了还留在列表里。
 */
import { Link } from "@tanstack/react-router";
import { Trash2 } from "lucide-react";

import { confirmDialog } from "@/components/confirm-dialog-host";
import { Button } from "@/components/ui/button";
import {
  useAllProjectSummaries,
  useSoftDeleteProject,
} from "@/lib/queries/projects";
import type { ProjectSummary } from "@/types/project";

interface StudioProjectListProps {
  /** 项目行的目标路由，例如 `/studio/$project`（画布首页传 `/canvas/$project`）。 */
  to: "/studio/$project" | "/canvas/$project";
}

export function StudioProjectList({ to }: StudioProjectListProps) {
  const { data: projects, isLoading } = useAllProjectSummaries();
  const softDelete = useSoftDeleteProject();

  const visible = (projects ?? []).filter((p) => p.status !== "deleted");

  async function remove(project: ProjectSummary) {
    const confirmed = await confirmDialog({
      title: "删除作品",
      description: `确定删除《${project.name}》吗？项目会移入回收站，内部端仍可恢复。`,
      confirmText: "删除",
      confirmVariant: "destructive",
    });
    if (confirmed) softDelete.mutate(project.id);
  }

  if (isLoading) {
    return <div className="text-sm text-muted-foreground">正在读取…</div>;
  }
  if (visible.length === 0) {
    return (
      <div className="rounded-xl border border-dashed border-border/60 px-4 py-6 text-center text-sm text-muted-foreground">
        还没有作品，上传一个剧本开始吧。
      </div>
    );
  }

  return (
    <ul className="divide-y divide-border/50 overflow-hidden rounded-xl border border-border/60 bg-card">
      {visible.map((project) => (
        <li key={project.id} className="flex items-center">
          <Link
            to={to}
            params={{ project: project.id }}
            className="flex min-w-0 flex-1 items-center justify-between gap-3 px-4 py-3 transition-colors hover:bg-muted/40"
          >
            <span className="min-w-0 flex-1 truncate text-sm font-medium">
              {project.name}
            </span>
            <span className="shrink-0 text-xs text-muted-foreground">
              {project.episodeCount ? `${project.episodeCount} 集` : "未拆分"}
            </span>
          </Link>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            aria-label={`删除《${project.name}》`}
            title="删除"
            className="mr-2 shrink-0 text-muted-foreground hover:text-destructive"
            disabled={softDelete.isPending}
            onClick={() => void remove(project)}
          >
            <Trash2 className="size-4" />
          </Button>
        </li>
      ))}
    </ul>
  );
}
