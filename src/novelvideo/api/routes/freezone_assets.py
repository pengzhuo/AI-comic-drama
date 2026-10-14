"""Block of helpers split out of ``novelvideo/api/routes/freezone.py``.

Global references are rewritten to ``_fz.<name>`` so monkeypatching the
source module still takes effect; the source re-imports these names.
"""

from __future__ import annotations

import novelvideo.api.routes.freezone as _fz


@_fz.router.get("/projects/{project}/freezone/assets", tags=[_fz.TAG_FREEZONE_ASSETS])
async def list_freezone_assets(
    project: str,
    user: dict = _fz.Depends(_fz.get_api_user),
):
    """列出当前项目作用域下可供 Freezone 使用的 canonical assets。

    SQLiteStore 负责项目事实数据，PathResolver 负责磁盘路径解析。
    返回里同时保留 `exists` 和 `url`，这样调用方可以区分：
    “这是一个已知资产概念” 和 “这是一个当前可直接引用的文件”。
    """
    ctx, username, project_name, project_dir, _output_dir = (
        await _fz._resolve_freezone_project(project, user, required_role="viewer")
    )
    store = await _fz.make_sqlite_store_for_context(ctx)

    assets: list[dict] = []

    try:
        for character in store.get_all_characters():
            portrait_path = _fz.canonical_portrait_path(project_dir, character.name)
            assets.append(
                _fz._asset_record_from_path(
                    username=username,
                    project=project_name,
                    project_dir=project_dir,
                    project_id=project,
                    tab="characters",
                    kind="portrait",
                    role="character_portrait",
                    label=f"{character.name} / portrait",
                    sublabel=character.name,
                    abs_path=portrait_path,
                    aspect_ratio="1:1",
                    meta={"character": character.name},
                )
            )
            default_voice = _fz._asset_record_from_optional_project_path(
                username=username,
                project=project_name,
                project_dir=project_dir,
                project_id=project,
                tab="characters",
                kind="audio",
                role="character_voice",
                label=f"{character.name} / 默认声线",
                sublabel=character.name,
                stored_path=str(getattr(character, "reference_audio_path", "") or ""),
                meta={
                    "character": character.name,
                    "scope": "character_default",
                    "slot": "default",
                    "age_group": str(getattr(character, "age_group", "") or ""),
                    "sha256": str(
                        getattr(character, "reference_audio_sha256", "") or ""
                    ),
                    "updated_at": str(
                        getattr(character, "reference_audio_updated_at", "") or ""
                    ),
                },
            )
            if default_voice is not None:
                assets.append(default_voice)

            voice_samples = getattr(character, "voice_samples_by_age_group", None) or {}
            if isinstance(voice_samples, dict):
                for slot, slot_label in _fz.FREEZONE_AUDIO_AGE_GROUP_LABELS.items():
                    entry = voice_samples.get(slot)
                    if not isinstance(entry, dict):
                        continue
                    age_voice = _fz._asset_record_from_optional_project_path(
                        username=username,
                        project=project_name,
                        project_dir=project_dir,
                        project_id=project,
                        tab="characters",
                        kind="audio",
                        role="character_age_group_voice",
                        label=f"{character.name} / {slot_label}声线",
                        sublabel=character.name,
                        stored_path=str(entry.get("path", "") or ""),
                        meta={
                            "character": character.name,
                            "scope": "character_age_group",
                            "slot": slot,
                            "age_group": slot,
                            "sha256": str(entry.get("sha256", "") or ""),
                            "updated_at": str(entry.get("updated_at", "") or ""),
                        },
                    )
                    if age_voice is not None:
                        assets.append(age_voice)

            for identity in character.identities or []:
                identity_name = (
                    getattr(identity, "identity_name", "")
                    or getattr(identity, "identity_id", "")
                    or "identity"
                )
                identity_id = (
                    getattr(identity, "identity_id", "")
                    or f"{character.name}_{identity_name}"
                )
                identity_path = _fz.canonical_identity_path(
                    project_dir, character.name, identity_name
                )
                assets.append(
                    _fz._asset_record_from_path(
                        username=username,
                        project=project_name,
                        project_dir=project_dir,
                        project_id=project,
                        tab="characters",
                        kind="identity",
                        role="character_identity",
                        label=f"{character.name} / {identity_name}",
                        sublabel=character.name,
                        abs_path=identity_path,
                        aspect_ratio="1:1",
                        meta={"character": character.name, "identity_id": identity_id},
                    )
                )
                identity_costume_path = _fz.canonical_identity_costume_path(
                    project_dir,
                    character.name,
                    identity_name,
                )
                assets.append(
                    _fz._asset_record_from_path(
                        username=username,
                        project=project_name,
                        project_dir=project_dir,
                        project_id=project,
                        tab="characters",
                        kind="identity_costume",
                        role="identity_costume",
                        label=f"{character.name} / {identity_name} costume",
                        sublabel=character.name,
                        abs_path=identity_costume_path,
                        aspect_ratio="3:4",
                        meta={
                            "character": character.name,
                            "identity_id": identity_id,
                            "identity_name": identity_name,
                        },
                    )
                )
                identity_portrait_path = _fz.canonical_identity_portrait_path(
                    project_dir,
                    character.name,
                    identity_name,
                )
                assets.append(
                    _fz._asset_record_from_path(
                        username=username,
                        project=project_name,
                        project_dir=project_dir,
                        project_id=project,
                        tab="characters",
                        kind="identity_portrait",
                        role="identity_portrait",
                        label=f"{character.name} / {identity_name} portrait",
                        sublabel=character.name,
                        abs_path=identity_portrait_path,
                        aspect_ratio="3:4",
                        meta={
                            "character": character.name,
                            "identity_id": identity_id,
                            "identity_name": identity_name,
                        },
                    )
                )
                identity_voice = _fz._asset_record_from_optional_project_path(
                    username=username,
                    project=project_name,
                    project_dir=project_dir,
                    project_id=project,
                    tab="characters",
                    kind="audio",
                    role="identity_voice",
                    label=f"{character.name} / {identity_name}声线",
                    sublabel=character.name,
                    stored_path=str(
                        getattr(identity, "reference_audio_path", "") or ""
                    ),
                    meta={
                        "character": character.name,
                        "identity_id": identity_id,
                        "identity_name": identity_name,
                        "scope": "identity",
                        "age_group": str(getattr(identity, "age_group", "") or ""),
                        "sha256": str(
                            getattr(identity, "reference_audio_sha256", "") or ""
                        ),
                        "updated_at": str(
                            getattr(identity, "reference_audio_updated_at", "") or ""
                        ),
                    },
                )
                if identity_voice is not None:
                    assets.append(identity_voice)

        for scene in await store.list_scenes():
            scene_name = scene.name
            director_pano_path = None
            stage_manifest_module = None
            try:
                from novelvideo.director_world import stage_manifest

                stage_manifest_module = stage_manifest
                director_pano_path = stage_manifest_module.resolve_pano_path(
                    project_dir, scene_name
                )
            except Exception:
                director_pano_path = None
            for kind, role, label, path, aspect in [
                (
                    "scene",
                    "scene_master",
                    f"{scene_name} / master",
                    _fz.canonical_scene_master_path(project_dir, scene_name),
                    "16:9",
                ),
                (
                    "scene",
                    "scene_reverse_master",
                    f"{scene_name} / reverse master",
                    _fz.canonical_scene_reverse_master_path(project_dir, scene_name),
                    "16:9",
                ),
                (
                    "scene",
                    "scene_director_pano_360",
                    f"{scene_name} / director pano 360",
                    director_pano_path,
                    "2:1",
                ),
            ]:
                if path is None or not _fz._is_freezone_scene_library_role(role):
                    continue
                assets.append(
                    _fz._asset_record_from_path(
                        username=username,
                        project=project_name,
                        project_dir=project_dir,
                        project_id=project,
                        tab="scenes",
                        kind=kind,
                        role=role,
                        label=label,
                        sublabel=scene_name,
                        abs_path=path,
                        aspect_ratio=aspect,
                        meta={
                            "scene": scene_name,
                            "scene_id": scene_name,
                            "scene_type": scene.scene_type,
                        },
                    )
                )
            if stage_manifest_module is not None:
                seen_stage_asset_paths: set[str] = set()
                for ply_kind, role, label in [
                    (
                        "master",
                        "scene_3gs_master_ply",
                        f"{scene_name} / 3D 世界（正面）",
                    ),
                    (
                        "reverse",
                        "scene_3gs_reverse_ply",
                        f"{scene_name} / 3D 世界（背面）",
                    ),
                    ("pano", "scene_3gs_pano_ply", f"{scene_name} / 3D 世界（360）"),
                    (
                        "custom",
                        "scene_3gs_custom_scene",
                        f"{scene_name} / 3D 世界（自定义）",
                    ),
                ]:
                    ply_path = stage_manifest_module.resolve_ply_path(
                        project_dir,
                        scene_name,
                        ply_kind=ply_kind,
                    )
                    if ply_path is None or not _fz._is_freezone_scene_library_role(role):
                        continue
                    rel = ply_path.relative_to(project_dir).as_posix()
                    if rel in seen_stage_asset_paths:
                        continue
                    seen_stage_asset_paths.add(rel)
                    assets.append(
                        _fz._asset_record_from_path(
                            username=username,
                            project=project_name,
                            project_dir=project_dir,
                            project_id=project,
                            tab="scenes",
                            kind="scene",
                            role=role,
                            label=label,
                            sublabel=scene_name,
                            abs_path=ply_path,
                            aspect_ratio="1:1",
                            meta={
                                "scene": scene_name,
                                "scene_id": scene_name,
                                "scene_type": scene.scene_type,
                                "ply_kind": ply_kind,
                            },
                        )
                    )
        for prop in await store.list_props():
            prop_name = prop.name
            assets.append(
                _fz._asset_record_from_path(
                    username=username,
                    project=project_name,
                    project_dir=project_dir,
                    project_id=project,
                    tab="props",
                    kind="prop",
                    role="prop_reference",
                    label=f"{prop_name} / reference",
                    sublabel=prop.prop_type or "object",
                    abs_path=_fz.canonical_prop_reference_path(project_dir, prop_name),
                    aspect_ratio="1:1",
                    meta={"prop_id": prop_name, "prop_type": prop.prop_type},
                )
            )
    finally:
        close = getattr(store, "close", None)
        if close:
            await close()

    return {"ok": True, "data": assets}

@_fz.router.get(
    "/projects/{project}/freezone/assets/beat-context", tags=[_fz.TAG_FREEZONE_ASSETS]
)
async def list_freezone_beat_context_assets(
    project: str,
    episode: _fz.Optional[int] = None,
    beat: _fz.Optional[int] = None,
    user: dict = _fz.Depends(_fz.get_api_user),
):
    """列出 default/project 画布可用的 novelvideo Beat 上下文资产。

    这个接口只聚合 novelvideo canonical 产物，不扫描 `freezone/_uploads`
    或 `freezone/_outputs`。用于 default 画布展示全局 Beat 资源；具体 Beat
    预设画布仍可继续读取 canvas `metadata.references`。
    """
    ctx, username, project_name, project_dir, _output_dir = (
        await _fz._resolve_freezone_project(project, user, required_role="viewer")
    )
    store = await _fz.make_sqlite_store_for_context(ctx)

    requested_episode = int(episode) if episode is not None else None
    requested_beat = int(beat) if beat is not None else None
    if requested_episode is None and requested_beat is not None:
        raise _fz.HTTPException(400, "episode is required when beat is provided")

    flat_assets: list[dict] = []
    episode_groups: list[dict] = []
    try:
        if requested_episode is not None:
            episode_numbers = [requested_episode]
        else:
            episode_numbers = sorted(
                {
                    int(getattr(ep, "number", 0) or 0)
                    for ep in getattr(store, "_episodes", {}).values()
                    if int(getattr(ep, "number", 0) or 0) > 0
                }
            )
            if not episode_numbers:
                try:
                    visual_beats = await store.list_visual_beats()
                except Exception:
                    visual_beats = []
                episode_numbers = sorted(
                    {
                        int(getattr(item, "episode_number", 0) or 0)
                        for item in visual_beats
                        if int(getattr(item, "episode_number", 0) or 0) > 0
                    }
                )

        for ep_num in episode_numbers:
            try:
                beats = await store.get_beats_as_dicts(ep_num)
            except Exception as exc:
                _fz.logger.warning(
                    "failed to load beats for asset context ep%s: %s", ep_num, exc
                )
                beats = []
            beat_numbers = sorted(
                {
                    int(item.get("beat_number") or 0)
                    for item in beats
                    if int(item.get("beat_number") or 0) > 0
                }
            )
            if requested_beat is not None:
                beat_numbers = [num for num in beat_numbers if num == requested_beat]

            beat_groups: list[dict] = []
            for beat_num in beat_numbers:
                try:
                    context = await _fz.build_beat_preset_context(
                        project_id=ctx.project_id,
                        username=username,
                        project=project_name,
                        project_dir=project_dir,
                        store=store,
                        episode=ep_num,
                        beat=beat_num,
                        primary_slot="render",
                    )
                    context = (
                        _fz.migrate_canvas_static_urls_in_memory(
                            context,
                            project_id=ctx.project_id,
                            owner_username=username,
                            project_name=project_name,
                            project_dir=project_dir,
                        )
                        or context
                    )
                except Exception as exc:
                    _fz.logger.warning(
                        "failed to build beat context assets for ep%s beat%s: %s",
                        ep_num,
                        beat_num,
                        exc,
                    )
                    continue

                beat_data = (
                    context.get("beat_data") if isinstance(context, dict) else {}
                )
                refs = context.get("refs") if isinstance(context, dict) else []
                beat_facts = {
                    "visual_description": str(
                        (beat_data or {}).get("visual_description") or ""
                    ),
                    "narration_segment": str(
                        (beat_data or {}).get("narration_segment") or ""
                    ),
                    "scene_id": _fz.beat_scene_id(beat_data or {}),
                    "detected_identities": (beat_data or {}).get("detected_identities")
                    or [],
                    "detected_props": (beat_data or {}).get("detected_props") or [],
                    "sketch_colors": (
                        (context.get("sketch_context") or {}).get("sketch_colors") or {}
                    ),
                    "prop_marker_colors": (
                        (context.get("sketch_context") or {}).get("prop_marker_colors")
                        or {}
                    ),
                }
                assets = [
                    asset
                    for ref in refs
                    if isinstance(ref, dict)
                    for asset in [
                        _fz._beat_context_asset_from_ref(
                            ref=ref,
                            project_id=project,
                            episode=ep_num,
                            beat=beat_num,
                            beat_facts=beat_facts,
                        )
                    ]
                    if asset is not None
                ]
                existing_assets = [
                    asset
                    for asset in assets
                    if asset.get("exists") and asset.get("url")
                ]
                flat_assets.extend(existing_assets)
                beat_groups.append(
                    {
                        "episode": ep_num,
                        "beat": beat_num,
                        "label": f"EP{ep_num} / Beat {beat_num}",
                        "scene_id": beat_facts["scene_id"],
                        "detected_identities": beat_facts["detected_identities"],
                        "detected_props": beat_facts["detected_props"],
                        "sketch_colors": beat_facts["sketch_colors"],
                        "prop_marker_colors": beat_facts["prop_marker_colors"],
                        "visual_description": str(
                            (beat_data or {}).get("visual_description") or ""
                        ),
                        "narration_segment": str(
                            (beat_data or {}).get("narration_segment") or ""
                        ),
                        "assets": assets,
                        "asset_count": len(existing_assets),
                    }
                )

            if beat_groups:
                episode_groups.append({"episode": ep_num, "beats": beat_groups})
    finally:
        close = getattr(store, "close", None)
        if close:
            await close()

    return {
        "ok": True,
        "data": {
            "scope": {
                "episode": requested_episode,
                "beat": requested_beat,
            },
            "episodes": episode_groups,
            "assets": flat_assets,
        },
    }

@_fz.router.post(
    "/projects/{project}/freezone/assets/identities", tags=[_fz.TAG_FREEZONE_ASSETS]
)
async def freezone_create_identity_asset(
    project: str,
    body: _fz.CreateIdentityAssetRequest,
    user: dict = _fz.Depends(_fz.get_api_user),
):
    """从选中的 Freezone 图片创建一个新的角色 identity。

    这个接口故意和 `/freezone/push` 分开：
    `push` 是覆盖已有 canonical slot，
    这里则是新建一个全新的 identity slot，并注册进项目存储。
    """
    ctx, username, _project_name, project_dir, _output_dir = (
        await _fz._resolve_freezone_project(project, user)
    )

    character = body.character.strip()
    identity_name = body.identity_name.strip()
    if not character:
        raise _fz.HTTPException(400, "character is required")
    if not identity_name:
        raise _fz.HTTPException(400, "identity_name is required")

    try:
        source_path = _fz.resolve_static_url_to_path(body.source_url, project_dir)
    except ValueError as exc:
        raise _fz.HTTPException(400, str(exc)) from exc
    if not source_path.exists():
        raise _fz.HTTPException(404, f"source file not found: {source_path}")

    store = await _fz.make_sqlite_store_for_context(ctx)
    try:
        char = store.get_character(character)
        if not char:
            raise _fz.HTTPException(404, f"character not found: {character}")
        identity = _fz.CharacterIdentity(
            identity_id=f"{character}_{identity_name}",
            character_name=character,
            identity_name=identity_name,
            appearance_details=body.appearance_details.strip(),
            face_prompt=body.face_prompt.strip(),
            age_group=body.age_group.strip(),
            source="freezone",
        )
        if any(
            existing.identity_id == identity.identity_id for existing in char.identities
        ):
            raise _fz.HTTPException(409, f"identity already exists: {identity.identity_id}")
        target = _fz.slot_target_path(
            project_dir,
            _fz.IdentityTarget(
                character=character,
                identity_id=identity.identity_id,
            ),
        )
        if target.exists():
            raise _fz.HTTPException(
                409, f"identity image already exists: {identity.identity_id}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            from PIL import Image

            with Image.open(source_path) as img:
                img.convert("RGB").save(target, format="PNG")
        except Exception:
            _fz.shutil.copy2(source_path, target)
        try:
            await store.add_character_identity(character, identity)
        except Exception:
            try:
                target.unlink(missing_ok=True)
            except Exception:
                _fz.logger.warning("failed to rollback copied identity image: %s", target)
            raise
    except _fz.HTTPException:
        raise
    except ValueError as exc:
        raise _fz.HTTPException(409, str(exc)) from exc
    finally:
        close = getattr(store, "close", None)
        if close:
            await close()

    rel = target.relative_to(project_dir).as_posix()
    return {
        "ok": True,
        "data": {
            "character": character,
            "identity_id": identity.identity_id,
            "identity_name": identity.identity_name,
            "target_path": str(target),
            "target_url": _fz.make_static_url_for_context(ctx, rel, local_path=target),
        },
    }

@_fz.router.post("/projects/{project}/freezone/init", tags=[_fz.TAG_FREEZONE_BOOTSTRAP])
async def init_freezone(project: str, user: dict = _fz.Depends(_fz.get_api_user)):
    """懒创建 Freezone 目录树，可重复调用且幂等。"""
    ctx, _username, _project_name, project_dir, _output_dir = (
        await _fz._resolve_freezone_project(project, user)
    )
    canvas_project_dir = _fz._canvas_state_project_dir(ctx, project_dir)
    _fz.freezone_root(project_dir).mkdir(parents=True, exist_ok=True)
    _fz.uploads_dir(project_dir).mkdir(parents=True, exist_ok=True)
    _fz.canvases_dir(canvas_project_dir).mkdir(parents=True, exist_ok=True)
    try:
        default_canvas = _fz.canvas_store.ensure_default_canvas(
            canvas_project_dir,
            project_id=ctx.project_id,
            actor_id=_fz._canvas_actor_id(user),
        )
    except (_fz.canvas_store.CanvasStoreError, _fz.CanvasLockBusy) as exc:
        _fz._raise_canvas_store_http(exc)
    return {
        "ok": True,
        "data": {
            "freezone_dir": str(_fz.freezone_root(project_dir)),
            "default_canvas": {
                "canvas_id": "default",
                "created": default_canvas.created,
                "revision": default_canvas.payload.get("revision"),
            },
        },
    }
