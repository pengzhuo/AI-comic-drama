"""Block of helpers split out of ``novelvideo/chat/service.py``.

Global references are rewritten to ``_svc.<name>`` so monkeypatching the
source module still takes effect; the source re-imports these names.
"""

from __future__ import annotations

import os

import novelvideo.chat.service as _svc


def _freezone_canvas_write_requested(prompt: str | None) -> bool:
    """Legacy intent hint; never use this to enforce canvas receipt postconditions."""

    raw_prompt = str(prompt or "")
    user_text = raw_prompt.split("[SUPERTALE_", 1)[0].strip()
    if not user_text:
        return False
    has_action = bool(_svc._FREEZONE_CANVAS_WRITE_ACTION_RE.search(user_text))
    has_canvas_object = bool(_svc._FREEZONE_CANVAS_WRITE_OBJECT_RE.search(user_text))
    has_direct_media_write = bool(_svc._FREEZONE_DIRECT_MEDIA_WRITE_RE.search(user_text))
    has_node_reference = "[SUPERTALE_CANVAS_NODE_REFERENCES]" in raw_prompt
    standalone_clear = bool(_svc.re.search(r"(?:清空|clear)", user_text, _svc.re.IGNORECASE))
    if _svc._FREEZONE_CANVAS_KNOWLEDGE_QUESTION_RE.search(user_text):
        return False
    # Skill Studio authors catalog configuration. Media words inside a Skill
    # description (for example, “创建图片转线稿 Skill”) do not authorize or
    # require a canvas mutation. Keep the canvas receipt guard only when the
    # same request explicitly asks to use/run the Skill, add it to canvas, or
    # perform another independent canvas mutation.
    if _svc._FREEZONE_SKILL_STUDIO_TRIGGER_RE.search(user_text):
        runtime_text = _svc._FREEZONE_SKILL_RUNTIME_NEGATION_RE.sub("", user_text)
        intent_text = _svc._FREEZONE_SKILL_CAPABILITY_RE.sub("", runtime_text)
        return bool(
            _svc._FREEZONE_SKILL_RUNTIME_REQUEST_RE.search(intent_text)
            or _svc._FREEZONE_INDEPENDENT_CANVAS_WRITE_RE.search(intent_text)
        )
    # A text artifact request such as “生成一个视频脚本” or “create an image
    # prompt” must remain a chat response unless the user explicitly names a
    # canvas/node mutation. Otherwise the post-turn adapter may replace the
    # generated text with a misleading canvas-write failure.
    # Only suppress an explicit text-artifact request.  Media requests may
    # legitimately contain the same words (for example “根据这个提示词生成
    # 一张图” or “生成一张带文案的图片”), so do not use a broad keyword
    # exclusion here.
    if (
        _svc._FREEZONE_TEXT_ONLY_REQUEST_RE.search(user_text)
        and not _svc.re.search(
            r"(?:根据|用|按照|基于|带|包含|from|using|based\s+on|with)",
            user_text,
            _svc.re.IGNORECASE,
        )
        and not _svc.re.search(
            r"(?:节点|画布|连线|node|canvas|edge)", user_text, _svc.re.IGNORECASE
        )
    ):
        return False
    return has_action and (
        has_canvas_object
        or has_direct_media_write
        or has_node_reference
        or standalone_clear
    )

async def _bind_server_observed_agent_product_execution(
    event: _svc.Any,
    *,
    project_dir: str | _svc.Path | None,
    project_state_dir: str | _svc.Path | None,
) -> None:
    """Bind an admitted product operation to the tool call carrying its result."""
    tool_name = _svc._codex_freezone_tool_name(event)
    if tool_name not in _svc._AGENT_PRODUCT_RESULT_TOOLS:
        return
    event_type = str(getattr(event, "type", "") or "tool_updated")
    status = str(getattr(event, "status", "") or "").strip().lower()
    if getattr(event, "error", None):
        return
    if event_type != "tool_started":
        if status not in {"completed", "success", "succeeded"}:
            return
        succeeded = any(
            payload.get("ok") is True
            for value in (
                getattr(event, "structured", None),
                getattr(event, "output", None),
            )
            for payload in _svc._json_objects_from_codex_tool_value(value)
        )
        if not succeeded:
            return

    tool_args: dict[str, _svc.Any] = {}
    for payload in _svc._json_objects_from_codex_tool_value(getattr(event, "input", None)):
        tool_args = payload
        break
    turn_id = str(getattr(event, "turn_id", "") or "").strip()
    tool_call_id = str(getattr(event, "call_id", "") or "").strip()
    state_root = project_state_dir or project_dir
    if not tool_args or not turn_id or not tool_call_id or state_root is None:
        return
    state_dir = _svc.Path(state_root)
    from novelvideo.freezone.agent_product_operations import (
        bind_agent_product_model_execution,
        read_agent_generation_session,
    )

    operation_ids: set[str] = set()
    if tool_name in {
        "freezone_prepare_workflow",
        "freezone_prepare_workflow_draft",
        "freezone_prepare_workflow_plan_draft",
    }:
        operation_id = str(tool_args.get("operation_id") or "").strip()
        if operation_id.startswith("agent_product_"):
            operation_ids.add(operation_id)
    else:
        session_id = str(tool_args.get("skill_studio_session_id") or "").strip()
        if not session_id:
            return
        session = await _svc.asyncio.to_thread(
            read_agent_generation_session,
            project_dir=state_dir,
            generation_session_id=session_id,
        )
        draft = session.get("draft") if isinstance(session, dict) else None
        operations = draft.get("operations") if isinstance(draft, dict) else None
        if not isinstance(operations, dict):
            return
        if tool_name == "freezone_put_agent_catalog_skill":
            operation = operations.get("skill")
        else:
            recipe_operations = operations.get("recipes")
            if not isinstance(recipe_operations, dict):
                return
            index = tool_args.get("index")
            operation = recipe_operations.get(index) or recipe_operations.get(
                str(index)
            )
            if not isinstance(operation, dict):
                recipe = tool_args.get("recipe")
                recipe_id = (
                    str(recipe.get("id") or "").strip()
                    if isinstance(recipe, dict)
                    else ""
                )
                matches = [
                    candidate
                    for candidate in recipe_operations.values()
                    if isinstance(candidate, dict)
                    and str(candidate.get("artifact_id") or "").strip() == recipe_id
                ]
                operation = matches[0] if len(matches) == 1 else None
        if isinstance(operation, dict):
            operation_id = str(operation.get("operation_id") or "").strip()
            if operation_id.startswith("agent_product_"):
                operation_ids.add(operation_id)
    if not operation_ids:
        return

    model_call_id = f"agent-turn:{turn_id}:tool:{tool_call_id}"
    for operation_id in sorted(operation_ids):
        try:
            await _svc.asyncio.to_thread(
                bind_agent_product_model_execution,
                project_dir=state_dir,
                operation_id=operation_id,
                model_call_id=model_call_id,
                executed_at=_svc.datetime.now(tz=_svc.timezone.utc).timestamp(),
                source="server_observed_agent_turn",
                turn_id=turn_id,
                tool_call_id=tool_call_id,
            )
        except ValueError:
            from novelvideo.chat import evidence_metrics

            evidence_metrics.observe("agent_product_binding_failed")
            _svc.logger.warning(
                "could not bind observed Agent product execution operation=%s",
                operation_id,
                exc_info=True,
            )

def _freezone_skill_studio_requested(prompt: str | None) -> bool:
    text = str(prompt or "").strip()
    if not text:
        return False
    return bool(_svc._FREEZONE_SKILL_STUDIO_TRIGGER_RE.search(text))

def _freezone_agent_catalog_summary(username: str, *, limit: int = 40) -> str:
    try:
        from novelvideo.freezone.agent_config_store import list_user_agent_config_items
    except Exception:
        return "catalog_summary_unavailable"

    lines: list[str] = []
    for kind in ("skills", "recipes"):
        try:
            items = list_user_agent_config_items(username, kind)
        except Exception:
            lines.append(f"{kind}: unavailable")
            continue
        visible = [
            item
            for item in items
            if item.get("enabled") is not False and item.get("hidden") is not True
        ]
        lines.append(f"{kind}:")
        if not visible:
            lines.append("- none")
            continue
        for item in visible[:limit]:
            source = str(item.get("_catalog_source") or "user")
            if item.get("_catalog_base_source") == "builtin":
                source = "customized"
            if kind == "skills":
                triggers = (
                    item.get("triggers")
                    if isinstance(item.get("triggers"), dict)
                    else {}
                )
                keywords = (
                    triggers.get("keywords") if isinstance(triggers, dict) else []
                )
                keyword_text = (
                    ", ".join(str(value) for value in keywords[:6])
                    if isinstance(keywords, list)
                    else ""
                )
                lines.append(
                    "- "
                    f"id={item.get('id')}; source={source}; category={item.get('category')}; "
                    f"description={item.get('description')}; keywords={keyword_text}"
                )
            else:
                action_keys = item.get("action_keys")
                action_text = (
                    ", ".join(str(value) for value in action_keys[:6])
                    if isinstance(action_keys, list)
                    else ""
                )
                lines.append(
                    "- "
                    f"id={item.get('id')}; source={source}; name={item.get('name')}; "
                    f"output_kind={item.get('output_kind')}; action_keys={action_text}"
                )
        if len(visible) > limit:
            lines.append(f"- ... {len(visible) - limit} more")
    return "\n".join(lines)

def _freezone_skill_studio_context(username: str, prompt: str | None) -> str:
    if not _svc._freezone_skill_studio_requested(prompt):
        return ""
    return (
        f"\n\n{_svc._FREEZONE_SKILL_STUDIO_INSTRUCTIONS}\n\n"
        "[FREEZONE_AGENT_CATALOG_SUMMARY]\n"
        f"{_svc._freezone_agent_catalog_summary(username)}\n"
        "[/FREEZONE_AGENT_CATALOG_SUMMARY]"
    )

def _write_hermes_tool_mode(username: str, *, mode: str) -> None:
    try:
        from novelvideo.chat.hermes_workspace import ensure_user_hermes_workspace

        home = ensure_user_hermes_workspace(
            username,
            profile="freezone" if mode == "freezone_canvas" else "director",
        )
        path = home / "tmp" / "dramaclaw_tool_mode.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _svc.json.dumps({"mode": mode}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001 - tool mode is defense-in-depth.
        _svc.logger.warning(
            "failed to write hermes tool mode for user=%s mode=%s: %s",
            username,
            mode,
            exc,
        )

def _route_prompt_with_execution_context(
    prompt: str,
    route_prompt: str | None,
) -> tuple[str, str]:
    """Separate visible user intent from transport-only execution context.

    The UI appends canvas ontology, attachment analysis, node references, and
    command context after the visible text. Hermes still needs that context,
    but BrainClaw must classify and embed only the visible intent following the
    final ``[USER_MESSAGE]`` marker.

    Only split the prompt when the clean route prompt is an exact leading text
    segment followed by a line boundary. Older callers and transformed prompts
    retain the previous behavior instead of risking lost user content.
    """

    transport_prompt = str(prompt or "")
    clean_route_prompt = str(route_prompt or "").strip()
    if not clean_route_prompt:
        return transport_prompt, ""
    if transport_prompt == clean_route_prompt:
        return clean_route_prompt, ""
    if transport_prompt.startswith(clean_route_prompt):
        suffix = transport_prompt[len(clean_route_prompt) :]
        if suffix.startswith(("\n", "\r")):
            return clean_route_prompt, suffix.strip()
    return transport_prompt, ""

def _prompt_with_user_context(
    username: str,
    project: str,
    prompt: str,
    *,
    tool_mode: str = "default",
    surface_context: dict[str, _svc.Any] | None = None,
    route_prompt: str | None = None,
    turn_id: str | None = None,
    require_generation_parameter_preflight: bool = False,
) -> str:
    user_message, execution_context = _svc._route_prompt_with_execution_context(
        prompt,
        route_prompt,
    )
    scope = f"project:{project}" if project else "home"
    canvas_id = _svc._freezone_canvas_id_from_context(surface_context)
    canvas_execution_mode = _svc._freezone_canvas_execution_mode_from_context(
        surface_context
    )
    generation_parameter_round = str(turn_id or "current_request").strip()
    if require_generation_parameter_preflight:
        generation_parameter_policy = (
            f"generation_parameter_round: {generation_parameter_round}\n"
            "For every new request in this round that will actually generate image or video media, "
            "show one preliminary structured generation-parameter clarification before any canvas "
            "write in both manual_confirm and auto_execute. Historical clarification answers, "
            "existing node data, Recipe defaults, and parameters from an earlier turn may only "
            "prefill recommended choices; they never count as confirmation for this round. Do not "
            "ask again after the clarification tool returns answers for this same round. This card "
            "covers image/video parameters only; never add a system-voice/custom-voice choice and "
            "do not ask the user to choose system voice versus custom voice.\n"
            "manual_confirm: After the preliminary parameter answers return, put them into the plan "
            "and submit the protected write. The normal approval card is still shown and remains the "
            "final parameter editor before execution.\n"
            "auto_execute: After the preliminary parameter answers return, submit the protected canvas "
            "write immediately without asking for another create/run confirmation. A normal approval "
            "event is still emitted and the frontend auto-applies it; explicit human-review requirements "
            "may pause. Use the MCP clarification tool, never built-in request_user_input.\n"
        )
    else:
        generation_parameter_policy = (
            "manual_confirm: Do not ask a preliminary image/video parameter clarification. Read the "
            "live schema and put supported defaults or symbolic recommended values into the plan; the "
            "approval card is where the user reviews and adjusts final generation parameters.\n"
            "auto_execute: If image/video parameters needed for generation are missing, ask once before "
            "the canvas write, with one structured question per missing field. A normal approval event "
            "is still emitted and the frontend auto-applies it; explicit human-review requirements may "
            "pause. After the answers return, submit the protected canvas write immediately without "
            "asking for another create/run confirmation. Use the MCP clarification tool, never built-in "
            "request_user_input.\n"
        )
    canvas_context = (
        "\n\n[FREEZONE_CANVAS_CONTEXT]\n"
        f"canvas_id: {canvas_id}\n"
        "Use this canvas_id for Freezone canvas tools unless the user explicitly names another canvas.\n"
        "[/FREEZONE_CANVAS_CONTEXT]\n\n"
        "[FREEZONE_CANVAS_EXECUTION_MODE]\n"
        f"mode: {canvas_execution_mode}\n"
        f"{generation_parameter_policy}"
        "If the mode is absent or invalid, use manual_confirm. The mode changes parameter collection "
        "only; it does not bypass validation, approval events, or the authorized canvas write path.\n"
        "[/FREEZONE_CANVAS_EXECUTION_MODE]"
        if tool_mode == "freezone_canvas"
        else ""
    )
    surface_instructions = (
        f"\n\n{_svc._FREEZONE_CANVAS_ASSISTANT_INSTRUCTIONS}"
        f"{_svc._freezone_skill_studio_context(username, route_prompt if route_prompt is not None else prompt)}"
        f"{canvas_context}"
        if tool_mode == "freezone_canvas"
        else ""
    )
    continuation_source = route_prompt if route_prompt is not None else prompt
    continuation_instructions = _svc._pipeline_continuation_instructions(
        continuation_source,
        tool_mode=tool_mode,
    )
    rendering_instructions = (
        ""
        if tool_mode == "freezone_canvas"
        else f"{_svc._JSON_RENDER_CHAT_INSTRUCTIONS}\n\n"
    )
    execution_context_block = (
        "[DRAMACLAW_EXECUTION_CONTEXT]\n"
        f"{execution_context}\n"
        "[/DRAMACLAW_EXECUTION_CONTEXT]\n\n"
        if execution_context
        else ""
    )
    return (
        "[DRAMACLAW_USER_CONTEXT]\n"
        f"username: {username}\n"
        f"scope: {scope}\n"
        "Project-scoped facts and learned preferences must stay in the project scope.\n\n"
        f"{rendering_instructions}"
        f"{continuation_instructions}"
        f"{surface_instructions}\n\n"
        f"{execution_context_block}"
        "[USER_MESSAGE]\n"
        f"{user_message}"
    )

def _pipeline_continuation_instructions(prompt: str, *, tool_mode: str) -> str:
    """Return a narrow execution hint for explicit mainline continuation commands."""
    if tool_mode != "default":
        return ""
    text = str(prompt or "").strip()
    if not text or len(text) > 80:
        return ""
    if not _svc._EXPLICIT_PIPELINE_CONTINUATION_RE.search(text):
        return ""
    if _svc._PIPELINE_CONTINUATION_QUESTION_RE.search(text):
        return ""
    return f"{_svc._DRAMACLAW_CONTINUATION_INSTRUCTIONS}\n\n"

def _chat_backend() -> str:
    preferred = (
        _svc.os.environ.get("DRAMACLAW_CHAT_BACKEND")
        or _svc.os.environ.get("SUPERTALE_CHAT_BACKEND")
        or "codex"
    ).strip().lower() or "codex"
    if preferred == "hermes":
        # Explicit "hermes" must succeed — do NOT silently fall back to
        # claude/codex. A missing hermes binary is a config error to surface.
        if _svc.is_hermes_backend_available():
            return "hermes"
        raise RuntimeError(
            "DRAMACLAW_CHAT_BACKEND=hermes requested but hermes is unavailable. "
            "Run `uv tool install 'hermes-agent[acp]'`, "
            "then run `hermes doctor` to diagnose."
        )
    if preferred == "codex":
        if _svc.is_codex_backend_available():
            return "codex"
        raise RuntimeError(
            "DRAMACLAW_CHAT_BACKEND=codex requested but Codex is unavailable. "
            "Install `openai-codex`/Codex Python SDK support in the backend environment "
            "and ensure CODEX_BIN points to a valid codex binary."
        )
    if preferred == "claude":
        if _svc.is_claude_backend_available():
            return "claude"
        raise RuntimeError(
            "DRAMACLAW_CHAT_BACKEND=claude requested but Claude is unavailable. "
            "Install claude-agent-sdk and ensure CLAUDE_CLI_PATH points to a valid claude binary."
        )
    if _svc.is_codex_backend_available():
        return "codex"
    if _svc.is_claude_backend_available():
        return "claude"
    return preferred

def _claude_cli_path() -> _svc.Path:
    configured = _svc.os.environ.get("CLAUDE_CLI_PATH", "").strip()
    if configured:
        return _svc.Path(configured).expanduser()
    resolved = _svc.shutil.which("claude")
    if resolved:
        return _svc.Path(resolved)
    return _svc.Path.home() / ".local" / "bin" / "claude"

def _codex_bin_path() -> _svc.Path | None:
    configured = _svc.os.environ.get("CODEX_BIN", "").strip()
    if configured:
        return _svc.Path(configured).expanduser()
    return None

def _codex_model() -> str:
    from novelvideo.shared.runtime_env import is_ce_effective

    if is_ce_effective():
        from novelvideo.model_gateway_settings import get_effective_agent_llm_config

        # CE is configured interactively and SQLite is authoritative. Only the
        # explicit Custom + BrainClaw choice sends the literal ``brainclaw``
        # model; Official and Hybrid send DramaClaw's logical Codex alias and
        # let RelayClaw decide what serves it, and Advanced mode sends the same
        # alias to the user-selected NewAPI gateway.
        from novelvideo.model_gateway_settings import (
            AGENT_LLM_MODE_THIRD_PARTY,
            CUSTOM_LLM_MODE_RELAYCLAW_BRAINCLAW,
        )

        gateway = get_effective_agent_llm_config()
        if gateway.mode == AGENT_LLM_MODE_THIRD_PARTY:
            return gateway.model or _svc._DEFAULT_CODEX_MODEL
        if gateway.mode == CUSTOM_LLM_MODE_RELAYCLAW_BRAINCLAW:
            return "brainclaw"
        return _svc._DEFAULT_CODEX_MODEL

    # EE/SaaS is deployment-configured. The gateway address and logical model
    # come from env, while an organization channel's key is authorized and
    # injected separately for each turn.
    # ``os.environ`` is read directly here so the CE env-config ratchet can see
    # the keys (a ``_svc.os`` alias is invisible to its AST scan).
    return (
        os.environ.get("CODEX_MODEL", _svc._DEFAULT_CODEX_MODEL).strip()
        or _svc._DEFAULT_CODEX_MODEL
    )

def _codex_reasoning_effort() -> str:
    value = (
        os.environ.get(
            "CODEX_REASONING_EFFORT", _svc._DEFAULT_CODEX_REASONING_EFFORT
        ).strip()
        or _svc._DEFAULT_CODEX_REASONING_EFFORT
    ).lower()
    if value not in _svc._CODEX_REASONING_EFFORT_VALUES:
        supported = ", ".join(sorted(_svc._CODEX_REASONING_EFFORT_VALUES))
        raise RuntimeError(
            f"Unsupported CODEX_REASONING_EFFORT={value!r}; expected one of: {supported}"
        )
    return value

def _claude_model() -> str | None:
    model = _svc.os.environ.get("CLAUDE_MODEL", "").strip()
    return model or None

def _claude_sdk_available() -> bool:
    return _svc.importlib.util.find_spec("claude_agent_sdk") is not None

def is_claude_backend_available() -> bool:
    return _svc._claude_cli_path().exists() and _svc._claude_sdk_available()

def is_codex_backend_available() -> bool:
    codex_bin = _svc._codex_bin_path()
    # Per-turn gateway credentials travel in App Server metadata. The SDK's
    # bundled 0.147 runtime logs that metadata verbatim, so only the explicitly
    # configured, DramaClaw-patched runtime is safe to start.
    return (
        codex_bin is not None
        and codex_bin.exists()
        and _svc.importlib.util.find_spec("openai_codex") is not None
    )

def is_hermes_backend_available() -> bool:
    """Lazy import so chat_service can be loaded without hermes deps."""
    try:
        from novelvideo.chat.hermes_pool import is_hermes_backend_available as _check
    except ImportError:
        return False
    return _check()

def is_chat_backend_available() -> bool:
    # NOTE: _chat_backend() raises when DRAMACLAW_CHAT_BACKEND=hermes is
    # requested but unavailable; catch so this probe stays non-throwing.
    try:
        backend = _svc._chat_backend()
    except RuntimeError:
        return False
    if backend == "claude":
        return _svc.is_claude_backend_available()
    if backend == "codex":
        return _svc.is_codex_backend_available()
    if backend == "hermes":
        return _svc.is_hermes_backend_available()
    return False

def get_chat_backend_name() -> str:
    return _svc._chat_backend()

def _repo_skill_roots() -> list[_svc.Path]:
    root = _svc._repo_root()
    return [
        root / "src" / "novelvideo" / "agent_skills",
        root / ".claude" / "skills",
        root / ".codex" / "skills",
    ]

def _skill_sources() -> list[tuple[str, _svc.Path]]:
    sources: dict[str, _svc.Path] = {}
    for repo_skills_root in _svc._repo_skill_roots():
        if not repo_skills_root.exists():
            continue
        for child in sorted(repo_skills_root.iterdir()):
            if child.is_dir() and (child / "SKILL.md").exists():
                # Keep the first matching skill name. Public agent skills are
                # preferred, followed by optional host-specific overlays.
                sources.setdefault(child.name, child)

    configured = (
        _svc.os.environ.get("CLAUDE_DRAMACLAW_SKILL_PATH")
        or _svc.os.environ.get("CLAUDE_SUPERTALE_SKILL_PATH")
        or ""
    ).strip()
    if configured:
        sources["dramaclaw"] = _svc.Path(configured).expanduser()

    return [(name, path) for name, path in sorted(sources.items()) if path.exists()]

def _sync_project_skills(skills_dir: _svc.Path, *, agent_profile: str = "main") -> None:
    skills_dir.mkdir(parents=True, exist_ok=True)
    # Multiple Codex turns can initialize the same project/profile at once.
    # Hold one cross-process lock across the digest, replacement and manifest
    # write so neither turn sees or removes the other's half-published Skill.
    lock_path = skills_dir / ".dramaclaw-managed-skills.lock"
    with _svc.portalocker.Lock(str(lock_path), timeout=30):
        _svc._sync_project_skills_locked(skills_dir, agent_profile=agent_profile)

def _sync_project_skills_locked(skills_dir: _svc.Path, *, agent_profile: str) -> None:
    profile = str(agent_profile or "main").strip() or "main"
    allowed = (
        {"freezone", "workflows", "dramaclaw-workflows"}
        if profile.startswith("freezone")
        else None
    )
    manifest_path = skills_dir / ".dramaclaw-managed-skills.json"
    previous_managed: set[str] = set()
    try:
        payload = _svc.json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("skills"), dict):
            previous_managed = {
                name
                for name in payload["skills"]
                if isinstance(name, str) and _svc._is_safe_managed_skill_name(name)
            }
    except (OSError, TypeError, ValueError, _svc.json.JSONDecodeError):
        pass

    sources = {
        name: src for name, src in _svc._skill_sources() if _svc._is_safe_managed_skill_name(name)
    }
    managed_names = previous_managed | set(sources)
    active: dict[str, str] = {}
    for skill_name in sorted(managed_names):
        dst = _svc._managed_skill_destination(skills_dir, skill_name)
        if dst is None:
            continue
        src = sources.get(skill_name)
        if src is None or (allowed is not None and skill_name not in allowed):
            _svc._remove_managed_skill_path(dst, root=skills_dir)
            continue
        source_digest = _svc._skill_tree_digest(src)
        destination_digest = _svc._skill_tree_digest(dst) if dst.is_dir() else ""
        if source_digest != destination_digest:
            if not _svc._remove_managed_skill_path(dst, root=skills_dir):
                continue
            _svc.shutil.copytree(src, dst)
        active[skill_name] = source_digest

    manifest_path.write_text(
        _svc.json.dumps(
            {"schema_version": 1, "profile": profile, "skills": active},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

def _is_safe_managed_skill_name(name: object) -> bool:
    if not isinstance(name, str) or not name or name != name.strip():
        return False
    candidate = _svc.Path(name)
    return (
        not candidate.is_absolute()
        and candidate.name == name
        and name not in {".", ".."}
        and "/" not in name
        and "\\" not in name
    )

def _managed_skill_destination(skills_dir: _svc.Path, skill_name: str) -> _svc.Path | None:
    if not _svc._is_safe_managed_skill_name(skill_name):
        return None
    root = skills_dir.resolve()
    destination = skills_dir / skill_name
    try:
        resolved = destination.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved == root:
        return None
    return destination

def _remove_managed_skill_path(path: _svc.Path, *, root: _svc.Path) -> bool:
    root = root.resolve()
    try:
        resolved = path.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return False
    if resolved == root:
        return False
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        _svc.shutil.rmtree(path)
    return True

def _skill_tree_digest(root: _svc.Path) -> str:
    if not root.is_dir():
        return ""
    digest = _svc.hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()

def _now_iso() -> str:
    return _svc.datetime.now(_svc.timezone.utc).isoformat()

def _project_dir(username: str, project: str) -> _svc.Path:
    base_dir = _svc._output_root() / username / project
    for path in (
        base_dir,
        base_dir / "graph",
        base_dir / "assets",
        base_dir / "assets" / "characters",
        base_dir / "scripts",
        base_dir / "images",
        base_dir / "audio",
        base_dir / "videos",
        base_dir / "uploads",
    ):
        path.mkdir(parents=True, exist_ok=True)
    return base_dir

def _project_state_dir(username: str, project: str) -> _svc.Path:
    base_dir = _svc._state_root() / username / project
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir

def _user_state_dir(username: str) -> _svc.Path:
    base_dir = _svc._state_root() / username
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir

def _user_agent_workspace(username: str) -> _svc.Path:
    workspace = _svc._user_state_dir(username) / ".chat_agents"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace

def _user_chat_agent_locks_dir(username: str) -> _svc.Path:
    base_dir = _svc._user_state_dir(username) / "chat_agent_locks"
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir

def _legacy_chat_db_path(
    username: str,
    project: str,
    project_dir: str | _svc.Path | None = None,
) -> _svc.Path:
    base_dir = (
        _svc.Path(project_dir)
        if project_dir is not None
        else _svc._project_dir(username, project)
    )
    return base_dir / ".chat" / "chat.db"

def _migrate_legacy_chat_db(
    username: str,
    project: str,
    new_db_path: _svc.Path,
    project_dir: str | _svc.Path | None = None,
    *,
    create_parent: bool = True,
) -> None:
    _svc.message_repository.migrate_legacy_chat_db(
        _svc._legacy_chat_db_path(username, project, project_dir),
        new_db_path,
        create_parent=create_parent,
    )

def _chat_db_path(
    username: str,
    project: str,
    project_dir: str | _svc.Path | None = None,
    project_state_dir: str | _svc.Path | None = None,
) -> _svc.Path:
    if project_state_dir is not None:
        db_path = _svc.Path(project_state_dir) / "chat.db"
        _svc._migrate_legacy_chat_db(
            username,
            project,
            db_path,
            project_dir,
            create_parent=True,
        )
        return db_path
    db_path = _svc._project_state_dir(username, project) / "chat.db"
    _svc._migrate_legacy_chat_db(username, project, db_path, project_dir, create_parent=True)
    return db_path

def _chat_input_history_path(username: str, project: str) -> _svc.Path:
    return _svc._project_state_dir(username, project) / "chat_input_history.json"

def _connect(db_path: _svc.Path) -> _svc.sqlite3.Connection:
    return _svc.message_repository.connect(db_path)

def load_chat_input_history(username: str, project: str) -> list[str]:
    if not username or not project:
        return []
    return _svc.message_repository.load_chat_input_history(
        _svc._chat_input_history_path(username, project)
    )

def save_chat_input_history(
    username: str, project: str, history: list[str], *, limit: int = 200
) -> None:
    if not username or not project:
        return
    _svc.message_repository.save_chat_input_history(
        _svc._chat_input_history_path(username, project), history, limit=limit
    )

def _get_setting(conn: _svc.sqlite3.Connection, key: str) -> str | None:
    return _svc.message_repository.get_setting(conn, key)

def _set_setting(conn: _svc.sqlite3.Connection, key: str, value: str) -> None:
    _svc.message_repository.set_setting(conn, key, value, now_iso=_svc._now_iso)

def _pid_is_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        _svc.os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True

def _chat_run_lock_path(username: str, project: str) -> _svc.Path:
    return _svc.session_registry.chat_run_lock_path(
        _svc._user_chat_agent_locks_dir(username), project
    )

def _chat_run_lock_payload(lock_id: str, *, started_at: str | None = None) -> str:
    return _svc.session_registry.chat_run_lock_payload(lock_id, started_at=started_at)

def _chat_run_lock_file_is_new(path: _svc.Path) -> bool:
    return _svc.session_registry.chat_run_lock_file_is_new(path)

def _chat_run_lock_owner_is_active(
    owner_id: str | None,
    owner_pid: int | None,
    started_at: _svc.datetime | None,
    updated_at: _svc.datetime | None,
) -> bool:
    return _svc.session_registry.chat_run_lock_owner_is_active(
        owner_id, owner_pid, started_at, updated_at, pid_is_alive=_svc._pid_is_alive
    )

def _acquire_chat_run_lock(username: str, project: str) -> str:
    return _svc.session_registry.acquire_chat_run_lock(
        _svc._chat_run_lock_path(username, project),
        owner_is_active=_svc._chat_run_lock_owner_is_active,
    )

def _release_chat_run_lock(username: str, project: str, lock_id: str) -> None:
    _svc.session_registry.release_chat_run_lock(
        _svc._chat_run_lock_path(username, project), lock_id
    )

def _heartbeat_chat_run_lock(username: str, project: str, lock_id: str) -> bool:
    return _svc.session_registry.heartbeat_chat_run_lock(
        _svc._chat_run_lock_path(username, project),
        lock_id,
        atomic_write=_svc._atomic_write_chat_run_lock_file,
    )

def chat_run_lock_is_active(username: str, project: str = "") -> bool:
    return _svc.session_registry.chat_run_lock_is_active(
        _svc._chat_run_lock_path(username, project),
        owner_is_active=_svc._chat_run_lock_owner_is_active,
    )

def force_release_chat_run_lock(username: str, project: str) -> None:
    _svc._remove_chat_run_lock_file(_svc._chat_run_lock_path(username, project))

def _turn_operation_finalizer(authorization: _svc.Any | None) -> _svc.Any | None:
    """Own the egress claim for this turn, if there is one.

    Placed here rather than on the worker slot because this is the only layer
    that sees a whole business turn: the slot outlives it and the streaming loop
    is re-entered by both retry paths.
    """
    claim = getattr(authorization, "claim", None)
    if claim is None:
        return None
    from novelvideo.chat.hermes_operation import TurnOperationFinalizer
    from novelvideo.ports import get_egress_operation_port

    return TurnOperationFinalizer(get_egress_operation_port(), claim)

def _turn_disposition_for(event: _svc.Any) -> str:
    """Classify how this turn ended.

    ``complete`` is also synthesised for a timeout, so the event type cannot
    settle the ledger on its own.
    """
    from novelvideo.chat.hermes_operation import disposition_for

    return disposition_for(event)

def _evidence_identity(
    project: str | None, store_scope: _svc.Any | None, agent_profile: str
) -> dict[str, str]:
    """Name the trajectory and project this turn belongs to.

    ``project_group_id`` is the DramaClaw project, or the home sentinel when
    there is none — BrainClaw refuses to invent a grouping it cannot see, so the
    caller must say "no project" explicitly rather than omit it.

    ``trajectory_id`` is the most specific conversation scope available within
    the project: a Freezone canvas-and-Agent profile when there is one,
    otherwise the project-and-profile conversation. Project is part of both
    names so a reused canvas ID cannot merge evidence families across projects;
    the profile keeps the identity correct if multi-session UI is enabled again.
    Within that boundary this deliberately over-groups — every turn of one long
    conversation lands in one family — because over-grouping only costs
    statistical power, while under-grouping manufactures independence that
    does not exist.
    """
    from novelvideo.chat.hermes_egress import HOME_SCOPE_EGRESS_PROJECT_ID

    project_id = (project or "").strip() or HOME_SCOPE_EGRESS_PROJECT_ID
    canvas_id = str(getattr(store_scope, "canvas_id", "") or "").strip()
    trajectory_id = (
        f"canvas:{project_id}:{canvas_id}:{agent_profile}"
        if canvas_id
        else f"conversation:{project_id}:{agent_profile}"
    )
    return {"trajectory_id": trajectory_id, "project_id": project_id}

async def _chat_run_lock_heartbeat_loop(
    username: str, project: str, lock_id: str
) -> None:
    while True:
        await _svc.asyncio.sleep(_svc._CHAT_RUN_LOCK_HEARTBEAT_SECONDS)
        if not _svc._heartbeat_chat_run_lock(username, project, lock_id):
            return

def _append_message(
    conn: _svc.sqlite3.Connection,
    role: str,
    content: str,
    media: list[dict[str, _svc.Any]] | None = None,
) -> dict[str, _svc.Any]:
    return _svc.message_repository.append_message(
        conn, role, content, media, now_iso=_svc._now_iso
    )
