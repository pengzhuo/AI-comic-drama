"""Celery runner for the 剧本预处理 stage.

Runs between ``ingest_fast`` and ``script_process``: it normalizes the imported
screenplay into the shape the rest of the pipeline consumes (canonical
``场号 地点 时间 内/外`` scene headings, trailing production notes such as
``天气：…`` on their own line) and only then hands off to the skill stage.

The stage is deterministic and idempotent: no model calls, no content loss, and
an already-canonical script is byte-identical to its input.  It also records the
normalized text durably (``novel.normalized.txt`` + ``screenplay/preprocess.json``)
so ``novel_source.load_imported_novel_content`` — the single read funnel every
later stage uses — returns the processed text.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

from novelvideo.i18n_message import MessageLike, lmsg
from novelvideo.project_context import ProjectContext
from novelvideo.task_backend.cancel import await_envelope_with_cancel_watch
from novelvideo.task_backend.registry import register_project_task_runner
from novelvideo.task_state import get_task_manager

logger = logging.getLogger(__name__)

TASK_TYPE = "screenplay_preprocess"


def run_screenplay_preprocess(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any] | None:
    return asyncio.run(
        await_envelope_with_cancel_watch(
            _run_screenplay_preprocess(envelope, ctx),
            envelope,
            task_type=TASK_TYPE,
        )
    )


async def _run_screenplay_preprocess(
    envelope: dict[str, Any], ctx: ProjectContext
) -> dict[str, Any]:
    from novelvideo.novel_source import load_raw_imported_novel_content
    from novelvideo.screenplay_preprocess import (
        backup_original_novel,
        has_preprocess_result,
        load_preprocess_result,
        save_preprocess_result,
    )
    from novelvideo.story_analysis import source_sha256
    from novelvideo.utils.screenplay_normalize import normalize_screenplay_text

    payload = envelope.get("payload") or {}
    output_dir = str(payload.get("output_dir") or ctx.output_dir)
    force = bool(payload.get("force"))
    apply_to_episodes = bool(payload.get("apply_to_episodes", True))
    manager = get_task_manager()

    def update(progress: float | None, task: MessageLike) -> None:
        manager.update_progress_for_project(
            ctx,
            TASK_TYPE,
            0,
            progress=progress,
            current_task=task,
            logs=[task],
        )

    if not force and has_preprocess_result(output_dir):
        update(None, lmsg("tasks.progress.screenplayPreprocess.skip", "已存在预处理结果，本次跳过（force=true 可重跑）"))
        return {
            "ok": True,
            "skipped": True,
            "output_dir": output_dir,
            "result": load_preprocess_result(output_dir),
        }

    raw_text = load_raw_imported_novel_content(output_dir)
    if not raw_text or not raw_text.strip():
        raise ValueError("请先导入小说")

    update(
        0.05,
        lmsg(
            "tasks.progress.screenplayPreprocess.start",
            "开始预处理剧本（规范化场景头 → 交给剧本处理技能）...",
        ),
    )

    normalization = normalize_screenplay_text(raw_text)
    update(
        0.35,
        lmsg(
            "tasks.progress.screenplayPreprocess.normalize",
            "场景头已归一：合并分行场头 {merged} 处、重写 {rewritten} 处（场景头 {headings} 个）",
            merged=normalization.stats.get("split_headings_merged", 0),
            rewritten=normalization.stats.get("headings_rewritten", 0),
            headings=normalization.scene_headings,
        ),
    )

    episodes_updated = 0
    episodes_skipped = 0
    if apply_to_episodes:
        episodes_updated, episodes_skipped = await _sync_episode_contents(
            ctx, raw_text, normalization.text
        )
        if episodes_updated or episodes_skipped:
            update(
                0.6,
                lmsg(
                    "tasks.progress.screenplayPreprocess.episodes",
                    "分集正文已同步 {updated} 集，另有 {skipped} 集因手工修改被跳过",
                    updated=episodes_updated,
                    skipped=episodes_skipped,
                ),
            )

    backup_original_novel(output_dir, raw_text)
    save_preprocess_result(
        output_dir,
        normalized_text=normalization.text,
        payload={
            "task_type": TASK_TYPE,
            "changed": normalization.changed,
            "episodes": normalization.episodes,
            "scene_headings": normalization.scene_headings,
            "stats": dict(normalization.stats),
            "samples": normalization.samples,
            "source_sha256": source_sha256(raw_text),
            "normalized_sha256": source_sha256(normalization.text),
            "apply_to_episodes": apply_to_episodes,
            "episodes_updated": episodes_updated,
            "episodes_skipped_edited": episodes_skipped,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    # 下游读到的是规范化文本，摄入指纹必须跟着换：``ingest_fast`` 的任务行一
    # 小时后过期，只剩指纹在判断「已摄入」，不改会让项目退回「小说摄入」。
    from novelvideo.task_backend.runners.ingest import remember_ingest_completion

    remember_ingest_completion(ctx)

    await _enqueue_script_process(ctx)

    update(
        1.0,
        lmsg("tasks.progress.screenplayPreprocess.done", "剧本预处理完成"),
    )
    return {
        "ok": True,
        "output_dir": output_dir,
        "changed": normalization.changed,
        "episodes": normalization.episodes,
        "scene_headings": normalization.scene_headings,
        "stats": dict(normalization.stats),
        "episodes_updated": episodes_updated,
        "episodes_skipped_edited": episodes_skipped,
    }


async def _sync_episode_contents(
    ctx: ProjectContext, raw_text: str, normalized_text: str
) -> tuple[int, int]:
    """把已存在的分集正文换成规范化文本，返回 ``(更新数, 跳过数)``。

    存量项目导入时还没有这个阶段，``episodes.raw_content`` 存的是带标注的原始场
    头。逐集比对：只有当前正文与「原始文本里那一集」完全一致（等价用户没手改）
    才替换；任何不一致都跳过并计数，绝不覆盖人工编辑过的正文。
    """

    from novelvideo.cognee.chapter_detector import ChapterDetector
    from novelvideo.task_backend.runners.graph_build import _load_store

    def chapters(text: str) -> dict[int, str]:
        try:
            detected = ChapterDetector().detect(text)
        except Exception:  # noqa: BLE001 - 分集识别失败就不同步，不影响预处理结果
            logger.exception("screenplay preprocess: chapter detection failed")
            return {}
        return {int(info.number): info.content for info in detected}

    raw_chapters = chapters(raw_text)
    new_chapters = chapters(normalized_text)
    if not raw_chapters or not new_chapters:
        return 0, 0

    store = await _load_store(ctx)
    updated = 0
    skipped = 0
    try:
        for number, normalized_chapter in new_chapters.items():
            raw_chapter = raw_chapters.get(number)
            if raw_chapter is None or raw_chapter == normalized_chapter:
                continue
            try:
                current = await store.load_episode_content(number)
            except Exception:  # noqa: BLE001 - 没分集 / 读取失败都算不同步
                continue
            if current is None:
                continue
            if _content_fingerprint(current) != _content_fingerprint(raw_chapter):
                skipped += 1
                continue
            await store.save_episode_content(number, normalized_chapter.strip())
            updated += 1
    finally:
        try:
            await store.close()
        except Exception:  # noqa: BLE001 - 关闭失败不该影响阶段结果
            logger.exception("screenplay preprocess: store close failed")
    return updated, skipped


def _content_fingerprint(text: str) -> str:
    """忽略空白差异的正文指纹，用于判断分集正文是否被手工改过。"""

    return re.sub(r"\s+", " ", text or "").strip()


async def _enqueue_script_process(ctx: ProjectContext) -> None:
    """Hand the normalized screenplay to the skill stage.

    Best effort: the preprocessing artifacts are already durable, so a rejected
    hand-off must never fail the stage.  A duplicate is harmless (the stage skips
    itself when a result exists, and the one-click coordinator dispatches the
    same stage on its own).
    """

    from novelvideo.novel_source import has_imported_novel
    from novelvideo.script_process import has_script_process_result
    from novelvideo.task_backend.client import enqueue_project_task

    try:
        if not has_imported_novel(ctx.output_dir):
            return
        if has_script_process_result(ctx.output_dir):
            return
        existing = get_task_manager().get_task_for_project(ctx, "script_process", 0)
        if existing is not None and existing.status in _ACTIVE_TASK_STATUSES:
            return
        await enqueue_project_task(
            ctx,
            task_type="script_process",
            product_surface="mainline",
            queue_kind="default",
            episode=0,
            payload={"output_dir": str(ctx.output_dir)},
        )
    except Exception:  # noqa: BLE001 - the preprocessing result is authoritative
        logger.exception("failed to hand off script processing after preprocessing")


_ACTIVE_TASK_STATUSES = frozenset(
    {"submitting", "queued", "pending", "starting", "running"}
)


register_project_task_runner(TASK_TYPE, run_screenplay_preprocess)
