"""Render / sketch / action-storyboard prompt strategies.

Split out of ``prompt_builder.py`` verbatim. The shared dataclasses,
``PromptComponents`` and small helpers remain in ``prompt_builder`` and are
imported here; ``prompt_builder`` imports these strategies back for its
``_STRATEGIES`` map. No behavior changed in the move.
"""

from __future__ import annotations

import hashlib  # noqa: F401
import json  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
from dataclasses import dataclass, field, replace  # noqa: F401
from enum import Enum  # noqa: F401
from typing import Any, Dict, List, Optional  # noqa: F401

from pypinyin import Style, pinyin  # noqa: F401

from novelvideo.config import IMAGE_DEFAULT_STYLE, get_style_preset  # noqa: F401
from novelvideo.generators.prompt_builder import (
    PromptContext,
    PromptMode,
    _uses_gpt_image_sketch_profile,
)
from novelvideo.generators.prompt_components import (
    PromptComponents,
    _panel_ar_hint,
    _resolve_prop_marker_tags,
)
from novelvideo.models import (  # noqa: F401
    beat_scene_id,
    real_detected_identities,
    real_detected_props,
)
from novelvideo.services.style_service import StyleService  # noqa: F401


class RenderModeStrategy:
    """Render 模式：基于草图渲染（使用文本格式，效果更稳定）。"""

    def build(self, ctx: PromptContext, components: PromptComponents) -> str:
        rows, cols = ctx.grid.rows, ctx.grid.cols
        total = ctx.grid.total_panels
        panel_hint = (
            _panel_ar_hint(ctx.grid.aspect_ratio, rows, cols)
            if ctx.grid.aspect_ratio
            else "SQUARE (1:1)"
        )
        preset_scope = (
            "Scene preset controls the final rendering style of the whole image: environment, lighting, skin texture, material response, and overall image finish. "
            "Composite / identity-sheet and portrait-only references lock character identity and appearance details only: face identity, facial geometry, hair identity, silhouette, and costume details. "
            "Do NOT inherit or preserve a reference image's CG, beauty-filter, illustrated, or over-retouched rendering look; re-render the same person in the project's scene preset."
        )

        # Render 专用开头：一句话讲清任务 + 对象 + 比例
        render_opening = f"""Colorize this {rows}×{cols} storyboard SKETCH (first attached image / Image 1) into a full-color continuous image with {total} seamless regions. Each panel MUST be {panel_hint}.

STYLE: {ctx.style.style_keywords}
{preset_scope}

!!! MANDATORY GRID FORMAT: {rows} ROWS × {cols} COLUMNS !!!
(This means {rows} horizontal rows stacked vertically, each row containing {cols} panels side by side)

Image 1 / SKETCH IS the base drawing — preserve ALL composition, crop, poses, and camera angles exactly. Other reference images must not change the sketch layout.
⚠️ HARD CONSTRAINT: {total-1} regions = FAIL. {total+1} regions = FAIL. Only {total} = PASS.
ONE continuous image. ZERO visible boundaries between regions."""

        # Render 模式：严格按 detected_identities 出场顺序，不回退 visual_description 标记
        ordered_chars = components.extract_panel_characters_from_detected(ctx.beats, ctx.characters)
        ctx.resolved_render_chars = list(ordered_chars)
        constraints = components.build_constraints(ctx, include_face_reminder=False)

        parts = [
            render_opening,  # Section 0+1: 合并的任务声明
            components.build_reference_map(
                ctx, ordered_chars, include_face_desc=False, include_silhouette=False
            ),
            components.build_identity_lock(ctx, ordered_chars, compact=True),
            components.build_asset_identity_lock(ctx),
            components.build_fusion_rules(ctx, ordered_chars),
            components.build_time_of_day_rules(ctx),
            components.build_color_identification_map(ctx, ordered_chars),
            components.build_single_beat_render_visual_reference(ctx),
            components.build_panel_roster(ctx),
            constraints,
        ]
        return "\n\n".join(p for p in parts if p)


class SketchModeStrategy:
    """Sketch 模式：伯里曼构造解剖风格 + 颜色编码。"""

    def build(self, ctx: PromptContext, components: PromptComponents) -> str:
        rows, cols = ctx.grid.rows, ctx.grid.cols
        total_panels = ctx.grid.total_panels

        # ASCII 布局（Sketch 模式使用竖屏 panel）
        ascii_layout = components.build_grid_ascii(rows, cols, ctx.grid.is_portrait_panel)

        panel_hint = (
            _panel_ar_hint(ctx.grid.aspect_ratio, rows, cols)
            if ctx.grid.aspect_ratio
            else "SQUARE (1:1)"
        )

        # ----- 先构建角色颜色信息（COLOR LAW 和 intro 都需要） -----
        char_lines = []
        char_names_for_color_law = []  # 用于 COLOR LAW 点名
        prop_lines = []
        local_prop_lines = []
        tag_color_map: dict[str, str] = {}  # tag → color_name（仅角色）
        prop_tag_color_map: dict[str, str] = {}  # prop tag -> marker color
        prop_color_lines: list[str] = []
        identity_tag_map: dict[str, str] = {}  # identity_id → tag
        prop_label_map: dict[str, str] = {}
        director_staging_lines: list[str] = []
        actual_beats_for_chars = ctx.beats[:total_panels]
        prop_tag_panel_map: dict[str, list[int]] = {}
        for panel_idx, beat in enumerate(actual_beats_for_chars, start=1):
            for prop_id in components._collect_prop_marker_ids([beat]):
                prop_tag = components.compute_prop_tag(prop_id)
                prop_tag_panel_map.setdefault(prop_tag, []).append(panel_idx)
        scene_image_refs = PromptComponents.collect_scene_image_refs(ctx)
        has_director_scene_ref_inputs = any(
            PromptComponents._is_director_scene_ref(ref) for ref in scene_image_refs
        )

        def _director_frame_prop_colors() -> dict[str, str]:
            colors: dict[str, str] = {}
            for ref in scene_image_refs:
                if not PromptComponents._is_director_scene_ref(ref):
                    continue
                image_paths = getattr(ref, "image_paths", []) or []
                if not image_paths:
                    continue
                meta_path = os.path.join(os.path.dirname(str(image_paths[0])), "frame_meta.json")
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except Exception:
                    continue
                for prop in meta.get("props") or []:
                    if not isinstance(prop, dict):
                        continue
                    prop_id = str(
                        prop.get("prop_id") or prop.get("name") or prop.get("id") or ""
                    ).strip()
                    marker_color = str(prop.get("marker_color") or "").strip()
                    if prop_id and marker_color:
                        colors.setdefault(prop_id, marker_color)
            return colors

        director_frame_prop_colors = _director_frame_prop_colors()

        ordered_char_names: list[str] = []
        if ctx.characters:
            from novelvideo.utils.identity_resolver import compute_char_tag as _compute_tag

            panel_chars = components._collect_char_identity_ids(
                actual_beats_for_chars,
                use_detected_identities=False,
            )
            ordered_char_names = [name for name in panel_chars if name in ctx.characters]
            for char_name in panel_chars:
                char_cfg = ctx.characters.get(char_name)
                if not char_cfg:
                    continue
                body_desc = components.derive_body_descriptor(char_cfg)

                def _format_color_line(tag, body_desc, color_str, char_name="", appearance=""):
                    """Format a color-coded character line."""
                    name_suffix = f" {char_name}" if char_name else ""
                    if color_str:
                        parts = color_str.split(" ", 1)
                        hex_code = parts[0]
                        color_name = parts[1] if len(parts) > 1 else parts[0]
                        color_label = f"**{color_name} ({hex_code})**"
                    else:
                        color_label = ""
                    if color_label:
                        return f"- {tag}{name_suffix} — {color_label} featureless identity proxy."
                    return f"- {tag}{name_suffix} — featureless identity proxy."

                def _extract_color_name(color_str):
                    if not color_str:
                        return ""
                    parts = color_str.split(" ", 1)
                    return parts[1] if len(parts) > 1 else parts[0]

                def _identity_body_desc(suffix):
                    """获取身份级 body_desc（优先使用身份级 body_type）。"""
                    override = char_cfg.identity_body_types.get(suffix, "")
                    if override:
                        gender = char_cfg.gender.strip()
                        if gender in ("女", "女性", "female"):
                            return f"FEMALE, {override}"
                        elif gender in ("男", "男性", "male"):
                            return f"MALE, {override}"
                        return override
                    return body_desc

                active_identity_ids = set(panel_chars.get(char_name) or set())

                def _active_suffixes() -> list[str]:
                    suffixes: list[str] = []
                    if not char_cfg.identity_appearances:
                        return suffixes
                    for suffix in char_cfg.identity_appearances:
                        identity_id = f"{char_name}_{suffix}"
                        if not active_identity_ids or identity_id in active_identity_ids:
                            suffixes.append(suffix)
                    return suffixes or list(char_cfg.identity_appearances.keys())

                if len(char_cfg.identity_appearances) > 1:
                    for suffix in _active_suffixes():
                        details = char_cfg.identity_appearances.get(suffix, "")
                        identity_id = f"{char_name}_{suffix}"
                        tag = _compute_tag(char_name, identity_id=identity_id)
                        color = char_cfg.identity_sketch_colors.get(suffix, char_cfg.sketch_color)
                        char_lines.append(
                            _format_color_line(
                                tag, _identity_body_desc(suffix), color, appearance=details
                            )
                        )
                        char_names_for_color_law.append(tag)
                        tag_color_map[tag] = _extract_color_name(color)
                        identity_tag_map[identity_id] = tag
                elif char_cfg.identity_appearances:
                    suffix = _active_suffixes()[0]
                    details = char_cfg.identity_appearances[suffix]
                    identity_id = f"{char_name}_{suffix}"
                    tag = _compute_tag(char_name, identity_id=identity_id)
                    color = char_cfg.identity_sketch_colors.get(suffix, char_cfg.sketch_color)
                    char_lines.append(
                        _format_color_line(
                            tag, _identity_body_desc(suffix), color, appearance=details
                        )
                    )
                    char_names_for_color_law.append(tag)
                    tag_color_map[tag] = _extract_color_name(color)
                    identity_tag_map[identity_id] = tag
                else:
                    tag = _compute_tag(char_name)
                    color = char_cfg.sketch_color
                    char_lines.append(
                        _format_color_line(
                            tag, body_desc, color, appearance=char_cfg.appearance_details
                        )
                    )
                    char_names_for_color_law.append(tag)
                    tag_color_map[tag] = _extract_color_name(color)
                    identity_tag_map[char_name] = tag

        for prop_id in components._collect_prop_marker_ids(actual_beats_for_chars):
            prop_tag = components.compute_prop_tag(prop_id)
            marker_color = str(
                ctx.prop_marker_colors.get(prop_id, "")
                or director_frame_prop_colors.get(prop_id, "")
                or ""
            ).strip()
            if marker_color:
                parts = marker_color.split(" ", 1)
                hex_code = parts[0]
                color_name = parts[1] if len(parts) > 1 else parts[0]
                color_label = f"**{color_name} ({hex_code})**" if hex_code else color_name
                scoped_panels = prop_tag_panel_map.get(prop_tag, [])
                scoped_panel_text = ", ".join(str(p) for p in scoped_panels) or "tagged panels only"
                prop_label_map[prop_tag] = f"{prop_id} {prop_tag} {marker_color}"
                prop_tag_color_map[prop_tag] = marker_color
                prop_color_lines.append(
                    f"- {prop_tag} — {color_label} global prop marker for \"{prop_id}\". "
                    f"PANEL SCOPE: color this prop ONLY in Panel(s): {scoped_panel_text}. "
                    "EXACT COLOR LOCK: every visible part of this global prop marker MUST "
                    f"use {marker_color} only. Do not use any real material color. "
                    "Only the exact tagged prop instance gets this color. In the same panel "
                    "or any other panel, visually similar objects such as tissue boxes, "
                    "cardboard boxes, packages, crates, or box-like furniture are NOT this "
                    "global prop unless their own object is explicitly marked with this tag; "
                    "draw them as black/gray line art only."
                )
                prop_lines.append(
                    f"- {prop_tag} — GLOBAL TABLE PROP \"{prop_id}\". It MUST be entirely "
                    f"{marker_color} ONLY in Panel(s): {scoped_panel_text}. Every visible part of "
                    "this global prop marker must use this exact marker "
                    "color only. Do not use any other hue, material tint, texture, shading, "
                    "or real-object surface color. Draw it as a flat solid "
                    "simple prop marker with ZERO internal detail. Like a mannequin stands "
                    "in for a person, this colored shape stands in for the prop. Any visually "
                    "similar untagged object in the same panel or outside those panel(s) remains "
                    "ordinary black/gray line art."
                )
            else:
                prop_label_map[prop_tag] = f"{prop_id} {prop_tag}"
                local_prop_lines.append(
                    f"- {prop_tag} — LOCAL / EPISODE PROP \"{prop_id}\". No color fill. "
                    "Draw only as black/gray line art if visible."
                )

        if has_director_scene_ref_inputs:
            seen_staging: set[tuple[str, str]] = set()
            for beat in actual_beats_for_chars:
                for raw_item in beat.get("director_staging_items") or []:
                    if not isinstance(raw_item, dict):
                        continue
                    label = str(raw_item.get("label") or "").strip()
                    if not label:
                        continue
                    marker_color = str(raw_item.get("marker_color") or "").strip()
                    key = (label, marker_color)
                    if key in seen_staging:
                        continue
                    seen_staging.add(key)
                    if marker_color:
                        director_staging_lines.append(f"- marker={marker_color} -> draw user object: {label}")
                    else:
                        director_staging_lines.append(f"- draw user object: {label}")

        # COLOR LAW 区块（点名角色/道具）
        colored_targets = [*char_names_for_color_law, *prop_tag_color_map.keys()]
        prop_block = ""
        if local_prop_lines:
            sections = []
            sections.append(
                "LOCAL / EPISODE PROPS (never color-coded in sketch):\n"
                + "\n".join(local_prop_lines)
            )
            prop_block = f"""
{chr(10).join(sections)}
"""
        staging_block = ""
        if director_staging_lines:
            staging_block = f"""
DIRECTOR STAGING OBJECTS (draw the user's listed object; marker color is locator only, never output color):
{chr(10).join(director_staging_lines)}
"""
        if colored_targets:
            color_law = f"""⚠️ COLOR LAW (NON-NEGOTIABLE):
These {len(colored_targets)} named characters / panel-scoped global props have assigned color fills: {', '.join(colored_targets)}
Character color applies regardless of pose — sitting, crouching, lying on floor, being held down = STILL colored.
Global prop color applies ONLY to the exact tagged prop instance in the panel(s) listed for that prop. Do NOT propagate a global prop color to similar-looking untagged objects in the same panel or any other panel.
Unnamed people and extras must be gray directional mannequins only, NO color fill.
Do not add arbitrary new color fills to furniture/background/staging. Only named characters and listed-panel global props keep marker colors.
Only GLOBAL TABLE PROPS can keep prop marker color. Local / episode props are black/gray line art only, even when written with [[prop]] markers.
For global props, the assigned marker color wins over the object's real-world material color inside its listed panel(s). Use the listed marker color only; do not render the object's normal material color.
DIRECTOR STAGING OBJECTS are NOT global props and MUST NOT keep marker color; draw staging as black/gray line art only.

COLOR-CODED CHARACTERS:
{chr(10).join(char_lines)}
{("COLOR-CODED GLOBAL PROPS:" + chr(10) + chr(10).join(prop_color_lines)) if prop_color_lines else ""}
"""
        else:
            color_law = ""

        has_scene_refs = bool(scene_image_refs)
        has_director_scene_refs = has_director_scene_ref_inputs
        has_director_blocking_refs = any(
            PromptComponents._is_director_image_ref(ref) for ref in scene_image_refs
        )
        # Space Map / scene_spatial_layout references are legacy/advisory for
        # storyboard now. Keep them in the generic scene-ref path rather than
        # switching to the old hard-locked map prompt.
        scene_geometry_block = ""
        if has_scene_refs:
            if has_director_scene_refs and has_director_blocking_refs:
                scene_geometry_block = """
SCENE GEOMETRY REFERENCE:
The attached scene reference is a 3GS DIRECTOR CONTROL FRAME, not a final sketch and
not a loose style reference. It contains the chosen camera view, rough background,
visible actor/mannequin placeholders, prop markers, and staging placeholders.

DIRECTOR CONTROL FRAME LOCK:
- Use the 3GS DIRECTOR CONTROL FRAME as a spatial/camera control input, not as pixels
  to keep. Translate it into the normal production sketch style.
- Preserve the same camera intent, crop, FOV, horizon, lens distance, object screen
  positions, actor screen regions, table edges, stool positions, window/fan/wall
  relation, counter side, condiment/object placement, and local furniture topology.
- Furniture contact is part of the blocking: tables, counters, stools, chairs,
  benches, beds, desks, and similar set pieces are solid support/occlusion objects.
  A seated mannequin must sit ON a visible or minimally implied seat and stay
  beside/behind the table edge, never inside the tabletop/counter/bench/table
  volume. Legs may go under a table, but hips/torso/head must remain outside the
  furniture body with a readable table-edge occlusion relationship.
- If the exported 3GS mannequin/marker intersects furniture because of projection,
  depth sorting, or marker scale, treat that as a control-frame artifact. Repair it
  with the smallest physically plausible adjustment inside the same actor screen
  region; do not move the actor to a different seat, table, side of the room, or
  new camera setup.
- Do NOT preserve projection errors literally. If the 3GS/360 capture creates fisheye
  bending, extreme wide-angle stretching, curved walls, bowed counters, warped floors,
  bent verticals, broken seam cuts, or discontinuous wall/floor surfaces, repair them
  into one coherent storyboard perspective with straight architectural construction.
  This correction must keep the same screen regions and staging; it is not permission
  to change camera, crop, scale, object order, or furniture placement.
- Door/window/opening topology is locked, but surface condition is NOT. If the source
  shows a walkable doorway, door panel, side jamb, threshold, window, or partition,
  keep the same passable-vs-blocking relationship, open/closed state, side angle,
  depth cue, and screen region. Do NOT turn an oblique doorway into a generic
  front-facing double door, do NOT fill an opening as a wall/cabinet, and do NOT
  invent a cleaner symmetrical door system. Do NOT copy dirt, damage, decay,
  material texture, reflections, or surface wear into the sketch; those belong to
  the later render/color stage.
- Redraw the environment as simplified storyboard line art / light gray construction
  lines. Do NOT keep the blurred 3GS rendering, texture, shading, lighting, noise, or
  photographic/game-render look.
- The visible 3GS actor/mannequin is only an actor placement placeholder. Replace it
  in the same approximate screen region with a color-coded directional mannequin. Its original
  pose is only a hint; the final mannequin pose, facing direction, held-object relation, and
  action must come from the panel visual_description.
- Visible colored prop/staging blocks are source placeholders. If the action uses a named prop with that marker color, transform that visible marker into the action prop; do NOT leave the original marker and create a duplicate marker elsewhere.
- Unrelated staging markers are production placeholders. Keep them visible as flat
  black/gray rough silhouettes in the same screen position; do not move, delete,
  color-fill, materialize, or beautify them.
- Ignore any older camera plan, exit-path plan, or door-framing plan. The attached image is the current human-approved camera.
- Do NOT push in, pull out, rotate, pan, reframe, or choose a cleaner alternate camera.
- Do NOT reconstruct the scene from imagination. Use the attached blocking frame only
  to decide the sketch camera and object placement.

OVERLAY OUTPUT CONTRACT:
- Replace every blocky 3GS actor/mannequin with simple directional storyboard mannequins that perform the action described in the panel's visual_description.
- Characters named by {{identity}} markers receive their assigned flat marker color.
- Unnamed background people / customers / crowd figures become gray directional mannequins only.
- If the visual_description says a character holds/carries/lifts a [[prop]], the scene sketch MUST show the mannequin doing that action around the visible actor placeholder: arms/hands wrapped around the prop at the hands, just like the free sketch workflow. If a visible GLOBAL prop marker block exists in the input for this same listed panel, reuse/transform that marker into the held prop; do not duplicate it. If it is a global prop with an assigned marker color in this panel, fill the held prop itself with that assigned prop color, not realistic brown/cardboard/material color. Local/episode props and similar untagged objects are black/gray line art only. Do not omit the held object relationship.
- Draw global props as simple storyboard prop silhouettes only when they are tagged/listed as part of the action for that panel; only the exact tagged prop instance keeps its assigned marker color. Local/episode props and similar untagged objects in the same panel or any other panel are black/gray line art only. Never create a second copy of an already-visible prop marker unless the story explicitly says there are two.
- Do not turn colored directional mannequins into realistic people during sketch generation.
- Do not turn colored global prop markers into final realistic objects during sketch generation.
- Do translate staging markers into black/gray rough line-art silhouettes by semantic label.
- The later render stage will replace mannequins and prop markers with real identities/materials.

ALLOWED CHANGES:
- Convert the original 3GS scene plate into clean production sketch line art.
- Replace blocky 3GS mannequins with simple colored directional mannequins in the same screen regions.
- Add only the beat action details around the existing colored mannequins, especially facing direction, arm pose, feet direction, and held props.
- Keep all characters as simple color-coded directional mannequins over the simplified line-art
  version of the chosen background.

FORBIDDEN CHANGES:
- No new camera angle.
- No blank white-paper redraw that loses the chosen 3GS camera/background topology.
- No generic line-art redraw that ignores the chosen 3GS camera/background topology.
- No realistic repaint, no new background painting from scratch, and no keeping the
  original blurred 3GS pixels as the visible background.
- No background repair pass unless a pixel artifact makes the actor/prop relation unreadable.
- No final cinematic lighting, depth of field, motion blur, material polish, or full environment beautification.
- No new furniture cluster.
- No changed table shape or table position.
- No different wall/window/fan arrangement.
- No extra named characters.
- Any signage, menus, posters, labels, clock faces, screens, and wall notices must stay unreadable.

This is a staging/marker scene sketch over an existing 3GS blocking base, not a final render or new scene generation task.
""".strip()
            elif has_director_scene_refs:
                scene_geometry_block = """
SCENE GEOMETRY REFERENCE:
The attached scene reference is a 3GS/director background sketch reference.
It may be a single beat-specific anchor or a director reference sheet aligned to this output grid.
It is a normal scene sketch reference with an existing background, not a separate camera plan.

GEOMETRY (STRICTLY INHERITED):
- The visible walls, ceiling, floor, windows, doors, counters, road edges, poles, seats, and major fixed fixtures must stay consistent with this environment anchor.
- You MAY NOT invent new walls, new windows, new architectural zones, new fixtures, or rearrange the existing layout.
- Preserve the same local left/right ordering of fixed fixtures that is visible in the environment anchor.
- Preserve the table/chair/stool orientation from the matching sheet cell. Do NOT rotate or rebuild a table cluster into a new diagonal foreground composition unless the contract explicitly asks for it.

LOCAL FURNITURE LOCK (CRITICAL):
- The visible furniture cluster in the director reference is the actual action zone for this beat. Do NOT treat it as a loose background texture.
- Preserve the exact relative order, scale, and partial visibility of local furniture: long table vs square table, table edge vs full tabletop, stool positions, window/fan/wall relation, counter side, and condiment/object placement.
- If the reference shows only a partial table edge or one corner of a square table, keep it partial in the same side of the frame. Do NOT move it to the center, enlarge it into a new foreground table, or redraw it as a different full dining setup.
- If a required seated/over-shoulder character needs support geometry that is not fully visible, attach the character to the existing visible table/seat edge with a minimal implied stool or shoulder cue. Do NOT spawn a second foreground table, bench, booth, platform, or unrelated furniture cluster.
- Foreground shoulders and bodies may occlude the inherited geometry, but they must not replace it with a new table layout.
- If you cannot confidently infer hidden furniture from the anchor, omit the hidden furniture rather than inventing a cleaner generic restaurant table.

COMPOSITION (CONTROLLED):
- Character placement, pose, and crop should come from the panel visual_description while respecting the visible background.
- For close-up or reaction beats, keep only the nearest support geometry that would naturally stay in frame after moving closer within the same anchored space.
- Do NOT expand a close-up into a fuller room layout or add extra local furniture just to make the frame feel filled.
- Place the directional mannequin characters on top of this inherited geometry.
- Scale mannequins to the furniture and shot scale. For medium and wide shots, no colored head should dominate the panel unless the contract explicitly says close-up.

HARD CONSTRAINTS:
- Do NOT average this anchor with an alternative scene layout.
- Do NOT silently rotate to a different scene side.
- Do NOT duplicate major fixtures on both sides of the room/street/carriage.
- Any signage, menus, posters, labels, clock faces, screens, and wall notices must stay unreadable. Replace them with abstract shapes or illegible marks only.
- Panels explicitly marked as BLANK PLACEHOLDER are exempt from scene geometry inheritance and should remain empty placeholders.

This environment anchor defines the visible background. The panel visual_description defines the action.
Keep scene lines minimal and clean — this is still a storyboard sketch, not a rendered scene.
""".strip()
            elif ctx.mode == PromptMode.SKETCH:
                # Sketch 模式：scene 参考只作为 STYLE ANCHOR（线条/材质/光照/调色风格），
                # 不锁几何也不锁机位。每个 panel 的相机和构图完全由 visual_description 驱动。
                scene_geometry_block = """
SCENE STYLE ANCHOR (NOT a geometry constraint):
The attached scene reference image is a STYLE ANCHOR for sketch generation, NOT a camera or layout constraint.

USE THE SCENE REFERENCE FOR:
- Linework density, brush feel, and overall hand-drawn finish
- Material language and surface texture vocabulary (wood / tile / metal / concrete etc.)
- General scene identity (so the panel reads as "this location" rather than a generic interior)

DO NOT USE THE SCENE REFERENCE FOR:
- Camera angle, framing, or shot size — these come from each panel's visual_description and your director's judgement
- Specific furniture count or exact wall positions — sketches don't need pixel-level geometry, they need readable blocking
- Forcing every panel to mirror the reference's FRONT-FACING establishing view

DIRECTING FREEDOM:
- You are the storyboard director: choose the best shot size (wide / medium / close-up / OTS / insert / reaction) and camera angle (eye-level / high / low / three-quarter / side / over-shoulder / first-person) for each panel based on the beat's emotional and narrative needs.
- Vary shot sizes and camera angles aggressively across the 16 panels to build cinematic rhythm. Avoid using the same medium eye-level shot for multiple consecutive panels.
- Each panel should feel like a different camera setup, not the same camera mirrored.

LIGHT GEOMETRY HYGIENE (soft, not enforced):
- Stay roughly inside the same location identity across consecutive panels in the same scene; do not switch venues mid-conversation.
- Major fixed fixtures (counter side / entry direction / window wall) should stay consistent across closely-related panels so shot/reverse-shot reads coherently. Minor furniture count may shift between shots.
- Any signage, menus, posters, labels, clock faces, screens, and wall notices must stay unreadable. Replace them with abstract shapes or illegible marks only.
- Panels explicitly marked as BLANK PLACEHOLDER remain empty placeholders.
""".strip()
            else:
                scene_geometry_block = """
SCENE GEOMETRY REFERENCE:
The attached scene reference assets describe the SAME environment.
In the 2.0 scene asset pipeline they are normally:
- MASTER / FRONT: real scene look, materials, lighting, and the primary view
- TOP_DOWN / FLOOR PLAN: room layout and object placement from above
- REVERSE / BACK: missing rear-side fixtures and surfaces

GEOMETRY (STRICTLY INHERITED):
- The scene's walls, ceiling, floor, windows, doors, counters, road edges, poles, seats, and major fixed fixtures come from the scene reference assets.
- You MAY NOT invent new walls, new windows, new architectural zones, new fixtures, or rearrange the existing layout.
- For each storyboard panel, use MASTER/FRONT for look and visible details, TOP_DOWN for spatial placement, and REVERSE/BACK only when the shot faces the rear side or needs missing back-wall evidence.
- If only a subset of assets is provided, use the available assets as constraints and keep uncertain hidden areas minimal.

COMPOSITION (STILL FREE):
- You MAY choose shot size freely (wide / medium / close-up) based on the beat.
- You MAY choose camera angle within the scene (eye-level / high / low / over-shoulder / three-quarter / side-view) as long as the visible geometry remains consistent with the chosen PRIMARY region.
- If no region matches exactly, compose a new shot that is geometrically consistent with the nearest region, while using the other regions only to verify continuity of fixed fixtures and surface layout.
- For each panel, first identify the beat's LOCAL ACTION ZONE inside the inherited scene geometry: for example one table pair, one booth, one counter segment, one doorway zone, or one seat row.
- Adapt environment density to shot scale:
  close-up / reaction / insert -> keep the SAME local action zone, but only draw the nearest support geometry that would naturally remain in frame
  medium shot -> keep the local action zone readable without expanding to the whole room
  wide / establishing / walking shot -> preserve the broader room or street layout
- The environment geometry is fixed. Prefer CAMERA REPOSITIONING over restaging the room. When turning a wide shot into a medium or close-up, move closer to the existing local action zone instead of teleporting the subject to a different table, seat, doorway, or foreground platform.
- If a beat is a tighter shot of someone already seated or interacting at a table/seat zone, preserve that SAME local furniture zone and crop closer into it rather than inventing a second foreground table or bench.
- When a beat describes dining, conversation at a table, or seated interaction, preserve the same local table/seat relationship implied by the scene. Do NOT turn a background table into a different front-table setup.
- Keep the fixed furniture inventory of the visible local zone stable across shots. If the chosen zone has one table, one bench pair, or one counter segment, do NOT silently turn it into two tables, a new bench, or a different furniture cluster.
- Place the stick-figure characters on top of this inherited geometry.

HARD CONSTRAINTS:
- Do NOT duplicate the same major fixture on both sides of the room/street/carriage.
- Do NOT average all reference regions together into a vague blended layout.
- Do NOT invent a third architectural layout different from the provided reference regions.
- Do NOT invent a new foreground table, bench, counter, doorway, or wall segment just to support a close-up.
- Do NOT merely restage the action into a different furniture zone when a tighter camera move within the same zone would satisfy the beat.
- Do NOT swap the subject from one local action zone to another unless the beat explicitly describes that movement.
- Do NOT add, remove, duplicate, or replace fixed furniture units inside the chosen local action zone unless the beat explicitly shows that physical change.
- If the beat is built around one table pair, one booth, or one seat pair, preserve that SAME pair and SAME seating orientation when moving closer. Do NOT restage it as a different generic front-table setup.
- Keep MASTER / TOP_DOWN / REVERSE as complementary evidence for one fixed space, not as alternative redesigns.
- In highly symmetrical spaces such as train cars, hallways, and corridors, keep doors, windows, seats, poles, signage blocks, and ceiling fixtures consistent with the chosen camera direction, using other references only to confirm continuity.
- Any signage, menus, posters, labels, clock faces, screens, and wall notices must stay unreadable. Replace them with abstract shapes or illegible marks only.
- Panels explicitly marked as BLANK PLACEHOLDER are exempt from scene geometry inheritance and should remain empty placeholders.

The scene reference defines WHERE the environment is. Your job is to add WHO is there and WHAT they do.
Keep scene lines minimal and clean — this is still a storyboard sketch, not a rendered scene.
""".strip()

        rough_gpt_sketch = _uses_gpt_image_sketch_profile(ctx) and not (
            has_director_scene_refs and has_director_blocking_refs
        )
        if has_director_scene_refs and has_director_blocking_refs:
            style_block = """STYLE: **DIRECTOR CONTROL TO PRODUCTION STORYBOARD SKETCH**.
Convert the attached 3GS director control frame into a director-control production sketch: simplified background line art + color-coded actor proxies with readable facing cues + flat listed-panel global prop markers. The 3GS frame locks camera and placement only; it must not remain as a blurred rendered background. This is NOT final rendering."""
            role_line = "ROLE: You are a pragmatic production storyboard cleanup artist."
            task_line = f"TASK: Translate the attached 3GS director control frame into a {rows}x{cols} production storyboard sketch ({total_panels} panel). Preserve the approved camera intent, crop, composition, object placement, and actor/prop screen regions, but correct 3GS/360 projection distortion and redraw the background as simplified line art while replacing/posing visible actors and named action props according to visual_description."
            panel_rule_block = """DIRECTOR CONTROL TRANSLATION RULE:
- The first 3GS director frame is a control input, not a pixel base. Preserve camera intent, crop, room side, furniture screen positions, and prop/staging marker positions.
- Keep character/furniture contact physically readable in the sketch: seated mannequins sit on stools/chairs/benches beside the table edge, not inside the table/counter volume. If the control marker crosses a tabletop or counter, interpret the crossing as a rough placement artifact and make the smallest local correction while keeping the same screen region and action.
- Repair projection artifacts during translation: straighten bowed walls, counters, door frames, table edges, floor seams, shelves, screens, and vertical fixtures; merge broken 360 seam fragments into one readable surface; normalize impossible fisheye/wide-angle bending into a coherent storyboard perspective.
- Projection cleanup must not move actors, props, furniture, or the camera. Keep their screen regions, ordering, and scale stable.
- Door/opening rule: preserve topology, not material finish. A visible doorway remains a doorway, a blocking door panel remains a blocking panel, an open passage stays open, a closed barrier stays closed, and an angled side doorway stays angled in the same screen region. Do not replace it with a generic centered double-door icon. Do not copy dirt, decay, glass reflections, surface texture, or damaged detail into the sketch.
- Redraw the scene into the normal sketch vocabulary: clean black/gray environment lines on light paper, no 3GS blur, no rendered texture, no cinematic lighting.
- For each panel, change only what the visual_description requires: actor mannequin pose/facing/action and held named prop relation.
- Translate DIRECTOR STAGING OBJECTS from the listed user object label and visual_description: if the label says horse, draw a horse-like rough storyboard silhouette in the same screen position, not an anonymous box.
- STAGING COLOR BAN: if a DIRECTOR STAGING OBJECT lists marker=#RRGGBB, use that marker color ONLY to find the colored control shape in the Director frame. The output staging object MUST be black/gray line art only. It must NOT have colored fill, colored outline, colored tint, or any marker-colored pixels."""
            rendering_preface = """- OUTPUT MUST LOOK LIKE A NORMAL PRODUCTION SKETCH, not a 3GS screenshot with markers.
- Redraw the chosen 3GS background as simplified line art / light gray construction lines while preserving its camera and topology.
- Attached scene master/detail references are only for material/fixture recognition; they must not override the 3GS control frame composition.
- Do not add final cinematic lighting, depth of field, motion blur, realistic materials, or final environment polish.
- Do not preserve the original 3GS blur, texture, shading, noisy pixels, or game-render look.
"""
            environment_rules = """- Existing environment/furniture/staging stays in the same screen position but becomes simplified line art.
- Staging objects with semantic labels must remain recognizable as that object class in sketch form (horse, vehicle, sedan chair, pile of boxes, etc.).
- Staging marker colors are input locators only. All staging output must be black/gray line art only, with no marker color preserved.
- Global prop markers remain flat marker-color shapes only in their listed panel(s) when they are part of the action.
- Only named actors and listed-panel global props can be colored. Unnamed background people, local/episode props, and all staging objects are black/gray only."""
        else:
            style_block = """STYLE: **COLOR-CODED DIRECTIONAL STORYBOARD MANNEQUIN** on pure white background.
Speed and clarity over artistic quality. Focus on CHARACTER PLACEMENT, POSE, and CAMERA ANGLE.
SYMBOLIC STORYBOARD PEOPLE ONLY: all humans are featureless identity proxies, not character designs. Use only oval head, one body-axis/spine line, single-stroke arms/legs, short shoulder/hip direction ticks, tiny facing tick, and tiny ground-contact direction ticks. These ticks show facing direction only; they are not shoes or feet details. NO clothing of any kind, no hair, no facial features, no skin, no gendered body shape, no realistic anatomy.
Global props are flat solid color markers/silhouettes only in their listed panel(s) (real material rendered later in the render stage). Local/episode props are black/gray line art only.
Backgrounds are minimal contextual black/gray line art based on attached scene references when present; characters and listed-panel global props are the visual priority."""
            role_line = "ROLE: You are a MASTER FILM DIRECTOR and storyboard artist."
            task_line = f"TASK: Create a {rows}x{cols} storyboard grid ({total_panels} panels) for a dramatic short film sequence. Read each panel's scene description and make the written action visually readable. Choose angle/framing only where the description leaves room; do not sacrifice blocking clarity for cinematic variety."
            panel_rule_block = """SINGLE-MOMENT RULE:
- Each panel must depict exactly ONE camera setup and ONE frozen story moment.
- Do NOT combine multiple sub-shots, multiple time slices, or multiple sequential actions inside one panel.
- Do NOT create split-screen, collage, comic-strip subdivisions, montage inserts, before/after composites, or flashback overlays inside a panel.
- If a panel description mentions a short sequence, memory, or several actions in a row, collapse it into the single dominant visual moment instead of showing all of them at once."""
            rendering_preface = """- WHITE PAPER BACKGROUND ONLY. No cinematic grayscale rendering, no dark fills, no gradients, no shadows, no lighting effects, no material shading.
- If scene master/reverse references are attached, draw a simple black/gray background line-art version of that scene; do NOT leave the background blank.
- The scene reference controls scene identity and fixed-space cues only. Redraw it as clean black/gray outline art; do NOT copy its darkness, blur, texture, color, material finish, or lighting mood.
- If the beat is dark/night/interior dim, suggest darkness with sparse line density or simple hatching only; do NOT fill the panel with black/gray shadow.
"""
            environment_rules = """- Unnamed props/environment → black line art, no fill
- Background → simplified architectural black/gray lines based on attached master/reverse scene references when provided; omit decorative detail."""
        if rough_gpt_sketch:
            style_block = """STYLE: in the style of a **rushed film director's storyboard scribble**, **rough hand-drawn sketch on cheap white paper**, **completely uninterested in artistic finish**, loose pencil/marker doodle scribbled in 30 seconds, deliberately unpolished, raw thumbnail-grade draft.
- Imperfect strokes; uneven line weight; visible "thinking on paper" feel
- This is a DRAFT / THUMBNAIL / BLOCKING SKETCH — NOT a finished illustration, NOT digital art, NOT vector graphics, NOT a children's book illustration, NOT clean line art
- Named characters get a single flat color fill in their assigned marker color. Global props get marker color fill ONLY in the panel(s) listed for that prop; local/episode props, visually similar untagged objects, and every DIRECTOR STAGING OBJECT are loose black/gray pencil/marker line work on pure white paper
- Characters are COLOR-CODED DIRECTIONAL MANNEQUINS; listed-panel global props are COLOR-CODED simple silhouettes filled with their marker color; local/episode props are black/gray line art only
- Speed and clarity over artistic quality; treat this as a 30-second blocking sketch, not a final piece"""
            role_line = "ROLE: You are a MASTER FILM DIRECTOR and storyboard artist."
            task_line = f"TASK: Create a {rows}x{cols} storyboard grid ({total_panels} panels) for a dramatic short film sequence. Read each panel's scene description and make the written action visually readable. Choose angle/framing only where the description leaves room; do not sacrifice blocking clarity for cinematic variety."
            panel_rule_block = """SINGLE-MOMENT RULE:
- Each panel must depict exactly ONE camera setup and ONE frozen story moment.
- Do NOT combine multiple sub-shots, multiple time slices, or multiple sequential actions inside one panel.
- Do NOT create split-screen, collage, comic-strip subdivisions, montage inserts, before/after composites, or flashback overlays inside a panel.
- If a panel description mentions a short sequence, memory, or several actions in a row, collapse it into the single dominant visual moment instead of showing all of them at once."""
            rendering_preface = """- Line work is loose and sketchy: imperfect strokes, slightly wobbly lines, occasional double-stroke or messy ends — like a fast pencil/marker draft, NOT clean vector lines
- Do NOT clean up, polish, smooth, or vectorize the lines; rough is correct
- Backgrounds must stay ultra-simplified: only a few major shapes and blocking lines, never a fully rendered scene
- Suggest the location with 3-8 essential strokes/shapes only; leave generous white space instead of filling the frame with detail
- NO texture rendering, NO debris scatter, NO surface patterning, NO dense perspective construction, NO cinematic atmosphere rendering
"""
            environment_rules = """- Unnamed props/environment → sparse, thin, light-gray line art only, no fill (this rule does NOT apply to global props in their explicitly listed panel(s) — those keep their assigned marker color fill)
- Background canvas → pure white
- Keep backgrounds extremely simple: only the minimum lines needed to show location, depth, and blocking
- Background details must never compete with colored characters/listed-panel colored global props or make the panel feel busy"""

        action_pose_block = """ACTION BLOCKING RULE:
- Treat the panel visual_description as the action source of truth.
- If the panel description contains physical action verbs (walk, leave, enter, turn, stand up, sit down, lift, carry, push, pull, open, cross, step, run, fall, reach; or Chinese equivalents like 走出/离开/进入/转身/站起/坐下/抱起/搬起/推/拉/打开/跨出/跑/摔倒), draw the figure in an action pose instead of a neutral standing icon.
- If the visual_description explicitly specifies shot size, framing, close-up, upper body, camera angle, POV, empty shot, or blackout, obey that framing first. Do not add extra body parts, thresholds, or environmental cues that the written framing excludes.
- If the visual_description explicitly says a character holds/carries/lifts a named prop, show the prop contact clearly inside the written framing.
- Do not use arrows, text labels, speed lines, or comic motion effects to explain movement."""

        # ----- 主体 intro -----
        if has_director_scene_refs and has_director_blocking_refs:
            intro_action = (
                f"Edit the attached 3GS combined background in place into a {rows}x{cols} scene sketch. "
                f"Each panel MUST be {panel_hint}. Do not create a new camera view. "
                "Lock the approved composition and staging, but correct 3GS/360 projection artifacts: fisheye bending, wide-angle stretching, broken seams, warped floors, curved walls, bowed counters, skewed tables, screens, door frames, floor seams, and vertical fixtures must become straight/coherent storyboard construction. Preserve door/window/opening topology, passable-vs-blocking relationship, and open/closed/angled state; do not replace them with cleaner generic doors, and do not copy material dirt/decay/detail into the sketch."
            )
        else:
            intro_action = (
                f"Generate a {rows}x{cols} storyboard grid. Each panel MUST be {panel_hint}."
            )

        intro = f"""{intro_action}

⚠️ 100% CANVAS COVERAGE — artwork fills ENTIRE canvas edge-to-edge, NO margins/padding/borders.

!!! MANDATORY GRID FORMAT: {rows} ROWS × {cols} COLUMNS !!!
(This means {rows} horizontal rows stacked vertically, each row containing {cols} panels side by side)

{role_line}

{task_line}

{panel_rule_block}

{action_pose_block}

VISUAL DESCRIPTION AUTHORITY:
- Treat every non-empty panel description as the hard visual brief for that panel.
- If a panel description explicitly names shot type, camera angle, POV, framing, composition, empty shot, or blackout, obey that written direction exactly.
- If a panel description does NOT specify shot/camera/framing, then choose the best shot yourself using the directing guidelines below.
- Never override explicit panel wording with a generic directing rule; use directing freedom only where the panel description leaves room.

{style_block}

{color_law}{prop_block}{staging_block}RENDERING RULES:
{rendering_preface}- Named characters → very simple COLOR-CODED DIRECTIONAL MANNEQUINS / featureless identity proxies with marker color fill/outline. Marker color identifies the character; it is not final character art.
- Global props (listed in COLOR-CODED GLOBAL PROPS) → ONLY in the listed panel(s), REPLACE the exact tagged prop instance with a flat solid marker shape entirely filled in its exact assigned marker color, with ZERO internal detail (no edges, no flaps, no logos, no shading). The marker color is mandatory and overrides real material color only for that tagged prop instance in those panel(s). Similar boxes/tissue boxes/packages in the same panel or any other panel are NOT color-coded unless they are explicitly tagged as this global prop; draw them as black/gray line art only.
- Unnamed people → gray directional mannequins only, no fill
{environment_rules}
- Anchor scale lock: identity proxies must fit the nearby support surface / seat / fixture scale. Do NOT draw giant foreground heads or oversized identity markers in any panel.
- Close-up rule: for close-ups, show only the necessary head oval, facing tick, shoulder direction line, and nearby support-surface / wall / fixture cues. Do NOT crop into a giant colored head or poster-like portrait.
- Keep identity proxies thin, simple, and map-like. Color marks identify characters; proxy shape only identifies blocking/facing direction.
- Focus on POSE, POSITION, FACING DIRECTION, and prop contact only, not anatomy.
- Directional mannequin allowed elements: round/oval head; optional tiny 5-15px nose/facing tick with NO facial features; one body-axis/spine line; short shoulder line and hip line to show body facing; one spine center line ONLY for back-to-camera; tiny ground-contact direction ticks to show front/back direction (not shoes or feet details); single-stroke arms and legs.
- Shot-size adaptation: wide/full shots may show full proxy with head direction, shoulder/hip line, and ground-contact direction ticks; medium shots use head direction, body-axis line, and shoulder line; close-ups/extreme close-ups use only head/shoulder orientation and omit lower body unless the visual_description explicitly asks for it; back-to-camera figures use head oval + spine line + shoulder line.
- Directional mannequin forbidden elements: facial features (eyes, mouth, detailed nose), hair, clothing/costume/outfit of any kind, clothing patterns/materials/folds, collars, sleeves, belts, shoes, accessories, fingers/toes, muscles, body volume, realistic anatomy, shading, gradients, lighting effects.
- NO facial features, NO hair detail, NO hands or fingers, NO shoes, NO feet detail beyond tiny ground-contact direction ticks
- NO clothing of any kind, NO clothing folds, NO costume ornament, NO fabric detail, NO body volume
- NO muscles, NO realistic anatomy, NO shading, NO semi-realistic human forms
- Keep all human figures as minimal directional mannequins only
- NO comic/manga effect lines: no motion lines, speed lines, trembling lines, impact strokes, emphasis rays, or symbolic action marks
- Do not turn an ordinary panel into a poster-like summary collage or a panel-within-panel layout unless the description explicitly demands that visual device

LAYOUT (CRITICAL - MUST BE EXACT):
- EXACTLY {rows} rows × {cols} columns = {total_panels} panels total
- Each row MUST have EXACTLY {cols} panels (no more, no less)
- Each column MUST have EXACTLY {rows} panels (no more, no less)
- ⚠️ Each individual panel MUST be {_panel_ar_hint(ctx.grid.aspect_ratio, rows, cols)}
- Panels numbered left-to-right, top-to-bottom (1, 2, 3... {total_panels})
- ⚠️ ONE CONTINUOUS IMAGE. NO drawn borders, gutters, or dividing lines between regions
- Adjacent regions from different scenes may have natural background transitions
- All {total_panels} panels MUST be EQUAL SIZE and ALIGNED in a perfect grid

⚠️⚠️⚠️ ABSOLUTELY NO TEXT ON IMAGE ⚠️⚠️⚠️
- DO NOT render ANY text, labels, numbers, or captions on the image
- DO NOT add panel numbers (1, 2, 3...) visually on the image
- Scene signage, menus, posters, clocks, screens, and wall notices must use abstract or illegible marks only
- The final image must contain ONLY artwork, ZERO text

{ascii_layout}
"""

        # Panel 描述
        lines = []
        actual_beats = ctx.beats[:total_panels]

        for i, beat in enumerate(actual_beats, start=1):
            visual_desc = beat.get("visual_description", "")
            visual_desc = _resolve_prop_marker_tags(visual_desc)

            # 草图路径：剥离颜色词（颜色由标记系统控制，避免污染调色盘）
            from novelvideo.utils.text_utils import strip_color_words

            visual_desc = strip_color_words(visual_desc)

            # 替换 {{}} 标记为 identity_id（兼容 {{identity_id}} 和 {{角色名}}）
            from novelvideo.utils.identity_resolver import (
                resolve_visual_description_markers,
                build_identity_to_char_map,
            )

            id_to_char = build_identity_to_char_map(ctx.characters)
            visual_desc = resolve_visual_description_markers(
                visual_desc, ctx.characters, id_to_char, use_identity_id=True
            )

            visual_desc = visual_desc.rstrip("。！？，、；：")

            # Sketch 模式：Nanobanana 自主构图（Master Director），不注入导演手册的镜头语言
            panel_desc = visual_desc
            # 默认 sketch 不再注入大段 BLOCKING 约束；仅在 render 模式或 3GS director 修图模式下保留几何约束
            blocking_hints = (
                []
                if ctx.mode == PromptMode.SKETCH
                or rough_gpt_sketch
                or (has_director_scene_refs and has_director_blocking_refs)
                else components.infer_sketch_blocking_hints(
                    beat,
                    has_scene_refs=has_scene_refs,
                    has_director_scene_refs=has_director_scene_refs,
                )
            )

            # 兜底扫描 ctx.characters，替换残余的裸角色名
            if ctx.characters:
                from novelvideo.models import extract_char_identities_from_markers

                char_identities = extract_char_identities_from_markers(
                    beat.get("visual_description", ""), strict=False
                )
                for char_name, char_cfg in ctx.characters.items():
                    if char_name in panel_desc:
                        safe_pattern = r"(?<![{\[])" + re.escape(char_name) + r"(?![_}\]])"
                        if char_name in char_identities:
                            identity_id = char_identities[char_name]
                            if identity_id:
                                tag = _compute_tag(char_name, identity_id=identity_id)
                                panel_desc = re.sub(safe_pattern, tag, panel_desc)
                        else:
                            panel_desc = re.sub(safe_pattern, "", panel_desc)

            # 在 tag 后注入颜色名，强化命名角色可见性
            for tag, color_name in tag_color_map.items():
                if tag in panel_desc:
                    panel_desc = panel_desc.replace(tag, f"{tag} ({color_name})")
            for tag, marker_color in prop_tag_color_map.items():
                if tag in panel_desc:
                    panel_desc = panel_desc.replace(
                        tag,
                        f"{tag} ({marker_color} ONLY; no other colors) [THIS PANEL ONLY]",
                    )
            if blocking_hints:
                panel_desc = f"{panel_desc} | BLOCKING: {' '.join(blocking_hints)}"
            lines.append(f"- **Panel {i}**: {panel_desc}")

        # 填充不足的 panels（使用明确的占位符，避免模型自作主张）
        if len(actual_beats) < total_panels:
            for i in range(len(actual_beats) + 1, total_panels + 1):
                if rough_gpt_sketch:
                    lines.append(
                        f"- **Panel {i}** [BLANK PLACEHOLDER]: A completely blank unused panel. "
                        "Pure white background only. No scenery, no characters, no symbols, no marks, no text."
                    )
                else:
                    lines.append(
                        f"- **Panel {i}** [BLANK PLACEHOLDER]: A completely blank unused panel. "
                        "Pure white background only. No scenery, no characters, no symbols, no marks, no text."
                    )

        shots = "\n".join(lines)

        if has_director_scene_refs and has_director_blocking_refs:
            compact_registry = (
                f"\n\nNEGATIVE CONSTRAINTS:\n{ctx.registry_negative_clause}"
                if ctx.registry_negative_clause
                else ""
            )
            compact_prop_block = prop_block.strip()
            compact_staging_block = staging_block.strip()
            compact_color_law = color_law.strip()
            compact_sections = [
                (
                    f"Convert the attached 3GS director control frame into a {rows}x{cols} "
                    f"production storyboard sketch. Each panel must be {panel_hint}. "
                    "Use the attached image as camera/topology/staging control only, not as pixels to keep."
                ),
                (
                    "OUTPUT STYLE: clean director-control production sketch on light paper. Redraw background as simplified "
                    "black/light-gray line art. Named characters are simple color-coded actor proxies with readable facing cues; named "
                    "props are flat solid marker shapes. No realistic rendering, no blur, no texture, no "
                    "cinematic lighting."
                ),
                (
                    f"LAYOUT: exactly {rows} rows x {cols} columns = {total_panels} panel(s). "
                    "No borders/gutters/panel numbers. No text, captions, labels, readable signage, or watermarks."
                ),
                compact_color_law,
                compact_prop_block,
                compact_staging_block,
                (
                    "CONTROL LOCK: preserve the approved camera intent, crop, horizon/lens distance, actor "
                    "screen regions, prop/staging marker positions, local furniture screen geometry, table edges, "
                    "stools/chairs, counters, doors/windows/openings, and wall/floor relationships. Do not reframe, "
                    "push in, pull out, rotate, pan, zoom, choose a cleaner camera, or invent a new furniture cluster."
                ),
                (
                    "PROJECTION CLEANUP: repair only capture artifacts from 3GS/360: fisheye bending, wide-angle "
                    "stretching, broken seams, warped floors, bowed counters/walls, tilted verticals, and skewed "
                    "door/table lines. Keep the same screen regions, topology, scale, and object order."
                ),
                (
                    "FURNITURE CONTACT: tables/counters/stools/chairs/benches/desks are solid support and occlusion "
                    "objects. A seated mannequin sits on a visible or minimally implied seat beside/behind the "
                    "table edge, never inside a tabletop/counter/bench/table volume. If a 3GS marker intersects "
                    "furniture, treat it as projection/depth error and make the smallest local correction inside "
                    "the same actor screen region."
                ),
                (
                    "ACTORS/PROPS: replace visible mannequins with simple color-coded actor proxies in the same approximate "
                    "screen regions. The final pose/facing/action comes from visual_description. You may make the smallest "
                    "local position/pose adjustment required to make that action readable; do not teleport the actor "
                    "across the room or change the camera. Reuse "
                    "visible global prop markers for held/carried/lifted props only in that prop's listed "
                    "panel(s); do not duplicate them or color visually similar untagged objects "
                    "in the same panel or other panels. Local/episode props and similar "
                    "untagged props are black/gray line art only."
                ),
                (
                    "FACING CUE RULE: keep humans minimal, but make front/back/side facing readable with "
                    "only these cues: oval head plus tiny facing tick, simple capsule/trapezoid torso for full/medium "
                    "shots, shoulder/hip lines, optional spine line only for back-to-camera, and tiny "
                    "ground-contact direction ticks that are not shoes or feet details. "
                    "No facial features, clothing detail, fingers, shoes, realistic anatomy, shading, or rendered body volume."
                ),
                (
                    "STAGING SEMANTICS: draw DIRECTOR STAGING OBJECTS from the user's listed object label and "
                    "visual_description; if the label says horse, draw a horse-like rough storyboard silhouette "
                    "in the same screen position, not an anonymous box. STAGING COLOR BAN: if marker=#RRGGBB "
                    "is listed, use that marker color ONLY to find the colored control shape; the output staging "
                    "object MUST be black/gray line art only. It must NOT have colored fill, colored outline, colored tint, "
                    "or marker-colored pixels. Only named actors and listed-panel global props can be colored. "
                    "The same rule applies to vehicles, sedan chairs, box piles, and "
                    "other labeled staging objects."
                    if director_staging_lines
                    else ""
                ),
                (
                    "SCENE DESCRIPTIONS (do not render this text):\n"
                    f"{shots}"
                    "\n\nFINAL CHECK: all humans remain simple directional mannequins; background is line art; no 3GS pixels; "
                    "no text; no extra named characters; one clear frozen story moment per panel."
                ),
            ]
            return "\n\n".join(section for section in compact_sections if section) + compact_registry

        seamless = components.build_seamless_constraint(total_panels)

        # 构建角色/道具 → 出现 panel 编号映射
        tag_panels: dict[str, list[int]] = {}
        prop_panels: dict[str, list[int]] = {}
        if ctx.characters or prop_label_map:
            from novelvideo.models import extract_char_identities_from_markers

            for i, beat in enumerate(ctx.beats[:total_panels], start=1):
                vd = beat.get("visual_description", "")
                char_ids = extract_char_identities_from_markers(vd, strict=False)
                for char_name, identity_id in char_ids.items():
                    if char_name in ctx.characters:
                        tag = (
                            _compute_tag(char_name, identity_id=identity_id)
                            if identity_id
                            else _compute_tag(char_name)
                        )
                        tag_panels.setdefault(tag, []).append(i)
                for prop_id in components._collect_prop_marker_ids([beat]):
                    prop_tag = components.compute_prop_tag(prop_id)
                    if prop_tag in prop_label_map:
                        prop_panels.setdefault(prop_tag, []).append(i)

        # 计算角色数量用于 checklist
        num_chars = len(char_lines)
        if char_names_for_color_law:
            color_check = (
                f"- {num_chars} colored directional mannequins: {', '.join(char_names_for_color_law)}; "
                "unnamed people are gray directional mannequins"
            )
        else:
            color_check = ""
        if prop_tag_color_map:
            prop_tail = (
                "unrelated staging markers, furniture, visually similar untagged props in the same panel or other panels, and background stay black/gray line art"
                if has_director_scene_refs
                else "episode-local props, visually similar untagged props in the same panel or other panels, furniture, and background stay black/gray line art"
            )
            prop_scope_parts = []
            for tag, color in prop_tag_color_map.items():
                panels = prop_tag_panel_map.get(tag, [])
                panel_text = ", ".join(str(p) for p in panels) or "tagged panels only"
                prop_scope_parts.append(f"{tag} {color} only in panels {panel_text}")
            prop_color_check = (
                f"- {len(prop_tag_color_map)} colored global prop markers: "
                f"{'; '.join(prop_scope_parts)}; "
                "each global prop marker must use its exact listed color only, with no "
                "non-assigned hue or material-color override; "
                f"{prop_tail}"
            )
            color_check = f"{color_check}\n{prop_color_check}" if color_check else prop_color_check

        # 角色出现 panel 对照表
        panel_map_lines = []
        for tag, color_name in tag_color_map.items():
            panels = tag_panels.get(tag, [])
            if panels:
                panel_map_lines.append(
                    f"- {tag} {color_name} must appear in panels: {', '.join(str(p) for p in panels)}"
                )
        for prop_tag, _prop_label in prop_label_map.items():
            panels = prop_panels.get(prop_tag, [])
            if panels:
                color_only = prop_tag_color_map.get(prop_tag, "")
                panel_text = ", ".join(str(p) for p in panels)
                if color_only:
                    panel_map_lines.append(
                        f"- {prop_tag} {color_only} must appear colored ONLY in panels: {panel_text}; "
                        "same/similar untagged objects in the same panel or every other panel must remain black/gray line art"
                    )
                else:
                    panel_map_lines.append(f"- {prop_tag} must appear in panels: {panel_text}")
        panel_map_str = "\n".join(panel_map_lines)

        # 动态取第一个实际 tag 作为 checklist 示例
        example_tag = "[TRY_2ec7]"
        if char_lines:
            import re as _re

            _tag_match = _re.search(r"\[(\w+)\]", char_lines[0])
            if _tag_match:
                example_tag = _tag_match.group(0)

        if has_director_scene_refs and has_director_blocking_refs:
            background_check = (
                "- Background is the chosen 3GS camera translated into simplified line-art sketch\n"
                "- No blurred 3GS pixels, no rendered texture, no cinematic lighting, no screenshot look"
            )
        elif rough_gpt_sketch:
            background_check = (
                "- Background canvas is PURE WHITE\n"
                "- Backgrounds are sparse light-gray context only; characters and action must dominate every panel"
            )
        else:
            background_check = "- Background uses inherited scene geometry when available; otherwise keep it minimal and neutral"
        final_checklist = f"""⚠️ FINAL CHECKLIST:
- ZERO text/labels/numbers on image
- DO NOT draw character tags {example_tag} etc. on the image — these are script references only
- Location tags like [场景名] and time tags like [时间] are for YOUR reference only — NEVER render them as text overlays on the image
- EXACTLY {rows}x{cols} = {total_panels} panels, all EQUAL SIZE
- ONE continuous image, NO borders between panels
{color_check}
{panel_map_str}
- Each panel should read as one clear, coherent visual unit with one dominant viewpoint
- Use split-screen, collage, repeated time slices, or multi-stage action only when explicitly implied by the panel description
- ALL human figures are simple DIRECTIONAL STORYBOARD MANNEQUINS, not realistic people
{background_check}""".strip()

        if has_director_scene_refs and has_director_blocking_refs:
            directing = """DIRECTING GUIDELINES:
This pass translates a 3GS director control frame into a normal production sketch.
- Treat the attached scene reference as a camera/topology control input, not a pixel base.
- Ignore older camera plans, exit-path plans, and door-framing plans. The attached image is the current approved camera.
- Do NOT reconstruct, reframe, push in, pull out, rotate, pan, zoom, crop, or choose a cleaner alternate camera.
- Preserve the original camera intent, lens distance, crop, horizon, rough actor screen regions, foreground scale, prop/staging marker positions, and all local furniture screen geometry.
- Preserve physically readable furniture contact: seated mannequins must sit on a visible or implied seat beside/behind the table edge, never inside a table/counter/bench volume. If a 3GS marker intersects furniture, treat it as projection/depth artifact and make the smallest local correction inside the same screen region.
- Projection cleanup is required: convert 3GS/360 fisheye bending, wide-angle stretching, seam cuts, warped floors, curved walls, bowed counters, and tilted verticals into a coherent hand-drawn perspective. Keep the same screen positions and topology; only repair the projection artifact.
- Doors, windows, thresholds, and walkable openings are topology anchors. Repair their lines, but keep their passable-vs-blocking relationship, side angle, open/closed state, depth cue, and screen region. Do not simplify an oblique doorway into a generic front-facing double door or a decorative window. Keep these as clean construction lines only; no dirt, decay, texture, reflections, or material detail in the sketch.
- Replace visible 3GS mannequins in their current approximate screen regions, but the final pose must obey the panel's visual_description action. Make that action readable from pose, facing direction, feet placement, and prop contact.
- Keep the approved 3GS camera, scene topology, door/window/threshold positions, and local furniture geometry. You may make the smallest local adjustment to a mannequin or held prop when required to show the described action clearly; do not reframe the camera or teleport the actor across the room.
- Collapse multi-step beat text into ONE current production pose around the visible placeholder. Do not draw repeated time slices, ghost figures, future exit silhouettes, or a second copy of the same character.
- Reuse visible global prop marker blocks as the action props only when that prop is tagged/listed for this panel. If a visible listed-panel global prop marker becomes held/carried, transform it into the held prop instead of leaving one copy behind and adding another copy.
- Leave unrelated staging marker blocks visible in their original screen position.
- Redraw the original 3GS environment as simplified sketch line art while preserving the approved camera/topology. Do not keep the blurred rendered background.
- Do not do beautification, cinematic lighting, depth of field, motion blur, rendered texture, or material polish here. Only structural projection cleanup is allowed/required.
- Draw the mannequin physical action from visual_description, including facing direction, feet direction, arms/hands holding/carrying/lifting a prop when the beat says so; listed-panel global props use their assigned prop marker color even when the real object would normally be brown/gray/etc.; local/episode props and similar untagged objects stay black/gray line art.
- Add only minimal beat action detail needed for readability, inside the same screen regions.
- Keep each panel legible as a production-usable storyboard image, not a generic summary poster""".format(
                total_panels=total_panels
            )
        elif rough_gpt_sketch:
            directing = """DIRECTING GUIDELINES:
- Use directing freedom only where the panel description leaves camera/framing unspecified.
- Prioritize the written visual_description over generic shot variety.
- Vary shot size/angle only when it does not override explicit visual_description wording.
- Each panel must still depict ONE single frozen moment.""".format(
                total_panels=total_panels
            )
        elif ctx.mode == PromptMode.SKETCH:
            directing = """DIRECTING GUIDELINES:
- Use directing freedom only where the panel description leaves camera/framing unspecified.
- Prioritize the written visual_description over generic shot variety.
- Vary shot size/angle only when it does not override explicit visual_description wording.
- The scene STYLE ANCHOR (if attached) is not a camera lock; design each panel's camera independently unless the panel description asks for that view.""".format(
                total_panels=total_panels
            )
        else:
            directing = """DIRECTING GUIDELINES:
As the director, you have strong creative freedom over shot size, angle, framing, blocking, and composition across the sequence.
- If a scene reference sketch is provided, all directing freedom must remain consistent with the chosen PRIMARY geometry region for that panel. Do NOT invent a new architectural layout.
- Scene geometry is fixed; character blocking may vary shot-to-shot for storytelling, but must remain locally believable inside that fixed geometry.
- For close or reaction shots, change CAMERA DISTANCE before changing ROOM COMPOSITION. Move closer to the same local action zone instead of rebuilding the room around the subject.
- Vary your shot sizes, angles, and framing to create rhythm across the {total_panels} panels
- Use composition and camera language to maximize emotional impact
- Build visual momentum toward climactic moments
- Maintain spatial continuity when characters stay in the same scene
- Avoid repetitive eye-level medium shots when a more intentional camera choice would sharpen the beat
- When neighboring panels describe one continuous action, make them feel editorially connected through contrast, progression, and staging
- Keep each panel legible as a production-usable storyboard image, not a generic summary poster""".format(
                total_panels=total_panels
            )

        scene_geometry_section = f"\n\n{scene_geometry_block}" if scene_geometry_block else ""
        reference_map_section = ""
        if PromptComponents.collect_scene_image_refs(
            ctx
        ) or PromptComponents.collect_prop_image_refs(ctx):
            scene_prop_only_ctx = replace(
                ctx,
                characters={},
            )
            reference_map_section = "\n\n" + components.build_reference_map(
                scene_prop_only_ctx,
                [],
                include_sketch=False,
                include_face_desc=False,
                include_silhouette=False,
            )
        registry_section = (
            f"\n\n{ctx.registry_negative_clause}" if ctx.registry_negative_clause else ""
        )

        return f"{intro}{reference_map_section}{scene_geometry_section}\n{seamless}\n\n{directing}\n\nSCENE DESCRIPTIONS (for your reference only — do NOT render any of this text on the image):\n{shots}\n\n{final_checklist}{registry_section}"


# =============================================================================
# Action Storyboard 模式
# =============================================================================


class ActionStoryboardStrategy:
    """Action Storyboard 模式：将一段动作描述拆解为 25 格连续分镜序列。

    与普通 SKETCH 模式的区别：
    - 所有 25 个 panel 是同一段动作的**连续分镜序列**（不是不同 beat）
    - 从左到右、从上到下展现动作从起始到结束的完整过程
    """

    def build(self, ctx: PromptContext, components: PromptComponents) -> str:
        rows, cols = ctx.grid.rows, ctx.grid.cols
        total_panels = ctx.grid.total_panels

        ascii_layout = components.build_grid_ascii(rows, cols, ctx.grid.is_portrait_panel)
        panel_hint = (
            _panel_ar_hint(ctx.grid.aspect_ratio, rows, cols)
            if ctx.grid.aspect_ratio
            else "SQUARE (1:1)"
        )

        # 从 beats[0] 中获取 action_description（由调用方注入）
        action_description = ""
        if ctx.beats:
            action_description = ctx.beats[0].get("visual_description", "")

        # 构建角色颜色信息（复用 Sketch 的颜色编码逻辑）
        char_lines = []
        char_names_for_color_law = []
        prop_lines = []
        if ctx.characters:
            from novelvideo.utils.identity_resolver import compute_char_tag as _compute_tag

            for char_name, char_cfg in ctx.characters.items():
                body_desc = components.derive_body_descriptor(char_cfg)

                def _format_color_line(tag, body_desc, color_str):
                    if color_str:
                        parts = color_str.split(" ", 1)
                        hex_code = parts[0]
                        color_name = parts[1] if len(parts) > 1 else parts[0]
                        return f"- {tag} — **{color_name} ({hex_code})** figure. {body_desc}."
                    return f"- {tag} — {body_desc}."

                if char_cfg.identity_appearances:
                    for suffix, details in char_cfg.identity_appearances.items():
                        identity_id = f"{char_name}_{suffix}"
                        tag = _compute_tag(char_name, identity_id=identity_id)
                        color = char_cfg.identity_sketch_colors.get(suffix, char_cfg.sketch_color)
                        char_lines.append(_format_color_line(tag, body_desc, color))
                        char_names_for_color_law.append(tag)
                else:
                    tag = _compute_tag(char_name)
                    color = char_cfg.sketch_color
                    char_lines.append(_format_color_line(tag, body_desc, color))
                    char_names_for_color_law.append(tag)

        color_law = ""
        prop_block = ""
        for prop_id in components._collect_prop_marker_ids(ctx.beats):
            prop_tag = components.compute_prop_tag(prop_id)
            prop_lines.append(
                f"- {prop_tag} — LOCAL / EPISODE PROP \"{prop_id}\". "
                "Draw only as black/gray line art if visible; no color fill."
            )

        colored_targets = char_names_for_color_law
        if colored_targets:
            color_law = f"""⚠️ COLOR LAW (NON-NEGOTIABLE):
ONLY these {len(colored_targets)} named elements receive color fill: {', '.join(colored_targets)}
Every other element is black/gray line art only, NO color fill.

COLOR-CODED CHARACTERS:
{chr(10).join(char_lines)}
"""
        if prop_lines:
            prop_block = (
                "\nLOCAL / EPISODE PROPS (never color-coded in sketch):\n"
                f"{chr(10).join(prop_lines)}\n"
            )

        # 替换 action_description 中的 {{}} 为 tag
        resolved_action = action_description
        if ctx.characters:
            from novelvideo.utils.identity_resolver import (
                resolve_visual_description_markers,
                build_identity_to_char_map,
            )

            id_to_char = build_identity_to_char_map(ctx.characters)
            resolved_action = resolve_visual_description_markers(
                resolved_action, ctx.characters, id_to_char, use_identity_id=True
            )

        prompt = f"""Generate a {rows}x{cols} ACTION STORYBOARD grid. Each panel MUST be {panel_hint}.

⚠️ 100% CANVAS COVERAGE — artwork fills ENTIRE canvas edge-to-edge, NO margins/padding/borders.

!!! MANDATORY GRID FORMAT: {rows} ROWS × {cols} COLUMNS !!!

ROLE: You are a MASTER ACTION CHOREOGRAPHER and storyboard artist.

TASK: Decompose the following action sequence into {total_panels} CONTINUOUS FRAMES arranged in a {rows}x{cols} grid. Read left-to-right, top-to-bottom, panels 1→{total_panels} form a SINGLE continuous action sequence from start to finish.

STYLE: **COLOR-CODED STORYBOARD SKETCH** with a minimal neutral background.
Speed and clarity over artistic quality. Focus on CHARACTER PLACEMENT, POSE, and ACTION FLOW.

{color_law}{prop_block}RENDERING RULES:
- Named characters → SOLID FILL in their assigned color
- Unnamed people → gray outline only, no fill
- Unnamed props/environment → black line art, no fill
- Background → minimal and neutral line treatment only
- Focus on POSE, MOVEMENT, and SPATIAL RELATIONS
- Keep figures simple: basic head, torso, limbs

LAYOUT (CRITICAL):
- EXACTLY {rows} rows × {cols} columns = {total_panels} panels total
- Each panel MUST be {panel_hint}
- Panels read LEFT to RIGHT, TOP to BOTTOM (1→{total_panels})
- ONE CONTINUOUS IMAGE, NO borders between panels
- All {total_panels} panels MUST be EQUAL SIZE

⚠️⚠️⚠️ ABSOLUTELY NO TEXT ON IMAGE ⚠️⚠️⚠️

{ascii_layout}

ACTION SEQUENCE TO DECOMPOSE INTO {total_panels} FRAMES:
{resolved_action}

CHOREOGRAPHY GUIDELINES:
- Panel 1: Starting position / setup
- Panels 2-{total_panels - 1}: Progressive action breakdown — each panel advances the action by one beat
- Panel {total_panels}: Final position / resolution
- Maintain consistent character coloring across ALL panels
- Show clear movement progression — each frame should be visually distinct from the previous
- Vary camera angles to emphasize impact moments
- Key strikes/impacts should get dedicated panels

⚠️ FINAL CHECKLIST:
- ZERO text/labels/numbers on image
- EXACTLY {rows}x{cols} = {total_panels} panels, all EQUAL SIZE
- ONE continuous action from panel 1 to panel {total_panels}
- Background stays minimal and neutral"""

        return prompt


# =============================================================================
# 统一入口
# =============================================================================


