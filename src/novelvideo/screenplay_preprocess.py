"""剧本预处理阶段的产物读写。

一键流程在 ``ingest_fast`` 之后、剧本处理（流程包阶段 1—2）之前，先把导入的剧本
规范化成系统能直接消费的形式（见 :mod:`novelvideo.utils.screenplay_normalize`）：
场景头归一成 ``场号 地点 时间 内/外``，尾部制作标注（``天气：…``）移到独立行，
原文一字不丢。

产物布局::

    <project_dir>/novel.original.txt     # 首次预处理时留档的原始导入文本
    <project_dir>/novel.normalized.txt   # 规范化后的文本（下游实际读取的文件）
    <project_dir>/screenplay/
        preprocess.json                  # 处理结果 + 统计（完成标志位）

``preprocess.json`` 以原子替换方式写入，因此它的存在即代表本阶段已完成；流水线
状态计算与一键调度都只依赖该文件。规范化文本与原文逐字节一致时仍然写标记
（``changed=false``），这样「已确认无需处理」也走同一个阶段。

重新导入时必须先 :func:`clear_preprocess_artifacts`，否则旧的规范化文本会盖住新
导入的内容。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

RESULT_VERSION = 1

PREPROCESS_DIRNAME = "screenplay"
RESULT_FILENAME = "preprocess.json"
NORMALIZED_NOVEL_FILENAME = "novel.normalized.txt"
ORIGINAL_NOVEL_FILENAME = "novel.original.txt"


def preprocess_dir(project_dir: str | Path) -> Path:
    return Path(project_dir) / PREPROCESS_DIRNAME


def result_path(project_dir: str | Path) -> Path:
    return preprocess_dir(project_dir) / RESULT_FILENAME


def normalized_novel_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / NORMALIZED_NOVEL_FILENAME


def original_novel_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / ORIGINAL_NOVEL_FILENAME


def has_preprocess_result(project_dir: str | Path) -> bool:
    """``preprocess.json`` 是否存在；这是本阶段的唯一完成标志。"""

    try:
        return result_path(project_dir).is_file()
    except OSError:
        return False


def load_preprocess_result(project_dir: str | Path) -> dict[str, Any] | None:
    """读取处理结果；缺失或损坏时返回 ``None``（不抛错）。"""

    try:
        raw = result_path(project_dir).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def read_normalized_novel(project_dir: str | Path) -> str:
    """读取规范化文本；缺失时返回空串。"""

    try:
        return normalized_novel_path(project_dir).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def save_preprocess_result(
    project_dir: str | Path,
    *,
    payload: dict[str, Any],
    normalized_text: str,
) -> Path:
    """原子写入规范化文本与结果；``preprocess.json`` 最后落盘（完成标志）。"""

    _atomic_write_text(normalized_novel_path(project_dir), normalized_text)
    body = dict(payload)
    body.setdefault("version", RESULT_VERSION)
    _atomic_write_text(
        result_path(project_dir),
        json.dumps(body, ensure_ascii=False, indent=2),
    )
    return result_path(project_dir)


def backup_original_novel(project_dir: str | Path, text: str) -> Path | None:
    """首次预处理时把原始导入文本留档；已存在则不覆盖。"""

    path = original_novel_path(project_dir)
    if path.exists():
        return None
    _atomic_write_text(path, text)
    return path


def clear_preprocess_artifacts(project_dir: str | Path) -> None:
    """删除规范化产物（重新导入时调用），不存在也不报错。"""

    for path in (
        normalized_novel_path(project_dir),
        result_path(project_dir),
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            continue


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(text)
        os.replace(temp_path, path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
