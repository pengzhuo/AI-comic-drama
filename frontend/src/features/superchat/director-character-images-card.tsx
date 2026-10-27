// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { useMemo, useRef, useState } from "react";
import { ImagePlus, Loader2, SkipForward } from "lucide-react";

import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { p } from "@/lib/api-path";
import {
  useCharacters,
  useUploadIdentityImage,
  useUploadPortrait,
} from "@/lib/queries/characters";
import { usePipelineStatus } from "@/lib/queries/episodes";
import { cn } from "@/lib/utils";
import type { Character } from "@/types/character";

/** 一键成片在「角色图片指定」上的两个暂停点。 */
export type DirectorGateKind = "character_images" | "identity_images";

/**
 * 与后端 ``pipeline_status.character_needs_portrait`` 同一条判定：没有
 * ``face_prompt`` 的角色根本渲染不出肖像，不该出现在卡片里让用户白传图。
 */
export function characterNeedsPortrait(character: Character): boolean {
  if (!character.face_prompt?.trim()) return false;
  return Boolean(character.is_main) || (character.identity_ids?.length ?? 0) > 0;
}

/** 记住用户的答案（上传或跳过）并让暂停的成片继续跑。 */
export async function confirmDirectorGate(
  project: string,
  gate: DirectorGateKind,
  episode: number,
): Promise<void> {
  await api
    .post(p`api/v1/projects/${project}/chat/director-auto/gates/${gate}/confirm`, {
      json: { episode },
    })
    .json<unknown>();
}

function Thumb({ url, label }: { url?: string | null; label: string }) {
  const [broken, setBroken] = useState(false);
  const showImage = Boolean(url) && !broken;
  return (
    <span className="relative flex size-11 shrink-0 items-center justify-center overflow-hidden rounded-xl bg-black/[0.06] ring-1 ring-black/[0.06]">
      {showImage ? (
        <img
          src={url ?? ""}
          alt={label}
          className="size-full object-cover"
          onError={() => setBroken(true)}
        />
      ) : (
        <ImagePlus className="size-4 text-muted-foreground/70" />
      )}
    </span>
  );
}

type RowProps = {
  project: string;
  character: Character;
  onUploaded: () => void;
};

function PortraitRow({ project, character, onUploaded }: RowProps) {
  const upload = useUploadPortrait(project, character.name);
  const inputRef = useRef<HTMLInputElement>(null);
  const [done, setDone] = useState(false);
  const [failed, setFailed] = useState(false);

  const pick = (file: File | undefined) => {
    if (!file) return;
    setFailed(false);
    upload.mutate(file, {
      onSuccess: () => {
        setDone(true);
        onUploaded();
      },
      onError: () => setFailed(true),
    });
  };

  return (
    <div className="flex items-center gap-3 px-3 py-2.5">
      <Thumb url={character.portrait_url} label={`${character.name} 肖像`} />
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm leading-5 text-foreground/90">{character.name}</div>
        <div className="truncate text-xs leading-4 text-muted-foreground">
          {done ? "已指定肖像" : failed ? "上传失败，请重试" : "未指定，将由 AI 生成"}
        </div>
      </div>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="h-8 shrink-0 rounded-full px-3 text-xs text-muted-foreground hover:bg-black/[0.08] hover:text-foreground"
        disabled={upload.isPending}
        onClick={() => inputRef.current?.click()}
      >
        {upload.isPending ? <Loader2 className="size-3.5 animate-spin" /> : "上传"}
      </Button>
      <input
        ref={inputRef}
        type="file"
        accept="image/*"
        className="hidden"
        onChange={(event) => {
          pick(event.target.files?.[0]);
          event.target.value = "";
        }}
      />
    </div>
  );
}

function IdentityRow({
  project,
  character,
  identityName,
  onUploaded,
}: {
  project: string;
  character: string;
  identityName: string;
  onUploaded: () => void;
}) {
  const upload = useUploadIdentityImage(project, character);
  const inputRef = useRef<HTMLInputElement>(null);
  const [done, setDone] = useState(false);
  const [failed, setFailed] = useState(false);

  const pick = (file: File | undefined) => {
    if (!file) return;
    setFailed(false);
    upload.mutate(
      { identityName, file },
      {
        onSuccess: () => {
          setDone(true);
          onUploaded();
        },
        onError: () => setFailed(true),
      },
    );
  };

  return (
    <div className="flex items-center gap-3 px-3 py-2.5">
      <Thumb label={`${character} ${identityName} 身份图`} />
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm leading-5 text-foreground/90">
          {character}
          <span className="text-muted-foreground">「{identityName}」</span>
        </div>
        <div className="truncate text-xs leading-4 text-muted-foreground">
          {done ? "已指定身份图" : failed ? "上传失败，请重试" : "未指定，将由 AI 生成"}
        </div>
      </div>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="h-8 shrink-0 rounded-full px-3 text-xs text-muted-foreground hover:bg-black/[0.08] hover:text-foreground"
        disabled={upload.isPending}
        onClick={() => inputRef.current?.click()}
      >
        {upload.isPending ? <Loader2 className="size-3.5 animate-spin" /> : "上传"}
      </Button>
      <input
        ref={inputRef}
        type="file"
        accept="image/*"
        className="hidden"
        onChange={(event) => {
          pick(event.target.files?.[0]);
          event.target.value = "";
        }}
      />
    </div>
  );
}

export function DirectorCharacterImagesCard({
  project,
  gate,
  episode,
  busy = false,
  onResolved,
}: {
  project: string;
  gate: DirectorGateKind;
  episode: number;
  busy?: boolean;
  onResolved: () => void;
}) {
  const wantsPortraits = gate === "character_images";
  const charactersQuery = useCharacters(project, wantsPortraits);
  const pipelineQuery = usePipelineStatus(wantsPortraits ? "" : project);
  const [answered, setAnswered] = useState<string[]>([]);
  const [confirming, setConfirming] = useState(false);

  const portraitRows = useMemo(
    () => (charactersQuery.data?.data ?? []).filter(characterNeedsPortrait),
    [charactersQuery.data],
  );
  const identityRows = useMemo(() => {
    const missing = pipelineQuery.data?.data?.episode_status?.identities_missing ?? [];
    return missing.map((item) => ({
      key: `${item.character}::${item.identity_id}`,
      character: item.character,
      identityName: item.identity_name,
    }));
  }, [pipelineQuery.data]);

  const total = wantsPortraits ? portraitRows.length : identityRows.length;
  const answeredCount = answered.length;
  const loading = wantsPortraits ? charactersQuery.isLoading : pipelineQuery.isLoading;

  const resolve = async () => {
    if (confirming) return;
    setConfirming(true);
    try {
      await confirmDirectorGate(project, gate, episode);
      onResolved();
    } finally {
      setConfirming(false);
    }
  };

  const markAnswered = (key: string) =>
    setAnswered((current) => (current.includes(key) ? current : [...current, key]));

  return (
    <div className="border-b border-black/10 bg-black/[0.03] px-4 pb-3 pt-3 text-sm">
      <div className="mb-2 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-sm font-medium leading-5 text-foreground">
            {wantsPortraits ? "角色图片指定" : "身份图指定"}
          </div>
          <div className="mt-0.5 text-xs leading-5 text-muted-foreground">
            {wantsPortraits
              ? "上传你自己的角色肖像，一键成片会直接用这张图，不再 AI 生成；不传的角色照旧由 AI 生成。"
              : `第 ${episode} 集新增的身份可以在这里指定身份图，不传的照旧由 AI 生成。`}
          </div>
        </div>
        {total > 0 && (
          <span className="shrink-0 rounded-full bg-black/[0.05] px-2 py-1 text-[11px] tabular-nums text-muted-foreground">
            {answeredCount}/{total}
          </span>
        )}
      </div>

      <div className="mb-2 overflow-hidden rounded-2xl bg-black/[0.035] ring-1 ring-black/[0.07]">
        {loading ? (
          <div className="flex items-center gap-2 px-3 py-4 text-xs text-muted-foreground">
            <Loader2 className="size-3.5 animate-spin" />
            正在读取…
          </div>
        ) : total === 0 ? (
          <div className="px-3 py-4 text-xs text-muted-foreground">
            {wantsPortraits ? "没有需要指定肖像的角色。" : "本集没有待指定身份图的身份。"}
          </div>
        ) : (
          <div className="max-h-64 divide-y divide-black/[0.06] overflow-y-auto">
            {wantsPortraits
              ? portraitRows.map((character) => (
                  <PortraitRow
                    key={character.name}
                    project={project}
                    character={character}
                    onUploaded={() => markAnswered(character.name)}
                  />
                ))
              : identityRows.map((row) => (
                  <IdentityRow
                    key={row.key}
                    project={project}
                    character={row.character}
                    identityName={row.identityName}
                    onUploaded={() => markAnswered(row.key)}
                  />
                ))}
          </div>
        )}
      </div>

      <div className="flex items-center justify-end gap-2">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className={cn(
            "h-8 rounded-full px-3 text-xs text-muted-foreground hover:bg-black/[0.08] hover:text-foreground",
          )}
          disabled={busy || confirming}
          onClick={() => void resolve()}
        >
          <SkipForward className="size-3.5" />
          跳过，由 AI 生成
        </Button>
        <Button
          type="button"
          size="sm"
          className="h-8 rounded-full px-3.5 text-xs"
          disabled={busy || confirming}
          onClick={() => void resolve()}
        >
          {confirming ? <Loader2 className="size-3.5 animate-spin" /> : null}
          继续制作
        </Button>
      </div>
    </div>
  );
}
