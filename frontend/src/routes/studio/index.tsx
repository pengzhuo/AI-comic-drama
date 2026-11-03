// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { createFileRoute, useNavigate } from "@tanstack/react-router";

import { StudioProjectList } from "@/features/studio/studio-project-list";
import { StudioUploadCard } from "@/features/studio/studio-upload-card";

export const Route = createFileRoute("/studio/")({
  component: StudioHome,
});

function StudioHome() {
  const navigate = useNavigate();

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">上传剧本，一键成片</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          全程自动：导入剧本 → 拆集 → 生成画面与配音 → 合成成片。每完成一集都能看到成片与费用明细。
        </p>
      </div>

      <StudioUploadCard
        onCreated={(project, options) =>
          void navigate({
            to: "/studio/$project",
            params: { project: project.id },
            // 用户在上传后的粗估确认里点了「开始制作」才自动开跑。
            search: options?.autostart ? { autostart: true } : {},
          })
        }
      />

      <section className="space-y-2">
        <h2 className="text-sm font-semibold text-muted-foreground">我的剧</h2>
        <StudioProjectList to="/studio/$project" />
      </section>
    </div>
  );
}
