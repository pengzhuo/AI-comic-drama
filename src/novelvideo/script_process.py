"""剧本处理（流程包阶段 1—2）产物的读写与轻量校验。

一键流程在 ``ingest_fast`` 之后先执行「剧本处理」阶段：按内置的
「通用AI影视制作流程包」对导入的剧本做全文读审与制作剧本化，产物落到项目
目录下，供后续阶段（角色提取、分集规划、逐行脚本）复用。

产物布局::

    <project_dir>/script_process/
        result.json             # 结构化结果 + 元数据（完成标志位）
        production_script.md    # 结构化制作剧本全文

``result.json`` 以原子替换方式写入，因此它的存在即代表本阶段已完成；流水线
状态计算与一键调度都只依赖该文件。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

RESULT_VERSION = 1

SCRIPT_PROCESS_DIRNAME = "script_process"
RESULT_FILENAME = "result.json"
PRODUCTION_SCRIPT_FILENAME = "production_script.md"

#: 注入下游提示词时的最大字符数；超出的摘要会被截断并标注。
DOWNSTREAM_DIGEST_MAX_CHARS = 1800


def script_process_dir(project_dir: str | Path) -> Path:
    return Path(project_dir) / SCRIPT_PROCESS_DIRNAME


def result_path(project_dir: str | Path) -> Path:
    return script_process_dir(project_dir) / RESULT_FILENAME


def production_script_path(project_dir: str | Path) -> Path:
    return script_process_dir(project_dir) / PRODUCTION_SCRIPT_FILENAME


def has_script_process_result(project_dir: str | Path) -> bool:
    """``result.json`` 是否存在；这是本阶段的唯一完成标志。"""

    try:
        return result_path(project_dir).is_file()
    except OSError:
        return False


def load_script_process_result(project_dir: str | Path) -> dict[str, Any] | None:
    """读取结构化结果；缺失或损坏时返回 ``None``（不抛错）。"""

    path = result_path(project_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


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


def save_script_process_result(
    project_dir: str | Path,
    *,
    payload: dict[str, Any],
    production_script: str,
) -> Path:
    """原子写入制作剧本全文与结构化结果，并返回结果文件路径。

    ``production_script.md`` 先落盘，``result.json`` 最后落盘——后者是完成标志。
    """

    _atomic_write_text(production_script_path(project_dir), production_script)
    body = dict(payload)
    body.setdefault("version", RESULT_VERSION)
    body["production_script_path"] = PRODUCTION_SCRIPT_FILENAME
    _atomic_write_text(
        result_path(project_dir),
        json.dumps(body, ensure_ascii=False, indent=2),
    )
    return result_path(project_dir)


def read_production_script(project_dir: str | Path) -> str:
    """读取制作剧本全文；缺失时返回空串。"""

    try:
        return production_script_path(project_dir).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return ""


def build_downstream_digest(
    project_dir: str | Path,
    *,
    max_chars: int = DOWNSTREAM_DIGEST_MAX_CHARS,
) -> str:
    """生成注入下游提示词的紧凑摘要（理解账 + 人物 + 缺口）。

    下游的逐行脚本标注只需要“全局理解”这一层，不需要整份制作剧本；
    因此这里只回传理解账、人物表与未决项，并做长度截断。
    """

    result = load_script_process_result(project_dir)
    if not result:
        return ""

    parts: list[str] = []
    understanding = str(result.get("understanding") or "").strip()
    if understanding:
        parts.append(f"## 剧本理解账\n{understanding}")

    characters = result.get("characters") or []
    if isinstance(characters, list) and characters:
        lines = ["## 人物（流程包补全）"]
        for item in characters:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            bits = [name]
            aliases = item.get("aliases") or []
            if isinstance(aliases, list) and aliases:
                bits.append("别名：" + "、".join(str(a) for a in aliases if str(a).strip()))
            role = str(item.get("role") or "").strip()
            if role:
                bits.append(f"定位：{role}")
            lines.append("- " + "；".join(bits))
        if len(lines) > 1:
            parts.append("\n".join(lines))

    gaps = result.get("gaps") or []
    if isinstance(gaps, list) and gaps:
        lines = ["## 未决项（不得脑补，遇到请保守处理）"]
        lines.extend(f"- {str(gap).strip()}" for gap in gaps if str(gap).strip())
        if len(lines) > 1:
            parts.append("\n".join(lines))

    digest = "\n\n".join(parts).strip()
    if not digest:
        return ""
    if len(digest) > max_chars:
        digest = digest[: max_chars - 1] + "…"
    return digest


__all__ = [
    "DOWNSTREAM_DIGEST_MAX_CHARS",
    "PRODUCTION_SCRIPT_FILENAME",
    "RESULT_FILENAME",
    "RESULT_VERSION",
    "SCRIPT_PROCESS_DIRNAME",
    "build_downstream_digest",
    "has_script_process_result",
    "load_script_process_result",
    "production_script_path",
    "read_production_script",
    "result_path",
    "save_script_process_result",
    "script_process_dir",
]
