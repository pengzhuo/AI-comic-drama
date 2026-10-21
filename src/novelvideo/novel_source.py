"""Canonical novel-source prerequisite checks."""

from __future__ import annotations

from pathlib import Path


NOVEL_IMPORT_REQUIRED_MESSAGE = "请先导入小说"
NOVEL_IMPORT_REQUIRED_CODE = "NOVEL_IMPORT_REQUIRED"


class NovelImportRequiredError(ValueError):
    error_code = NOVEL_IMPORT_REQUIRED_CODE

    def __init__(self) -> None:
        super().__init__(NOVEL_IMPORT_REQUIRED_MESSAGE)


def novel_import_required_response() -> dict[str, str | bool]:
    return {
        "ok": False,
        "code": NOVEL_IMPORT_REQUIRED_CODE,
        "error": NOVEL_IMPORT_REQUIRED_MESSAGE,
    }


def load_raw_imported_novel_content(project_dir: str | Path) -> str | None:
    """原始导入文本（``novel.txt``），不含剧本预处理的结果。"""

    novel_path = Path(project_dir) / "novel.txt"
    if not novel_path.exists():
        return None
    return novel_path.read_text(encoding="utf-8")


def load_imported_novel_content(project_dir: str | Path) -> str | None:
    """系统实际消费的导入文本。

    剧本预处理跑过之后，``novel.normalized.txt`` 就是系统应该读的那一份
    （场景头已归一、尾部标注已拆行）；原文仍留在 ``novel.txt``（以及首次预处理
    时的 ``novel.original.txt`` 留档）里，供展示与比对。
    """

    normalized_path = Path(project_dir) / "novel.normalized.txt"
    try:
        if normalized_path.is_file():
            normalized = normalized_path.read_text(encoding="utf-8")
            if normalized.strip():
                return normalized
    except OSError:
        pass
    return load_raw_imported_novel_content(project_dir)


def has_imported_novel(project_dir: str | Path) -> bool:
    content = load_imported_novel_content(project_dir)
    return bool(content and content.strip())


def require_imported_novel(project_dir: str | Path) -> str:
    content = load_imported_novel_content(project_dir)
    if not content or not content.strip():
        raise NovelImportRequiredError()
    return content


def novel_source_digest(project_dir: str | Path) -> str | None:
    """导入小说的内容指纹；没有（或为空）返回 None。

    ``ingest_fast`` 的任务行只是「进行中」的瞬时记录，完成后过
    ``COMPLETED_TTL`` 就被惰性删除，所以「这份文本是否已经分析过」要落在持久
    产物上：摄入完成时把当时的指纹写进项目配置，之后比对指纹即可，重新上传
    换了文本就自动失效。

    算法与 ``story_analysis.source_sha256`` 保持一致（下游脚本处理结果里的
    ``source_sha256`` 就是它），存量项目才能在缺少配置标记时直接与已有结果
    比对（见 ``pipeline_status._ingest_artifact_digest_matches``）。
    """
    content = load_imported_novel_content(project_dir)
    if not content or not content.strip():
        return None
    from novelvideo.story_analysis import source_sha256

    return source_sha256(content)


def resolve_uploaded_novel_filename(
    project_dir: str | Path,
    imported_content: str,
    *,
    preferred_filename: str | None = None,
) -> str | None:
    """Resolve the upload that produced the durable imported novel.

    New projects persist the exact filename in project_config. For historical
    projects, compare parsed upload content with novel.txt and choose the newest
    exact match.
    """
    from novelvideo.utils.document_parsers import (
        is_supported_novel_path,
        load_novel_text,
    )

    uploads_dir = Path(project_dir) / "uploads"
    if not uploads_dir.is_dir():
        return None

    def valid_candidate(filename: str | None) -> Path | None:
        if not filename or Path(filename).name != filename:
            return None
        candidate = uploads_dir / filename
        if not candidate.is_file() or not is_supported_novel_path(candidate):
            return None
        return candidate

    preferred = valid_candidate(preferred_filename)
    if preferred is not None:
        return preferred.name

    candidates = sorted(
        (
            path
            for path in uploads_dir.iterdir()
            if path.is_file() and is_supported_novel_path(path)
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for candidate in candidates:
        try:
            if load_novel_text(candidate) == imported_content:
                return candidate.name
        except Exception:
            continue
    return None
