// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { Link, createFileRoute, useNavigate } from "@tanstack/react-router";

import { StudioUploadCard } from "@/features/studio/studio-upload-card";
import { useAllProjectSummaries } from "@/lib/queries/projects";

export const Route = createFileRoute("/studio/")({
  component: StudioHome,
});

function StudioHome() {
  const navigate = useNavigate();
  const { data: projects, isLoading } = useAllProjectSummaries();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">上传剧本，一键成片</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          全程自动：导入剧本 → 拆集 → 生成画面与配音 → 合成成片。每完成一集都能看到成片与费用明细。
        </p>
      </div>

      <StudioUploadCard
        onCreated={(project) =>
          void navigate({
            to: "/studio/$project",
            params: { project: project.id },
            // 让工作台页面在导入完成后自动开跑「一键成片」。
            search: { autostart: true },
          })
        }
      />

      <section className="space-y-2">
        <h2 className="text-sm font-semibold text-muted-foreground">我的剧</h2>
        {isLoading ? (
          <div className="text-sm text-muted-foreground">正在读取…</div>
        ) : (projects?.length ?? 0) === 0 ? (
          <div className="rounded-xl border border-dashed border-border/60 px-4 py-6 text-center text-sm text-muted-foreground">
            还没有作品，上传一个剧本开始吧。
          </div>
        ) : (
          <ul className="divide-y divide-border/50 overflow-hidden rounded-xl border border-border/60 bg-card">
            {projects?.map((project) => (
              <li key={project.id}>
                <Link
                  to="/studio/$project"
                  params={{ project: project.id }}
                  className="flex items-center justify-between gap-3 px-4 py-3 transition-colors hover:bg-muted/40"
                >
                  <span className="min-w-0 flex-1 truncate text-sm font-medium">
                    {project.name}
                  </span>
                  <span className="shrink-0 text-xs text-muted-foreground">
                    {project.episodeCount ? `${project.episodeCount} 集` : "未拆分"}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
