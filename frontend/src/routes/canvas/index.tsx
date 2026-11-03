// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { createFileRoute, useNavigate } from "@tanstack/react-router";

import { StudioProjectList } from "@/features/studio/studio-project-list";
import { StudioUploadCard } from "@/features/studio/studio-upload-card";

export const Route = createFileRoute("/canvas/")({
  component: CanvasHome,
});

/**
 * 客户画布入口页：一个项目的列表（与 `/studio` 首页同构）。
 *
 * `/canvas/<项目>` 才是画布本身；这里让没带项目来的用户挑一个，或上传剧本建一个。
 * 建好之后直接进该项目的画布（画布入口没有「一键成片」运行页，所以不带 autostart）。
 */
function CanvasHome() {
  const navigate = useNavigate();

  return (
    <div className="mx-auto w-full max-w-5xl space-y-6 px-4 py-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">捏画工作台</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          选择一个作品打开捏画，或上传剧本创建新的作品。
        </p>
      </div>

      <StudioUploadCard
        onCreated={(project) =>
          void navigate({
            to: "/canvas/$project",
            params: { project: project.id },
          })
        }
      />

      <section className="space-y-2">
        <h2 className="text-sm font-semibold text-muted-foreground">我的剧</h2>
        <StudioProjectList to="/canvas/$project" />
      </section>
    </div>
  );
}
