"""「资产参考图」关卡的缺口计算：项目里该有的图，缺哪张补哪张。

这个关卡插在「脚本生成」与「草图生成」之间，把项目场景库里的每个场景、每个道具
的参考图补齐，再进入需要它们的阶段：

* 草图只吃场景图（``allowed_kinds={"scene"}``）；
* 渲染首帧同时吃场景图与道具图。

两类图因此都是「上游输入」：缺了它们，草图/首帧就只能靠文字想象，人物服装、道具
外观、环境空间在镜头之间就会漂移。存量项目与后期新增的场景变体都按「缺哪张补哪张」
处理，不回溯、不批量扫库。

本模块只负责**判定与排序**，不碰模型、网关与生成参数；真正入队的是
:func:`novelvideo.pipeline_auto._dispatch_asset_references`，payload 逐字段复刻
REST 端点（``scenes.py`` / ``props.py``）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from novelvideo.utils.path_resolver import (
    canonical_prop_reference_path,
    canonical_scene_master_path,
    canonical_scene_reverse_master_path,
    canonical_scene_spatial_layout_path,
)

logger = logging.getLogger(__name__)

#: 自动资产参考图总开关（``0/false/off/no`` 关闭；关闭后该关卡判定为已完成）。
ASSET_REFERENCES_ENV = "DRAMACLAW_AUTO_ASSET_REFERENCES"

#: 一个场景需要的静态参考图，顺序即生成顺序（master 是其余两张的视觉锚）。
SCENE_REFERENCE_KINDS: tuple[str, ...] = ("master", "reverse_master", "spatial_layout")

#: 以 ``master`` 为视觉锚的场景参考图：master 不在时生成器直接报「请先生成
#: master」，发出去只会白跑一趟再重来。
_SCENE_MASTER_DEPENDENT_KINDS: frozenset[str] = frozenset(
    {"reverse_master", "spatial_layout"}
)

_SCENE_KIND_LABELS = {
    "master": "正面",
    "reverse_master": "反面",
    "spatial_layout": "空间布局",
}


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() not in {"0", "false", "off", "no"}


def asset_references_enabled() -> bool:
    """该关卡是否参与 ``next_step`` 判定与自动派发。"""

    return _env_flag(ASSET_REFERENCES_ENV, True)


@dataclass(frozen=True, slots=True)
class AssetReferenceGap:
    """一张缺失的资产参考图。"""

    kind: str  # "scene" | "pano" | "prop"
    name: str  # 场景名 / 道具名
    detail: str = ""  # 场景参考图的子类型（master/reverse_master/spatial_layout）

    @property
    def signature(self) -> str:
        if self.kind == "scene":
            return f"dispatch:asset_ref:scene:{self.name}:{self.detail}"
        if self.kind == "pano":
            return f"dispatch:asset_ref:pano:{self.name}"
        return f"dispatch:asset_ref:prop:{self.name}"

    @property
    def label(self) -> str:
        if self.kind == "scene":
            return f"场景「{self.name}」{_SCENE_KIND_LABELS.get(self.detail, self.detail)}图"
        if self.kind == "pano":
            return f"场景「{self.name}」360 全景图"
        return f"道具「{self.name}」参考图"

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "name": self.name,
            "detail": self.detail,
            "label": self.label,
            "signature": self.signature,
        }


def scene_reference_gaps(project_dir: Path, scene_name: str) -> list[AssetReferenceGap]:
    """某个场景缺的静态参考图（正面 / 反面 / 空间布局）。"""

    canonical = {
        "master": canonical_scene_master_path,
        "reverse_master": canonical_scene_reverse_master_path,
        "spatial_layout": canonical_scene_spatial_layout_path,
    }
    return [
        AssetReferenceGap("scene", scene_name, kind)
        for kind in SCENE_REFERENCE_KINDS
        if not canonical[kind](project_dir, scene_name).exists()
    ]


def scene_master_ready(project_dir: Path, scene_name: str) -> bool:
    """场景正面主图是否已落盘（其余两张场景图与 360 全景的视觉锚）。"""

    return canonical_scene_master_path(project_dir, scene_name).exists()


def gap_dependency_ready(project_dir: Path, gap: AssetReferenceGap) -> bool:
    """这条缺口的前置产物是否已就位；没就位就先别发，发了必然白跑。

    ``master`` 是所有场景图的视觉锚，另外两类场景图（``reverse_master`` /
    ``spatial_layout``）没有它就生成不了——生成器会直接报「请先生成 master」。
    360 全景虽然名义上有 ``pano_from_text`` 兜底，但只有 ``pano_from_master``
    才吃得到 master/reverse 的视觉锚与 overlap 分析，master 没落盘就派发等于把
    片场环境换成一次文生想象，与场景正面图不一致。

    所以同一个场景先只发 ``master``，等它落盘后的下一轮再发依赖它的那几张：跨场景
    仍然整批并行，只是同一场景内不再把注定的失败和注定降级的全景发出去。
    """

    if gap.kind == "scene" and gap.detail in _SCENE_MASTER_DEPENDENT_KINDS:
        return scene_master_ready(project_dir, gap.name)
    if gap.kind == "pano":
        return scene_master_ready(project_dir, gap.name)
    return True


def scene_pano_ready(project_dir: Path, scene_name: str) -> bool:
    """场景 360 全景是否已生成（``director_worlds/<场景>/v1/pano_360.png``）。"""

    from novelvideo.director_world.stage_manifest import resolve_pano_path

    try:
        return resolve_pano_path(project_dir, scene_name) is not None
    except Exception:  # noqa: BLE001 - 清单损坏不该让整条状态判定失败
        logger.warning("scene pano manifest unreadable scene=%s", scene_name, exc_info=True)
        return False


def prop_reference_ready(project_dir: Path, prop_name: str) -> bool:
    return canonical_prop_reference_path(project_dir, prop_name).exists()


def compute_asset_reference_gaps(
    project_dir: Path,
    *,
    scenes: Sequence[Any] | None,
    props: Sequence[Any] | None,
) -> list[AssetReferenceGap]:
    """按生成顺序列出所有缺失的资产参考图。

    顺序：逐个场景（正面 → 反面 → 空间布局 → 360），再逐个道具。同一场景内先出
    master，360 的 ``pano_from_master`` 才拿得到正面视觉锚。
    """

    gaps: list[AssetReferenceGap] = []
    for scene in scenes or ():
        name = str(getattr(scene, "name", "") or "").strip()
        if not name:
            continue
        gaps.extend(scene_reference_gaps(project_dir, name))
        if not scene_pano_ready(project_dir, name):
            gaps.append(AssetReferenceGap("pano", name))
    for prop in props or ():
        name = str(getattr(prop, "name", "") or "").strip()
        if not name:
            continue
        if not prop_reference_ready(project_dir, name):
            gaps.append(AssetReferenceGap("prop", name))
    return gaps


async def asset_reference_gaps_for_store(store: Any, project_dir: Path) -> list[AssetReferenceGap]:
    """``compute_asset_reference_gaps`` 的取数封装（场景 + 道具全部来自项目库）。

    轻量 store（只实现角色/分集的桩）没有资产表：那种项目就没有资产参考图要求，
    按「无缺口」处理而不是抛错。
    """

    if not asset_references_enabled():
        return []
    list_scenes = getattr(store, "list_scenes", None)
    list_props = getattr(store, "list_props", None)
    if list_scenes is None or list_props is None:
        return []
    scenes = await list_scenes()
    props = await list_props()
    return compute_asset_reference_gaps(project_dir, scenes=scenes, props=props)
