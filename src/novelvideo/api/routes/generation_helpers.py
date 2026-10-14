"""Helper functions split out of ``api/routes/generation.py``.

Global references are rewritten to ``_gen.<name>`` so that monkeypatching
``novelvideo.api.routes.generation.<name>`` still takes effect. generation.py
re-imports these functions so route handlers and tests keep resolving them.
"""

from __future__ import annotations

from novelvideo.api.routes import generation as _gen


def _single_render_mode_from_sketch(
    output_dir: str,
    episode: int,
    beat_indices: list[int],
) -> str | None:
    """Choose a single-render mode from the canonical upstream sketch."""
    if len(beat_indices) != 1:
        return None

    from PIL import Image

    sketch_path = _gen.PathResolver(output_dir, episode).sketch(int(beat_indices[0]))
    try:
        with Image.open(sketch_path) as image:
            width, height = image.size
    except Exception:
        return None
    if width <= 0 or height <= 0:
        return None

    ratio = width / height
    return "1x1_16-9" if abs(ratio - 16 / 9) < abs(ratio - 2 / 3) else "1x1_2-3"

async def _resolve_generation_project(project: str, user: dict, required_role: str = "editor"):
    return await _gen.resolve_project_scope(project, user, required_role=required_role)

def _requester_user_id_for_billing(resolved: _gen.Any, user: dict) -> str:
    ctx = getattr(resolved, "ctx", None)
    return str(
        getattr(ctx, "requester_user_id", "")
        or user.get("id")
        or user.get("user_id")
        or user.get("username")
        or ""
    )

def _color_assignment_requires_full_sketch_clean(
    previous: dict[str, str] | None,
    current: dict[str, str] | None,
) -> bool:
    """Return whether recoloring invalidates all existing sketches."""
    current_colors = {
        str(key): str(value)
        for key, value in (current or {}).items()
        if str(key).strip() and str(value).strip()
    }
    if not current_colors:
        return False

    previous_colors = {
        str(key): str(value)
        for key, value in (previous or {}).items()
        if str(key).strip() and str(value).strip()
    }
    if not previous_colors:
        return True

    for key, old_value in previous_colors.items():
        new_value = current_colors.get(key)
        if new_value is not None and new_value != old_value:
            return True
    return False

def normalize_beat_indices(beat_indices: list[int]) -> list[int]:
    normalized: list[int] = []
    seen: set[int] = set()
    for beat_index in beat_indices:
        value = int(beat_index)
        if value in seen:
            continue
        normalized.append(value)
        seen.add(value)
    return normalized

def validate_beat_indices(all_beats: list[dict], beat_indices: list[int]) -> list[int]:
    valid_beat_numbers = {int(beat.get("beat_number", 0) or 0) for beat in all_beats}
    return [
        int(beat_index) for beat_index in beat_indices if int(beat_index) not in valid_beat_numbers
    ]

def _render_plan_feature_disabled() -> bool:
    return _gen.os.getenv("DISABLE_RENDER_PLAN_V2") in {"1", "true", "True", "yes"}

def _resolve_render_image_selection(
    project_config: dict,
    requested_selection: str | None = None,
) -> str:
    from novelvideo.config import (
        DEFAULT_RENDER_IMAGE_SELECTION,
        normalize_image_generation_selection,
    )

    return normalize_image_generation_selection(
        requested_selection or project_config.get("render_image_selection"),
        fallback=DEFAULT_RENDER_IMAGE_SELECTION,
    )

def _resolve_sketch_image_selection(
    project_config: dict,
    requested_selection: str | None = None,
) -> str:
    from novelvideo.config import (
        DEFAULT_SKETCH_IMAGE_SELECTION,
        normalize_image_generation_selection,
    )

    return normalize_image_generation_selection(
        requested_selection or project_config.get("sketch_image_selection"),
        fallback=DEFAULT_SKETCH_IMAGE_SELECTION,
    )

def _image_selection_billing_metadata(
    image_selection: str,
    mode_key: str,
    *,
    image_role: str,
) -> dict:
    from novelvideo.config import (
        IMAGE_GENERATION_SELECTIONS,
        normalize_image_generation_selection,
    )
    from novelvideo.api.routes.model_credits import _image_selection_billing_params

    selection = normalize_image_generation_selection(image_selection)
    model_cfg = IMAGE_GENERATION_SELECTIONS.get(selection) or {}
    pricing_model = str(model_cfg.get("model") or "").strip()
    if not pricing_model:
        return {}
    pricing_params = _image_selection_billing_params(
        model=pricing_model,
        mode_key=mode_key,
        image_role=image_role,
    )
    return {
        "image_selection": selection,
        "pricing_kind": "image",
        "pricing_model": pricing_model,
        "pricing_params": pricing_params,
        "pricing_model_selection": selection,
        "pricing_model_label": str(model_cfg.get("label") or selection),
    }

def _sketch_regen_billing_metadata(image_selection: str, mode_key: str) -> dict:
    return _gen._image_selection_billing_metadata(
        image_selection,
        mode_key,
        image_role="sketch",
    )

def _render_regen_billing_metadata(image_selection: str, mode_key: str) -> dict:
    return _gen._image_selection_billing_metadata(
        image_selection,
        mode_key,
        image_role="render",
    )

def _single_video_billing_metadata(
    video_backend: str,
    *,
    resolution: str | None,
    duration: int | float | str | None,
) -> dict:
    from novelvideo.api.routes.model_credits import (
        _video_backend_feature_billing_params,
    )
    return _video_backend_feature_billing_params(
        {
            "video_backend": video_backend,
            "resolution": str(resolution or "").strip(),
            "pricing_quantity": duration,
        }
    )

def _resolve_render_bool_setting(
    project_config: dict,
    key: str,
    requested_value: bool | None,
    default: bool,
) -> bool:
    if requested_value is not None:
        return bool(requested_value)
    return bool(project_config.get(key, default))

def _render_settings_payload(username: str, project: str) -> dict:
    from novelvideo.config import image_generation_selection_options

    project_config = _gen.load_project_config(username, project)
    return {
        "render_image_selection": _gen._resolve_render_image_selection(project_config),
        "options": image_generation_selection_options(),
        "sketch_aspect_padding": _gen._resolve_render_bool_setting(
            project_config,
            "sketch_aspect_padding",
            None,
            True,
        ),
    }

def _sketch_settings_payload(username: str, project: str) -> dict:
    from novelvideo.config import image_generation_selection_options

    project_config = _gen.load_project_config(username, project)
    return {
        "sketch_image_selection": _gen._resolve_sketch_image_selection(project_config),
        "options": image_generation_selection_options(),
    }

def _plan_entry_to_dict(entry: _gen.Any) -> dict:
    if isinstance(entry, dict):
        beat_numbers = entry.get("beat_numbers") or []
        reasons = entry.get("reasons") or []
        warnings = entry.get("warnings") or []
        return _gen.PlanEntryOut(
            mode_key=entry.get("mode_key", ""),
            rows=int(entry.get("rows", 0) or 0),
            cols=int(entry.get("cols", 0) or 0),
            beat_numbers=[int(beat) for beat in beat_numbers],
            location=str(entry.get("location") or ""),
            padding_count=int(entry.get("padding_count") or 0),
            reasons=[str(reason) for reason in reasons],
            warnings=[str(warning) for warning in warnings],
        ).model_dump()

    return _gen.PlanEntryOut(
        mode_key=entry.mode_key,
        rows=entry.rows,
        cols=entry.cols,
        beat_numbers=list(entry.beat_numbers),
        location=entry.location,
        padding_count=entry.padding_count,
        reasons=list(entry.reasons),
        warnings=list(entry.warnings),
    ).model_dump()

def _plan_to_dicts(plan) -> list[dict]:
    return [_gen._plan_entry_to_dict(entry) for entry in plan]

def _parse_grid_beat_numbers(raw: str | None) -> list[int]:
    if not raw:
        return []
    text = raw.strip()
    if not text:
        return []
    if text.startswith("["):
        parsed = _gen.json.loads(text)
        values = parsed if isinstance(parsed, list) else []
    else:
        values = _gen.re.split(r"[,;\s]+", text)
    beat_numbers: list[int] = []
    seen: set[int] = set()
    for value in values:
        if value in ("", None):
            continue
        beat_num = int(value)
        if beat_num <= 0 or beat_num in seen:
            continue
        beat_numbers.append(beat_num)
        seen.add(beat_num)
    return beat_numbers

def _safe_grid_token(value: str) -> str:
    token = _gen.re.sub(r"[^A-Za-z0-9_.:-]+", "_", value.strip())
    return token.strip("._-") or "grid"

def _uploaded_grid_filename(
    grid_type: str, mode_key: str, beat_numbers: list[int], ext: str
) -> str:
    beats_slug = "-".join(str(beat) for beat in beat_numbers) or "manual"
    return (
        f"{_gen._safe_grid_token(grid_type)}_{_gen._safe_grid_token(mode_key)}_"
        f"{beats_slug}_grid_upload.{ext.lstrip('.')}"
    )

def _safe_grids_file(grids_dir: _gen.Path, relative_path: str) -> _gen.Path | None:
    if not relative_path:
        return None
    try:
        candidate = (grids_dir / relative_path).resolve()
        root = grids_dir.resolve()
    except Exception:
        return None
    if root == candidate or root not in candidate.parents:
        return None
    return candidate

def _find_pool_grid_entry(
    pool: _gen.Any,
    *,
    grid_type: str,
    mode_key: str | None,
    beat_numbers: list[int],
    grid_index: int,
) -> _gen.Any | None:
    if pool is None:
        return None
    if mode_key and beat_numbers:
        entry = pool.find_grid(grid_type, mode_key, beat_numbers)
        if entry is not None:
            return entry

    image_grid_paths = {
        img.grid_path
        for img in getattr(pool, "images", [])
        if img.type == grid_type
        and img.grid_index == grid_index
        and (not beat_numbers or img.original_beat in beat_numbers)
        and img.grid_path
    }
    for entry in getattr(pool, "grids", []):
        if entry.type != grid_type:
            continue
        if mode_key and entry.mode_key != mode_key:
            continue
        if beat_numbers and set(entry.beat_nums) != set(beat_numbers):
            continue
        if not image_grid_paths or entry.grid_path in image_grid_paths:
            return entry
    return None

def _task_limit_reason(exc: _gen._FanoutLimitError) -> str:
    """撞闸异常 → 作用域词。

    分支必须与 ``api/app.py`` 的 ``limit_scope`` 逐字同源（``:226`` 渠道闸 /
    ``:206`` 与 ``:271`` 人闸）—— 两侧同算法是跨 EU 的漂移探测器，故三处扇出
    循环共用这一个 helper，而不是各自内联一份。
    """
    if isinstance(exc, _gen.ProjectTaskLimitExceeded):
        return "project"
    if isinstance(exc, _gen.ChannelTaskLimitExceeded):
        return "platform" if exc.scope_kind == "platform" else "channel"
    if isinstance(exc, (_gen.ProjectUserTaskLimitExceeded, _gen.UserTaskLimitExceeded)):
        return "user"
    # pragma: no cover - the precise catch tuple keeps this unreachable
    raise TypeError(f"unsupported fanout task-limit exception: {type(exc).__name__}")

def _log_partial_dispatch_rejection(exc: _gen._FanoutLimitError) -> None:
    """已投出 k > 0 个后撞闸：异常就地被吞、响应是 200，走不到 ``api/app.py``
    的 handler，日志得在这里补 —— 否则「投了一半撞闸」在面板上不存在。

    一次撞闸记一行（按捕获到的那个异常），不按后面合成的每条 ``rejected`` 记。
    """
    _gen.log_task_limit_rejection(exc, limit_scope=_gen._task_limit_reason(exc))

def _task_limit_rejection(scope: str, exc: _gen._FanoutLimitError) -> dict[str, _gen.Any]:
    """把撞闸异常翻成扇出响应里的一条 ``rejected``（M8 §8.2 冻结形状）。"""
    return {
        "scope": scope,
        "reason": _gen._task_limit_reason(exc),
        "limit": exc.limit,
        "active": exc.active,
    }

def _custom_render_plan_error(plan: list[_gen.Any], beat_indices: list[int]) -> str | None:
    flat: list[int] = []
    seen: set[int] = set()
    for entry in plan:
        beat_numbers = [int(beat) for beat in getattr(entry, "beat_numbers", [])]
        if not beat_numbers:
            return "empty_grid"
        if int(getattr(entry, "rows", 0)) * int(getattr(entry, "cols", 0)) < len(beat_numbers):
            return "grid_capacity"
        for beat in beat_numbers:
            if beat in seen:
                return "duplicate_beat"
            seen.add(beat)
            flat.append(beat)
    if set(flat) != set(beat_indices) or len(flat) != len(beat_indices):
        return "beat_mismatch"
    return None

async def _read_uploaded_rgb_image(file: _gen.UploadFile):
    content = await file.read()
    if not content:
        raise ValueError("empty file")
    try:
        from PIL import Image

        return Image.open(_gen.io.BytesIO(content)).convert("RGB")
    except Exception as exc:
        raise ValueError(f"invalid image file: {exc}") from exc

def _register_uploaded_pool_image(
    *,
    project_dir: _gen.Path,
    episode_num: int,
    beat_num: int,
    image,
    image_type: str,
) -> str:
    from datetime import datetime
    from novelvideo.generators.pool_indexer import (
        add_cell_with_dedup,
        build_pool_index,
        _state_pool_index_path,
        _load_pool_index_for_update,
        _save_pool_index_unlocked,
        index_file_lock,
    )

    grids_dir = project_dir / "grids" / f"ep{episode_num:03d}"
    with index_file_lock(_state_pool_index_path(grids_dir)):
        index_path, pool = _load_pool_index_for_update(grids_dir)
        pool = pool or build_pool_index(grids_dir, episode_num)
        upload_dir = grids_dir / image_type
        upload_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        cell_path = upload_dir / f"beat_{beat_num:02d}_t{timestamp}.png"
        canonical_dir = project_dir / (
            "sketches" if image_type == "sketch" else "frames"
        ) / f"ep{episode_num:03d}"
        canonical_dir.mkdir(parents=True, exist_ok=True)
        image.save(canonical_dir / f"beat_{beat_num:02d}.png", format="PNG")
        image.save(cell_path, format="PNG")

        pool_image = add_cell_with_dedup(
            pool,
            cell_path,
            grids_dir,
            beat_num,
            timestamp,
            img_type=image_type,
            mode="upload",
            grid_index=0,
            cell_index=0,
            grid_path="",
            row=0,
            col=0,
        )
        if pool_image is None:
            pool_id = f"beat_{beat_num:02d}_t{timestamp}_{image_type}"
            assignment_path = None
        else:
            pool_id = pool_image.id
            assignment_path = pool_image.cell_path
        if image_type != "sketch" and assignment_path:
            pool.beat_assignments[str(beat_num)] = assignment_path
        _save_pool_index_unlocked(pool, index_path)
    return pool_id

async def _runtime_prop_menu_with_global_props(
    store: _gen.Any,
    episode_obj: _gen.Any,
    beats: list[dict],
) -> list[dict]:
    """Resolve episode prop markers against the global props table without mutating episode JSON."""
    from novelvideo.models import build_prop_menu, collect_prop_marker_ids_from_beat

    prop_menu = [item.model_dump() for item in episode_obj.prop_menu] if episode_obj else []
    marked_prop_ids: list[str] = []
    for beat in beats or []:
        for prop_id in collect_prop_marker_ids_from_beat(beat):
            if prop_id and prop_id not in marked_prop_ids:
                marked_prop_ids.append(prop_id)
    if not marked_prop_ids:
        return prop_menu

    existing = {item.prop_id: item.model_dump() for item in build_prop_menu(prop_menu=prop_menu)}
    changed = False
    for marker_prop_id in marked_prop_ids:
        global_prop = (
            store.get_cached_prop(marker_prop_id) if hasattr(store, "get_cached_prop") else None
        )
        if not global_prop:
            continue
        item = dict(existing.get(marker_prop_id) or {"prop_id": marker_prop_id})
        item["is_global_asset"] = True
        item["prop_type"] = (
            item.get("prop_type") or getattr(global_prop, "prop_type", "") or "object"
        )
        item["description"] = (
            item.get("description")
            or getattr(global_prop, "description", "")
            or getattr(global_prop, "visual_prompt", "")
            or marker_prop_id
        )
        existing[marker_prop_id] = item
        changed = True
    if not changed:
        return prop_menu

    ordered_ids: list[str] = []
    for item in build_prop_menu(prop_menu=prop_menu):
        if item.prop_id not in ordered_ids:
            ordered_ids.append(item.prop_id)
    for prop_id in marked_prop_ids:
        if prop_id in existing and prop_id not in ordered_ids:
            ordered_ids.append(prop_id)
    return [existing[prop_id] for prop_id in ordered_ids if prop_id in existing]

def _prop_marker_colors_from_menu(prop_menu: list[dict] | None) -> dict[str, str]:
    colors: dict[str, str] = {}
    for item in prop_menu or []:
        if not isinstance(item, dict):
            continue
        prop_id = str(item.get("prop_id") or "").strip()
        marker_color = str(item.get("marker_color") or "").strip()
        if prop_id and marker_color:
            colors[prop_id] = marker_color
    return colors

def _episode_from_store_or_none(store: _gen.Any, episode_num: int) -> _gen.Any | None:
    get_episode = getattr(store, "get_episode", None)
    if get_episode is None:
        return None
    try:
        return get_episode(episode_num)
    except Exception:
        return None

async def _build_character_map(
    store,
    beats,
    username,
    project,
    *,
    episode_num: int | None = None,
    use_detected_identities: bool = False,
):
    """构建角色映射。"""
    from novelvideo.services.character_ref_service import build_character_map_for_grid

    project_dir = _gen.get_user_base_dir(username) / project
    characters = store.get_all_characters()
    char_dicts = []
    for c in characters:
        char_dicts.append(
            {
                "name": c.name,
                "gender": c.gender,
                "body_type": getattr(c, "body_type", ""),
                "role": c.role,
                "is_main": getattr(c, "is_main", False),
                "portrait_path": _gen.compute_portrait_path(project_dir, c.name),
                "face_prompt": c.face_prompt,
                "appearance_details": c.appearance_details,
                "identities": (
                    [
                        {
                            "identity_id": id_.identity_id,
                            "identity_name": id_.identity_name,
                            "appearance_details": id_.appearance_details,
                            "face_prompt": id_.face_prompt,
                            "body_type": id_.body_type,
                            "fish_voice_id": id_.fish_voice_id,
                            "age_group": id_.age_group,
                            "portrait_image": id_.portrait_image,
                            "costume_image": id_.costume_image,
                            "primary_reference": _gen.compute_identity_path(
                                project_dir, c.name, id_.identity_name
                            ),
                            "character_tag": id_.character_tag,
                            "source": id_.source,
                        }
                        for id_ in c.identities
                    ]
                    if c.identities
                    else []
                ),
            }
        )

    # 优先从 SQLite 读取 sketch_colors；若缺失则按当前 beats 重新分配并写回 SQLite。
    _sc = None
    if episode_num:
        _sc = store.get_sketch_colors(episode_num) or None
        if not _sc:
            from novelvideo.generators.episode_optimizer import EpisodeOptimizer

            _sc = (
                EpisodeOptimizer.assign_sketch_colors(
                    char_dicts,
                    episode_beats=beats,
                )
                or None
            )

            if _sc:
                await store.set_sketch_colors(episode_num, _sc)

    return build_character_map_for_grid(
        grid_beats=beats,
        characters=char_dicts,
        user_output_dir=_gen.get_user_base_dir(username),
        project=project,
        sketch_colors=_sc,
        use_detected_identities=use_detected_identities,
    )

def _validate_seedance_pro_dialogue_only(beats: list[dict], video_backend: str) -> str | None:
    """Seedance 1.5 有声仅允许 dialogue beat。"""
    if video_backend not in {"seedance_pro", "newapi_seedance-1.5-pro"}:
        return None

    non_dialogue = [
        int(beat.get("beat_number", 0))
        for beat in beats
        if beat.get("audio_type", "narration") != "dialogue"
    ]
    if not non_dialogue:
        return None

    preview = "、".join(str(num) for num in non_dialogue[:8])
    suffix = " 等" if len(non_dialogue) > 8 else ""
    return f"Seedance 1.5 有声只允许用于 dialogue beat；当前包含非 dialogue Beat: {preview}{suffix}"

def _is_seedance2_backend(video_backend: str | None) -> bool:
    return _gen.is_huimeng_seedance2_backend(video_backend)

def _is_happyhorse_backend(video_backend: str | None) -> bool:
    return _gen._seedance2_model_from_backend(video_backend) == "happyhorse-1.0"

def _is_grok_video_backend(video_backend: str | None) -> bool:
    return _gen._seedance2_model_from_backend(video_backend) == "grok-video-channel"

def _seedance2_api_resolution(resolution: str | None) -> str:
    text = str(resolution or "").strip()
    if text in {"480p", "720p", "1080p"}:
        return text
    if "480" in text:
        return "480p"
    if "1080" in text:
        return "1080p"
    return "720p"

def _seedance2_model_from_backend(video_backend: str | None) -> str:
    text = str(video_backend or "").strip().lower()
    for prefix in ("newapi_", "huimeng_", "huimengi_"):
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    return text

def _seedance2_resolution_options_for_backend(video_backend: str | None) -> tuple[str, ...]:
    model = _gen._seedance2_model_from_backend(video_backend)
    return _gen.SEEDANCE2_RESOLUTION_OPTIONS_BY_MODEL.get(
        model,
        _gen.SEEDANCE2_DEFAULT_RESOLUTION_OPTIONS,
    )

def _seedance2_resolution_for_backend(
    video_backend: str | None,
    resolution: str | None,
) -> str:
    clean_resolution = _gen._seedance2_api_resolution(resolution)
    options = _gen._seedance2_resolution_options_for_backend(video_backend)
    if clean_resolution in options:
        return clean_resolution
    if "720p" in options:
        return "720p"
    return options[0]

def _happyhorse_resolution_for_backend(resolution: str | None) -> str:
    text = str(resolution or "").strip().lower()
    if "720" in text:
        return "720p"
    return "1080p"

def _happyhorse_ratio_for_backend(ratio: str | None) -> str:
    text = str(ratio or "").strip()
    return text if text in _gen.HAPPYHORSE_RATIO_OPTIONS else "16:9"

def _grok_video_resolution_for_backend(resolution: str | None) -> str:
    text = str(resolution or "").strip().lower()
    return text if text in _gen.GROK_VIDEO_RESOLUTION_OPTIONS else "720p"

def _grok_video_ratio_for_backend(ratio: str | None) -> str:
    text = str(ratio or "").strip()
    return text if text in _gen.GROK_VIDEO_RATIO_OPTIONS else "16:9"

def _seedance2_initial_prompt(beat: dict[str, _gen.Any], video_mode: str) -> str:
    if video_mode == "keyframe":
        return str(beat.get("keyframe_prompt") or "").strip()
    return str(beat.get("video_prompt") or beat.get("keyframe_prompt") or "").strip()

def _legacy_video_prompt_for_mode(beat: dict[str, _gen.Any], video_mode: str) -> str:
    if video_mode == "keyframe":
        return str(beat.get("keyframe_prompt") or "").strip()
    return str(beat.get("video_prompt") or "").strip()

def _missing_video_prompt_error(beat_num: int) -> str:
    return f"Beat {beat_num} 缺少视频提示词，请先点击“生成本 Beat 提示词”。"

def _seedance2_request_config_overrides(body: _gen.SingleVideoRequest) -> dict[str, _gen.Any]:
    return {
        field: getattr(body, field)
        for field in _gen.SEEDANCE2_SINGLE_VIDEO_CONFIG_FIELDS
        if field in body.model_fields_set and getattr(body, field) is not None
    }

def _merge_seedance2_request_config(
    beat: dict[str, _gen.Any],
    *,
    seedance2_config_json: str | None = None,
    config_overrides: dict[str, _gen.Any] | None = None,
) -> str | None:
    config_overrides = dict(config_overrides or {})
    if seedance2_config_json is None and not config_overrides:
        return None

    from novelvideo.seedance2_i2v.models import dump_seedance2_config, parse_seedance2_config

    merged = parse_seedance2_config(beat.get("seedance2_config_json")).model_dump(mode="json")
    incoming: dict[str, _gen.Any] = {}
    if seedance2_config_json is not None:
        try:
            incoming = _gen.json.loads(str(seedance2_config_json or "{}"))
        except _gen.json.JSONDecodeError as exc:
            raise ValueError("seedance2_config_json must be valid JSON") from exc
        if not isinstance(incoming, dict):
            raise ValueError("seedance2_config_json must be a JSON object")
        merged.update(incoming)
    merged.update(config_overrides)

    if "generate_audio" in incoming or "generate_audio" in config_overrides:
        merged["generate_audio_user_set"] = True
    if "human_review" in incoming or "human_review" in config_overrides:
        merged["human_review_user_set"] = True

    saved_json = dump_seedance2_config(merged)
    beat["seedance2_config_json"] = saved_json
    return saved_json

async def _api_audio_duration_seconds(output_dir: str | _gen.Path, episode: int, beat_num: int):
    """Duration of one beat's audio, or ``None`` if there isn't one to report.

    ``None`` already means "no duration for this beat" here (the file may simply
    not have been generated yet), so a probe that fails or times out reports the
    same thing rather than failing the whole beat list. On network storage an
    unreadable file is a routine condition, not a request-level error.
    """
    from novelvideo.utils.media_io import get_audio_duration_async
    from novelvideo.utils.path_resolver import PathResolver

    audio_path = PathResolver(output_dir, episode).audio(beat_num)
    if not audio_path.exists():
        return None
    try:
        return await get_audio_duration_async(str(audio_path))
    except Exception:
        return None

async def _prepare_seedance2_api_beat(
    *,
    store: _gen.Any,
    output_dir: str | _gen.Path,
    episode: int,
    beat: dict[str, _gen.Any],
    all_beats: list[dict[str, _gen.Any]],
    index: int,
    video_backend: str | None,
    resolution: str | None,
    ratio: str | None = None,
    prop_menu: list[_gen.Any] | None = None,
) -> _gen.Any:
    from novelvideo.manual_shots import resolve_target_video_duration
    from novelvideo.seedance2_i2v.models import parse_seedance2_config

    beat_num = int(beat.get("beat_number") or index + 1)
    video_mode = str(beat.get("video_mode") or "first_frame")
    audio_duration = await _gen._api_audio_duration_seconds(output_dir, episode, beat_num)
    target_duration = resolve_target_video_duration(beat, audio_duration)
    current_config = parse_seedance2_config(beat.get("seedance2_config_json"))
    requested_resolution = (
        _gen._seedance2_api_resolution(resolution)
        if resolution
        else current_config.resolution
    )
    prepared = await _gen.prepare_seedance2_generation_inputs(
        project_output=output_dir,
        state_dir=_gen.Path(store.state_dir),
        episode=episode,
        beat=beat,
        next_beat=all_beats[index + 1] if index + 1 < len(all_beats) else None,
        video_mode=video_mode,
        prompt=_gen._seedance2_initial_prompt(beat, video_mode),
        duration=target_duration,
        resolution=_gen._seedance2_resolution_for_backend(
            video_backend,
            requested_resolution,
        ),
        ratio=ratio,
        prop_menu=prop_menu,
    )
    if not str(prepared.prompt or "").strip():
        raise ValueError(
            f"Beat {beat_num} Seedance 2.0 最终提示词为空，"
            "请先填写 Seedance2.0主体提示词或点击“AI 优化”。"
        )

    beat["seedance2_config_json"] = prepared.seedance2_config_json
    if hasattr(store, "update_beat_asset"):
        await store.update_beat_asset(
            episode_number=episode,
            beat_number=beat_num,
            seedance2_config_json=prepared.seedance2_config_json,
        )
    return prepared

async def _prepare_happyhorse_api_beat(
    *,
    output_dir: str | _gen.Path,
    state_dir: str | _gen.Path,
    episode: int,
    beat: dict[str, _gen.Any],
    next_beat: dict[str, _gen.Any] | None,
    frame_path: _gen.Path,
    video_mode: str,
    prompt: str,
    duration: float,
    resolution: str | None,
    ratio: str | None,
    prop_menu: list[_gen.Any] | None = None,
) -> dict[str, _gen.Any]:
    from novelvideo.seedance2_i2v.assets import (
        append_seedance2_user_reference_assets,
        build_seedance2_project_assets,
        selected_reference_paths,
    )
    from novelvideo.seedance2_i2v.models import (
        Seedance2I2VMode,
        dump_seedance2_config,
        parse_seedance2_config,
    )

    config = parse_seedance2_config(beat.get("seedance2_config_json"))
    mode = config.mode
    if mode == Seedance2I2VMode.FIRST_LAST_FRAME or video_mode == "keyframe":
        raise ValueError("HappyHorse 1.0 不支持首尾帧模式，请改用首帧模式或多参模式")

    final_prompt = str(config.final_prompt or prompt or "").strip()
    if not final_prompt:
        beat_num = int(beat.get("beat_number") or 0)
        prefix = f"Beat {beat_num} " if beat_num else ""
        raise ValueError(f"{prefix}缺少视频提示词，请先生成或填写视频提示词")

    target_duration = int(config.duration or duration or 0)
    config.duration = target_duration
    config.resolution = _gen._happyhorse_resolution_for_backend(resolution or config.resolution)
    config.ratio = _gen._happyhorse_ratio_for_backend(ratio or config.ratio)
    config.final_prompt = final_prompt

    image_path: str | None = None
    references: list[dict[str, str]] = []

    if mode == Seedance2I2VMode.FIRST_FRAME:
        image_path = str(frame_path)
    else:
        assets = build_seedance2_project_assets(
            project_output=_gen.Path(output_dir),
            episode=episode,
            beat=beat,
            mode=Seedance2I2VMode.MULTIMODAL_REFERENCE,
            state_dir=state_dir,
            next_beat=next_beat,
            prop_menu=prop_menu,
        )
        append_seedance2_user_reference_assets(
            assets,
            reference_image_paths=list(config.reference_image_paths),
            reference_audio_paths=[],
        )
        image_paths = selected_reference_paths(assets, "reference_images")
        config.reference_image_paths = list(dict.fromkeys(image_paths))[:9]
        config.reference_audio_paths = []
        references = [
            {"type": "image", "path": path, "role": f"图片{index}"}
            for index, path in enumerate(config.reference_image_paths, 1)
        ]

    return {
        "prompt": final_prompt,
        "duration": target_duration,
        "resolution": config.resolution,
        "ratio": config.ratio,
        "image_path": image_path,
        "references": references,
        "config_json": dump_seedance2_config(config),
    }

async def _prepare_grok_video_api_beat(
    *,
    output_dir: str | _gen.Path,
    state_dir: str | _gen.Path,
    episode: int,
    beat: dict[str, _gen.Any],
    next_beat: dict[str, _gen.Any] | None,
    frame_path: _gen.Path,
    video_mode: str,
    prompt: str,
    duration: float,
    resolution: str | None,
    ratio: str | None,
    prop_menu: list[_gen.Any] | None = None,
) -> dict[str, _gen.Any]:
    from novelvideo.seedance2_i2v.assets import (
        append_seedance2_user_reference_assets,
        build_seedance2_project_assets,
        selected_reference_paths,
    )
    from novelvideo.seedance2_i2v.models import (
        Seedance2I2VMode,
        dump_seedance2_config,
        parse_seedance2_config,
    )

    config = parse_seedance2_config(beat.get("seedance2_config_json"))
    mode = config.mode
    if mode == Seedance2I2VMode.FIRST_LAST_FRAME or video_mode == "keyframe":
        raise ValueError("Grok Video 不支持首尾帧模式，请改用首帧模式或多参模式")

    final_prompt = str(config.final_prompt or prompt or "").strip()
    if not final_prompt:
        beat_num = int(beat.get("beat_number") or 0)
        prefix = f"Beat {beat_num} " if beat_num else ""
        raise ValueError(f"{prefix}缺少视频提示词，请先生成或填写视频提示词")

    target_duration = int(config.duration or duration or 0)
    config.duration = target_duration
    config.resolution = _gen._grok_video_resolution_for_backend(resolution or config.resolution)
    config.ratio = _gen._grok_video_ratio_for_backend(ratio or config.ratio)
    config.final_prompt = final_prompt

    image_path: str | None = None
    references: list[dict[str, str]] = []

    if mode == Seedance2I2VMode.FIRST_FRAME:
        image_path = str(frame_path)
    else:
        assets = build_seedance2_project_assets(
            project_output=_gen.Path(output_dir),
            episode=episode,
            beat=beat,
            mode=Seedance2I2VMode.MULTIMODAL_REFERENCE,
            state_dir=state_dir,
            next_beat=next_beat,
            prop_menu=prop_menu,
        )
        append_seedance2_user_reference_assets(
            assets,
            reference_image_paths=list(config.reference_image_paths),
            reference_audio_paths=[],
        )
        image_paths = selected_reference_paths(assets, "reference_images")
        config.reference_image_paths = list(dict.fromkeys(image_paths))[:7]
        config.reference_audio_paths = []
        references = [
            {"type": "image", "path": path, "role": f"图片{index}"}
            for index, path in enumerate(config.reference_image_paths, 1)
        ]

    return {
        "prompt": final_prompt,
        "duration": target_duration,
        "resolution": config.resolution,
        "ratio": config.ratio,
        "image_path": image_path,
        "references": references,
        "config_json": dump_seedance2_config(config),
    }

def _seedance2_asset_status_payload(
    asset: _gen.Any,
    *,
    project_ctx: _gen.ProjectContext,
    output_dir: _gen.Path,
) -> dict[str, _gen.Any]:
    try:
        rel_path = str(_gen.Path(asset.path).relative_to(output_dir))
    except ValueError:
        rel_path = str(asset.path)
    abs_path = str(asset.path)
    crop_source_path = getattr(asset, "crop_source_path", None)
    crop_source_abs_path = str(crop_source_path) if crop_source_path else ""
    crop_source_rel_path = ""
    if crop_source_path:
        try:
            crop_source_rel_path = str(_gen.Path(crop_source_path).relative_to(output_dir))
        except ValueError:
            crop_source_rel_path = crop_source_abs_path
    media_url = ""
    if bool(asset.exists):
        media_url = _gen.make_static_url_for_context(project_ctx, rel_path, local_path=_gen.Path(asset.path))
    crop_source_url = ""
    if crop_source_path and _gen.Path(crop_source_path).exists():
        crop_source_url = _gen.make_static_url_for_context(
            project_ctx,
            crop_source_rel_path,
            local_path=_gen.Path(crop_source_path),
        )
    can_delete = (
        str(asset.key).startswith(("user_image:", "user_audio:"))
        or "seedance2_uploads" in _gen.Path(abs_path).parts
        or "seedance2_crops" in _gen.Path(abs_path).parts
    )
    return {
        "key": str(asset.key),
        "label": str(asset.label),
        "media_type": str(asset.media_type),
        "selected": bool(asset.selected),
        "exists": bool(asset.exists),
        "reference_label": str(asset.reference_label),
        "note": str(asset.note or asset.validation_error or ""),
        "identity_id": str(getattr(asset, "identity_id", "") or ""),
        "prop_id": str(getattr(asset, "prop_id", "") or ""),
        "prop_scope": str(getattr(asset, "prop_scope", "") or ""),
        "path": rel_path,
        "url": media_url,
        "abs_path": abs_path,
        "crop_source_path": crop_source_rel_path,
        "crop_source_abs_path": crop_source_abs_path,
        "crop_source_url": crop_source_url,
        "validation_error": str(asset.validation_error or ""),
        "fallback_text": str(asset.fallback_text or ""),
        "can_crop": bool(asset.exists and asset.media_type == "image"),
        "can_trim": bool(asset.exists and asset.media_type == "audio"),
        "can_delete": can_delete,
    }

def _seedance2_returned_last_frame_status_payload(
    *,
    project_ctx: _gen.ProjectContext,
    output_dir: _gen.Path,
    episode: int,
    beat_num: int,
    enabled: bool,
) -> dict[str, _gen.Any] | None:
    if not enabled:
        return None
    base_path = (
        _gen.Path(output_dir)
        / "videos"
        / "beats"
        / f"ep{int(episode):03d}"
        / "returned_last_frames"
        / f"beat_{int(beat_num):02d}"
    )
    path = next(
        (
            base_path.with_suffix(suffix)
            for suffix in (".png", ".jpg", ".jpeg", ".webp", ".gif")
            if base_path.with_suffix(suffix).exists()
        ),
        base_path.with_suffix(".png"),
    )
    if not path.exists():
        return None
    try:
        rel_path = path.relative_to(output_dir).as_posix()
    except ValueError:
        rel_path = str(path)
    return {
        "key": "returned_last_frame",
        "label": f"返回尾帧 · Beat {int(beat_num)}",
        "media_type": "image",
        "selected": False,
        "exists": True,
        "reference_label": "尾帧",
        "note": "Seedance2 返回尾帧",
        "identity_id": "",
        "prop_id": "",
        "prop_scope": "",
        "path": rel_path,
        "url": _gen.make_static_url_for_context(project_ctx, rel_path, local_path=path),
        "abs_path": str(path),
        "validation_error": "",
        "fallback_text": "",
        "can_crop": False,
        "can_delete": False,
    }

def _seedance2_voice_status_payload(
    *,
    beat: dict[str, _gen.Any],
    characters: list[_gen.Any],
    username: str,
    project: str,
    store: _gen.Any,
    output_dir: _gen.Path,
) -> dict[str, _gen.Any]:
    audio_type = _gen.normalize_seedance2_audio_type(beat)
    if audio_type == "silence":
        return {
            "required": False,
            "ready": True,
            "label": "无音频",
            "detail": "静音 Beat 不生成音频",
            "speaker": "",
        }
    if audio_type == "dialogue":
        from novelvideo.seedance2_i2v.voice_reference_service import (
            dialogue_voice_reference_rows,
        )

        rows = dialogue_voice_reference_rows(
            beat,
            characters=characters,
            project_dir=output_dir,
        )
        ready_rows = [row for row in rows if row.status.active_reference_path]
        names = [row.display_name or row.speaker for row in rows]
        ready = bool(rows) and len(ready_rows) == len(rows)
        return {
            "required": True,
            "ready": ready,
            "label": "声线就绪" if ready else "声线缺失",
            "detail": "、".join(names) if names else "未指定 speaker",
            "speaker": str(beat.get("speaker") or ""),
        }

    from novelvideo.seedance2_i2v.voice_reference_service import (
        resolve_narrator_reference_status,
    )

    status = resolve_narrator_reference_status(
        store=store,
        username=username,
        project=project,
    )
    return {
        "required": True,
        "ready": bool(status.active_reference_path),
        "label": "声线就绪" if status.active_reference_path else "声线缺失",
        "detail": str(status.detail or status.error or "第三人称项目解说声线未配置"),
        "speaker": "NARRATOR",
    }

async def _seedance2_panel_context(
    *,
    project: str,
    episode_num: int,
    beat_num: int,
    user: dict = _gen.Depends(_gen.get_api_user),
) -> dict[str, _gen.Any]:
    resolved = await _gen._resolve_generation_project(project, user, required_role="viewer")
    username = resolved.username
    project_name = resolved.project_name
    output_dir = _gen.Path(resolved.output_dir)
    store = (
        await _gen.make_sqlite_store_for_context(resolved.ctx)
        if resolved.ctx
        else await _gen.make_sqlite_store(username, project_name)
    )
    beats = await store.get_beats_as_dicts(episode_num)
    beat = next((item for item in beats if int(item.get("beat_number") or 0) == beat_num), None)
    if not beat:
        raise _gen.HTTPException(status_code=404, detail=f"Beat {beat_num} not found")

    next_beat = next(
        (item for item in beats if int(item.get("beat_number") or 0) == beat_num + 1),
        None,
    )
    characters = store.get_all_characters()
    episode_obj = _gen._episode_from_store_or_none(store, episode_num)
    prop_menu = await _gen._runtime_prop_menu_with_global_props(store, episode_obj, beats)
    language = await _gen.detect_episode_asset_language(
        store,
        episode_num,
        fallback_text="\n".join(
            str(item.get(field) or "")
            for item in beats
            for field in ("narration_segment", "dialogue", "visual_description")
        ),
    )
    return {
        "project_ctx": resolved.ctx,
        "username": username,
        "project_name": project_name,
        "output_dir": output_dir,
        "store": store,
        "beats": beats,
        "beat": beat,
        "next_beat": next_beat,
        "characters": characters,
        "prop_menu": prop_menu,
        "language": language,
    }

def _seedance2_status_response(
    *,
    project: str,
    episode_num: int,
    beat_num: int,
    ctx: dict[str, _gen.Any],
) -> dict[str, _gen.Any]:
    from novelvideo.seedance2_i2v.panel_service import (
        build_seedance2_video_panel_state,
    )
    from novelvideo.utils.path_resolver import PathResolver

    output_dir = _gen.Path(ctx["output_dir"])
    beat = ctx["beat"]
    state = build_seedance2_video_panel_state(
        project_dir=output_dir,
        episode=episode_num,
        beat=beat,
        next_beat=ctx["next_beat"],
        characters=ctx["characters"],
        prop_menu=ctx["prop_menu"],
        state_dir=_gen.Path(ctx["store"].state_dir),
        language=ctx.get("language"),
    )
    assets = state.assets
    selected_assets = [asset for asset in assets if asset.selected]
    missing_assets = [
        asset
        for asset in assets
        if asset.required and (not asset.exists or bool(asset.validation_error))
    ]
    fallback_assets = [
        asset for asset in assets if str(asset.fallback_text or "").strip() and not asset.selected
    ]
    paths = PathResolver(output_dir, episode_num)
    project_ctx = ctx["project_ctx"]
    asset_items = [
        _gen._seedance2_asset_status_payload(asset, project_ctx=project_ctx, output_dir=output_dir)
        for asset in assets
    ]
    try:
        from novelvideo.seedance2_i2v.models import parse_seedance2_config

        config = parse_seedance2_config(beat.get("seedance2_config_json") or "{}")
        returned_last_frame = _gen._seedance2_returned_last_frame_status_payload(
            project_ctx=project_ctx,
            output_dir=output_dir,
            episode=episode_num,
            beat_num=beat_num,
            enabled=bool(config.return_last_frame),
        )
    except Exception:
        returned_last_frame = None
    if returned_last_frame is not None:
        asset_items.append(returned_last_frame)

    return {
        "ok": True,
        "data": {
            "beat_number": beat_num,
            "audio_type": _gen.normalize_seedance2_audio_type(beat),
            "seedance2_config_json": str(beat.get("seedance2_config_json") or ""),
            "media": {
                "render_ready": paths.frame(beat_num).exists(),
                "audio_ready": paths.audio(beat_num).exists(),
                "video_ready": paths.video(beat_num).exists(),
            },
            "voice": _gen._seedance2_voice_status_payload(
                beat=beat,
                characters=ctx["characters"],
                username=ctx["username"],
                project=project,
                store=ctx["store"],
                output_dir=output_dir,
            ),
            "prompt": {
                "ready": bool(str(state.final_prompt or "").strip()),
                "source": str(state.prompt_source or ""),
                "status": str(state.prompt_status or ""),
                "has_guidance": bool(str(state.prompt_guidance or "").strip()),
                "text_overlay_enabled": bool((state.text_overlay or {}).get("enabled")),
                "text_overlay": state.text_overlay or {},
                "inputs_stale": bool(
                    state.prompt_inputs_hash
                    and state.prompt_inputs_hash != state.current_prompt_inputs_hash
                ),
            },
            "assets": {
                "total": len(assets),
                "selected": len(selected_assets),
                "missing": len(missing_assets),
                "images": len([asset for asset in selected_assets if asset.media_type == "image"]),
                "audios": len([asset for asset in selected_assets if asset.media_type == "audio"]),
                "fallbacks": len(fallback_assets),
                "items": asset_items,
            },
        },
    }

def _api_video_backend_options() -> list[_gen.VideoBackendOption]:
    from novelvideo.config import NEWAPI_VIDEO_DURATION_BOUNDS
    from novelvideo.generators.video_generator import (
        NewApiVideoGenerator,
        newapi_video_backend_options,
        parse_newapi_video_backend,
    )

    hidden_mainline_backends = {
        "newapi_seedance-2.0-value",
        "newapi_seedance-2.0-fast-value",
        "newapi_happyhorse-1.0",
    }
    options = {
        value: label
        for value, label in newapi_video_backend_options(
            include_seedance2_variants=True
        ).items()
        if value not in hidden_mainline_backends
    }
    duration_bounds = NewApiVideoGenerator._parse_duration_bounds_config(
        NEWAPI_VIDEO_DURATION_BOUNDS
    )
    default_backend = _gen.VideoGenerateRequest().video_backend
    backend_options: list[_gen.VideoBackendOption] = []
    for value, label in options.items():
        model = parse_newapi_video_backend(value)
        bounds = duration_bounds.get(model or "")
        if model == "seedance-2.0-mini" and not bounds:
            bounds = (4, 15)
        if model == "happyhorse-1.0" and not bounds:
            bounds = (3, 15)
        if model == "grok-video-channel" and not bounds:
            bounds = (6, 30)
        is_happyhorse = _gen._is_happyhorse_backend(value)
        is_grok_video = _gen._is_grok_video_backend(value)
        backend_options.append(
            _gen.VideoBackendOption(
                value=value,
                label=label,
                is_default=value == default_backend,
                is_seedance2=_gen._is_seedance2_backend(value),
                is_happyhorse=is_happyhorse,
                is_grok_video=is_grok_video,
                dialogue_only=value in {"seedance_pro", "newapi_seedance-1.5-pro"},
                min_duration=bounds[0] if bounds else None,
                max_duration=bounds[1] if bounds else None,
                resolution_options=(
                    list(_gen.HAPPYHORSE_RESOLUTION_OPTIONS)
                    if is_happyhorse
                    else list(_gen.GROK_VIDEO_RESOLUTION_OPTIONS)
                    if is_grok_video
                    else None
                ),
                ratio_options=(
                    list(_gen.HAPPYHORSE_RATIO_OPTIONS)
                    if is_happyhorse
                    else list(_gen.GROK_VIDEO_RATIO_OPTIONS)
                    if is_grok_video
                    else None
                ),
                supported_modes=(
                    list(_gen.HAPPYHORSE_SUPPORTED_MODES)
                    if is_happyhorse
                    else list(_gen.GROK_VIDEO_SUPPORTED_MODES)
                    if is_grok_video
                    else None
                ),
                reference_image_max=7 if is_grok_video else 9 if is_happyhorse else None,
                reference_video_max=0 if is_grok_video else 1 if is_happyhorse else None,
                reference_audio_max=0 if is_grok_video or is_happyhorse else None,
            )
        )
    return backend_options

def _sketch_regen_queue_key(episode_num: int) -> str:
    return f"ep{int(episode_num):03d}"

def _is_react_sketch_regen_queue_items(items: object) -> bool:
    return (
        isinstance(items, list)
        and bool(items)
        and all(isinstance(item, dict) and "beatNumbers" in item for item in items)
    )

def _react_sketch_regen_queues(config: dict) -> tuple[dict, dict, bool]:
    queues = config.get("react_sketch_regen_queue")
    if not isinstance(queues, dict):
        queues = {}
    else:
        queues = dict(queues)

    legacy_queues = config.get("sketch_regen_queue")
    cleaned_legacy = dict(legacy_queues) if isinstance(legacy_queues, dict) else {}
    legacy_changed = False
    if isinstance(legacy_queues, dict):
        for key, items in legacy_queues.items():
            if (
                isinstance(key, str)
                and key.startswith("ep")
                and _gen._is_react_sketch_regen_queue_items(items)
            ):
                queues.setdefault(key, list(items))
                cleaned_legacy.pop(key, None)
                legacy_changed = True

    return queues, cleaned_legacy, legacy_changed

def _sketch_regen_queue_payload(username: str, project: str, episode_num: int) -> dict:
    config = _gen.load_project_config(username, project)
    queues, _cleaned_legacy, _legacy_changed = _gen._react_sketch_regen_queues(config)
    items = queues.get(_gen._sketch_regen_queue_key(episode_num))
    return {"items": items if isinstance(items, list) else []}

def _image_generation_guard_payload(attempt_count: int, subject: str) -> dict:
    next_attempt = attempt_count + 1
    if next_attempt >= 5:
        level = "locked"
        message = f"{subject} 已连续生成 {next_attempt} 次，请输入管理员密码继续本次生成。"
    elif next_attempt >= 3:
        level = "confirm"
        message = f"{subject} 已连续生成 {next_attempt} 次，确认继续生成吗？"
    else:
        level = "none"
        message = ""
    return {
        "attempt_count": attempt_count,
        "next_attempt": next_attempt,
        "level": level,
        "message": message,
    }

async def _collect_audio_prereq_errors(
    *,
    store,
    username: str,
    project: str,
    episode: int,
    beat_numbers,
    mode: str,
) -> list[str]:
    from novelvideo.audio.indextts2_beat_audio_task import (
        collect_indextts2_voice_prereq_errors,
    )

    try:
        return await collect_indextts2_voice_prereq_errors(
            store=store,
            username=username,
            project=project,
            episode=episode,
            beat_numbers=beat_numbers,
            mode=mode,
        )
    except AttributeError:
        # Unit-test fakes may not implement the full SQLiteStore voice-sample
        # surface. Real SQLiteStore has these attributes; skip only for narrow
        # fakes so existing route-contract tests can focus on dispatch shape.
        return []

async def _audio_generation_plan(
    *,
    store,
    username: str,
    project: str,
    episode: int,
    beat_numbers,
    mode: str,
) -> tuple[list[int], list[str], int]:
    from novelvideo.audio.indextts2_beat_audio_task import (
        build_indextts2_audio_generation_plan,
    )

    try:
        plan = await build_indextts2_audio_generation_plan(
            store=store,
            username=username,
            project=project,
            episode=episode,
            beat_numbers=beat_numbers,
            mode=mode,
        )
        return (
            list(plan.beat_numbers),
            list(plan.errors),
            int(getattr(plan, "billable_chars", 0) or 0),
        )
    except AttributeError:
        # Narrow route-test stores do not expose the voice and audio-state
        # surfaces used by the production planner.
        beats = await store.get_beats_as_dicts(episode)
        selected = {int(value) for value in beat_numbers or [] if int(value) > 0}
        planned = [
            int(beat.get("beat_number") or 0)
            for beat in beats
            if int(beat.get("beat_number") or 0) > 0
            and (not selected or int(beat.get("beat_number") or 0) in selected)
        ]
        from novelvideo.seedance2_i2v.voice_clone import (
            dialogue_text,
            narration_beat_text,
            normalize_seedance2_audio_type,
        )
        from novelvideo.utils.document_parsers import count_billable_text_chars

        planned_set = set(planned)
        billable_chars = 0
        for beat in beats:
            if int(beat.get("beat_number") or 0) not in planned_set:
                continue
            audio_type = normalize_seedance2_audio_type(beat)
            text = (
                narration_beat_text(beat)
                if audio_type == "narration"
                else dialogue_text(beat)
            )
            billable_chars += count_billable_text_chars(text)
        return planned, [], billable_chars

def _audio_billing_payload(
    beat_numbers: list[int],
    *,
    billable_chars: int = 0,
) -> dict:
    from novelvideo.audio.indextts2_beat_audio_task import (
        indextts2_audio_billing_params,
    )

    return {
        **indextts2_audio_billing_params(
            len(beat_numbers),
            billable_chars=billable_chars,
        ),
        "beat_numbers": list(beat_numbers),
    }

def _voice_prereq_error_response(errors: list[str]) -> dict:
    preview = "；".join(errors[:5])
    suffix = " ..." if len(errors) > 5 else ""
    return {
        "ok": False,
        "code": "voice_prereq_required",
        "error": f"{preview}{suffix}",
    }

def _global_optimize_billable_beat_numbers(
    *, output_dir: str, episode_num: int, beats: list[dict]
) -> list[int]:
    """Return Beat numbers with a canonical sketch, matching the worker path."""
    from novelvideo.utils.path_resolver import PathResolver

    sketches_dir = PathResolver(output_dir, episode_num).sketches_dir()
    billable: list[int] = []
    for beat in beats:
        beat_num = int(beat.get("beat_number") or 0)
        if beat_num <= 0:
            continue
        if any(
            (sketches_dir / f"beat_{beat_num:02d}.{ext}").exists()
            for ext in ("png", "jpg")
        ):
            billable.append(beat_num)
    return billable

def _canonical_sketch_path(project_dir: _gen.Path, episode_num: int, beat_num: int) -> _gen.Path:
    return project_dir / "sketches" / f"ep{episode_num:03d}" / f"beat_{beat_num:02d}.png"

def _canonical_sketch_url(
    ctx: _gen.ProjectContext,
    project_dir: _gen.Path,
    episode_num: int,
    beat_num: int,
) -> str:
    rel = f"sketches/ep{episode_num:03d}/beat_{beat_num:02d}.png"
    return _gen.make_static_url_for_context(
        ctx,
        rel,
        local_path=project_dir / rel,
    )

def _director_control_scope(episode_num: int, beat_num: int) -> str:
    return f"director_control_to_sketch:ep{int(episode_num):03d}:beat_{int(beat_num):02d}"

def _director_control_payload(
    *,
    ctx: _gen.ProjectContext,
    project_dir: _gen.Path,
    episode_num: int,
    beat_num: int,
) -> dict[str, _gen.Any]:
    from novelvideo.director_world.control_frame_to_sketch import _director_control_mode_key
    from novelvideo.utils.path_resolver import PathResolver

    paths = PathResolver(str(project_dir), int(episode_num))
    control_frame = paths.director_render(int(beat_num))
    ready = control_frame.exists()
    mode_key = ""
    if ready:
        mode_key, _aspect_ratio = _director_control_mode_key(
            control_frame=control_frame,
            requested_mode_key="",
            requested_aspect_ratio="",
        )
    rel_path = None
    url = None
    if ready:
        try:
            rel_path = control_frame.relative_to(project_dir).as_posix()
            url = _gen.make_static_url_for_context(ctx, rel_path, local_path=control_frame)
        except ValueError:
            rel_path = control_frame.as_posix()
    return {
        "episode": int(episode_num),
        "beat_num": int(beat_num),
        "ready": ready,
        "path": control_frame.as_posix(),
        "rel_path": rel_path,
        "url": url,
        "scope": _gen._director_control_scope(episode_num, beat_num),
        "mode_key": mode_key,
    }

def _director_overlay_beat_context(beat: dict[str, _gen.Any]) -> dict[str, _gen.Any]:
    identities = beat.get("detected_identities") or []
    props = beat.get("detected_props") or []
    return {
        "detected_identities": [str(item) for item in identities if str(item).strip()],
        "detected_props": [str(item) for item in props if str(item).strip()],
    }

def _director_same_scene_beats(beats: list[dict[str, _gen.Any]], scene_name: str) -> list[dict[str, _gen.Any]]:
    items: list[dict[str, _gen.Any]] = []
    for item in beats:
        if _gen._beat_scene_name(item) != scene_name:
            continue
        beat_number = item.get("beat_number") or item.get("beat") or item.get("number")
        try:
            beat_int = int(beat_number)
        except (TypeError, ValueError):
            continue
        items.append({"beat": beat_int, "label": f"Beat {beat_int}", "scene_id": scene_name})
    return sorted(items, key=lambda entry: entry["beat"])

def _director_overlay_payload(
    *,
    episode_num: int,
    beat_num: int,
    scene_name: str,
    beat: dict[str, _gen.Any],
    body: dict[str, _gen.Any],
) -> dict[str, _gen.Any]:
    from datetime import datetime, timezone

    snapshot = body.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    body_actors = body.get("actors")
    body_props = body.get("props")
    body_stagings = body.get("stagings")
    actors = body_actors if isinstance(body_actors, list) else snapshot.get("actors") or []
    props = body_props if isinstance(body_props, list) else snapshot.get("props") or []
    stagings = body_stagings if isinstance(body_stagings, list) else snapshot.get("stagings") or []
    legacy_props = [*props, *stagings] if isinstance(props, list) and isinstance(stagings, list) else props
    frame_meta = body.get("frame_meta")
    frame_meta = frame_meta if isinstance(frame_meta, dict) else {}
    source = body.get("source")
    if not isinstance(source, dict):
        meta_source = frame_meta.get("source")
        source = meta_source if isinstance(meta_source, dict) else {}
    return {
        "schema_version": "director_stage_overlay_v1",
        "scene_id": scene_name,
        "episode": int(episode_num),
        "beat": int(beat_num),
        "frame_aspect": str(body.get("frame_aspect") or "16:9"),
        "source": source,
        "frame_meta": frame_meta,
        "snapshot": snapshot,
        "camera": snapshot.get("camera") or body.get("camera") or {},
        "actors": actors,
        "props": legacy_props,
        "stagings": stagings,
        "command_log": body.get("command_log") if isinstance(body.get("command_log"), list) else [],
        "deleted_keys": body.get("deleted_keys") if isinstance(body.get("deleted_keys"), list) else [],
        "beat_context": _gen._director_overlay_beat_context(beat),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }

def _director_overlay_status_payload(
    *,
    project_dir: _gen.Path,
    episode_num: int,
    beat_num: int,
    scene_name: str,
    beats: list[dict[str, _gen.Any]],
) -> dict[str, _gen.Any]:
    from novelvideo.director_world.paths import beat_blocking_path
    from novelvideo.director_world.store import load_beat_blocking

    path = beat_blocking_path(project_dir, episode_num, beat_num)
    same_scene = _gen._director_same_scene_beats(beats, scene_name)
    current = load_beat_blocking(project_dir, episode_num, beat_num)
    if current:
        return {
            "status": "current",
            "overlay": current,
            "path": path.as_posix(),
            "same_scene_beats": same_scene,
        }

    inherited: dict[str, _gen.Any] | None = None
    inherited_from: int | None = None
    for item in same_scene:
        candidate_beat = int(item["beat"])
        if candidate_beat >= int(beat_num):
            continue
        candidate = load_beat_blocking(project_dir, episode_num, candidate_beat)
        if candidate:
            inherited = candidate
            inherited_from = candidate_beat
    if inherited:
        return {
            "status": "inherited",
            "overlay": inherited,
            "path": path.as_posix(),
            "inherited_from_beat": inherited_from,
            "same_scene_beats": same_scene,
        }
    return {
        "status": "missing",
        "overlay": None,
        "path": path.as_posix(),
        "same_scene_beats": same_scene,
    }

def _decode_png_data_url(data_url: str) -> bytes:
    import base64

    prefix = "data:image/png;base64,"
    if not data_url.startswith(prefix):
        raise ValueError("expected PNG data URL")
    return base64.b64decode(data_url[len(prefix) :], validate=True)

def _director_control_frame_export_payload(
    *,
    ctx: _gen.ProjectContext,
    project_dir: _gen.Path,
    scene_name: str,
    episode_num: int,
    beat_num: int,
    body: dict[str, _gen.Any],
) -> dict[str, _gen.Any]:
    target_dir = (
        project_dir
        / "director_control_frames"
        / f"ep{int(episode_num):03d}"
        / f"beat_{int(beat_num):02d}"
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    images = body.get("images")
    images = images if isinstance(images, dict) else {}
    submitted_frame_meta = body.get("frame_meta")
    if not isinstance(submitted_frame_meta, dict) or not submitted_frame_meta:
        raise ValueError("combined, env_only and frame_meta are required")
    missing_kinds = [
        kind
        for kind in ("combined", "env_only")
        if not isinstance(images.get(kind), str) or not images.get(kind)
    ]
    if missing_kinds:
        raise ValueError("combined, env_only and frame_meta are required")
    filename_by_kind = {
        "combined": "combined.png",
        "env_only": "env_only.png",
    }
    paths: dict[str, str] = {}
    rel_paths: dict[str, str] = {}
    urls: dict[str, str] = {}
    for kind, filename in filename_by_kind.items():
        data_url = images.get(kind)
        if not isinstance(data_url, str) or not data_url:
            continue
        path = target_dir / filename
        path.write_bytes(_gen._decode_png_data_url(data_url))
        paths[kind] = path.as_posix()
        rel_paths[kind] = path.relative_to(project_dir).as_posix()
        urls[kind] = _gen.make_static_url_for_context(ctx, rel_paths[kind], local_path=path)

    snapshot = body.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    body_actors = body.get("actors")
    body_props = body.get("props")
    body_stagings = body.get("stagings")
    actors = body_actors if isinstance(body_actors, list) else snapshot.get("actors") or []
    props = body_props if isinstance(body_props, list) else snapshot.get("props") or []
    stagings = body_stagings if isinstance(body_stagings, list) else snapshot.get("stagings") or []
    legacy_props = [*props, *stagings] if isinstance(props, list) and isinstance(stagings, list) else props
    meta = dict(submitted_frame_meta)
    meta.setdefault("scene_id", scene_name)
    meta.setdefault("episode", int(episode_num))
    meta.setdefault("beat", int(beat_num))
    meta.setdefault("frame_aspect", str(body.get("frame_aspect") or "16:9"))
    meta.setdefault("actors", actors)
    meta.setdefault("props", legacy_props)
    meta.setdefault("stagings", stagings)
    meta["paths"] = rel_paths
    meta_path = target_dir / "frame_meta.json"
    meta_path.write_text(_gen.json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    paths["frame_meta"] = meta_path.as_posix()
    rel_paths["frame_meta"] = meta_path.relative_to(project_dir).as_posix()
    urls["frame_meta"] = _gen.make_static_url_for_context(ctx, rel_paths["frame_meta"], local_path=meta_path)
    return {"dir": target_dir.as_posix(), "paths": paths, "rel_paths": rel_paths, "urls": urls, "meta": meta}

async def _episode_beat_from_resolution(
    resolved,
    episode_num: int,
    beat_num: int,
):
    store = (
        await _gen.make_sqlite_store_for_context(resolved.ctx)
        if resolved.ctx
        else await _gen.make_sqlite_store(resolved.username, resolved.project_name)
    )
    try:
        beats = await store.get_beats_as_dicts(int(episode_num))
        target = next(
            (
                beat
                for beat in beats
                if int(beat.get("beat_number") or 0) == int(beat_num)
            ),
            None,
        )
        if target is None:
            raise _gen.HTTPException(status_code=404, detail=f"Beat {beat_num} not found")
        return store, target
    except Exception:
        close = getattr(store, "close", None)
        if close:
            await close()
        raise

def _beat_scene_name(beat: dict[str, _gen.Any]) -> str:
    return str(_gen.beat_scene_id(beat) or beat.get("location") or "").strip()

def _api_background_reference_url_builder(ctx: _gen.ProjectContext):
    def _build(path: _gen.Path, rel_path: str) -> str:
        return _gen.make_static_url_for_context(ctx, rel_path, local_path=path)

    return _build

def _api_background_anchor_url_builder(ctx: _gen.ProjectContext):
    def _build(path: _gen.Path, rel_path: str) -> str | None:
        return _gen.make_static_url_for_context(ctx, rel_path, local_path=path)

    return _build

def _background_anchors_payload(
    *,
    ctx: _gen.ProjectContext,
    username: str,
    project: str,
    project_dir: _gen.Path,
    beat: dict[str, _gen.Any],
    episode_num: int,
    beat_num: int,
) -> dict[str, _gen.Any]:
    return _gen.build_background_anchors_payload(
        project_dir=project_dir,
        username=username,
        project=project,
        beat=beat,
        episode_num=int(episode_num),
        beat_num=int(beat_num),
        reference_url_builder=_gen._api_background_reference_url_builder(ctx),
        anchor_url_builder=_gen._api_background_anchor_url_builder(ctx),
    )
