"""Generation instructions separating workflow planning from Recipe execution."""

WORKFLOW_PLANNING_INSTRUCTIONS = (
    "Workflow planning produces topology, Recipe identities, dependencies, confirmed "
    "generation parameters, and short node task briefs, not finished production content. "
    "Write each node prompt as one or two concise sentences stating its task, scope, "
    "required upstream outputs, and references. Preserve user-supplied story facts, "
    "dialogue, source text, and explicit creative requirements; do not replace or "
    "truncate user material. Do not invent complete scripts, detailed shot-by-shot "
    "storyboards, dialogue, camera choreography, sound cues, or final image/video "
    "generation prompts before their upstream production stages have executed. "
    "Runtime Recipe compilation combines the task brief with actual upstream outputs, "
    "the selected Recipe pipeline, confirmed parameters, and reference media to produce "
    "the executable prompt. For example, a video task brief can say: generate the first "
    "shot group from the upstream storyboard using the linked character and scene "
    "anchors. Plan all required stages and independent assets, but do not pretend "
    "their future generated content is already known. When the final result combines "
    "multiple video clips or video with audio, include one terminal videoComposeNode "
    "and connect each contributing video/audio node with composition_input_for. "
    "A single video output without composition does not need a videoComposeNode."
)


def workflow_planning_instructions() -> str:
    """画布工作流规划指令 + 流程包第 06 份（衔接声音与成片验收）。

    画布上出现配音/合成节点时，规划这一步就要带上第 06 份的声音与成片硬规则
    （合成本身是纯音视频编排，没有发往模型的提示词入口，只能沿规划指令送达）。
    注入只是增强，任何异常都退回原始指令。
    """

    try:
        from novelvideo.skills.ai_film_process import pack_block

        pack = pack_block("canvas_workflow_plan")
    except Exception:  # noqa: BLE001 - 注入只是增强，不能影响画布规划
        return WORKFLOW_PLANNING_INSTRUCTIONS
    if not pack:
        return WORKFLOW_PLANNING_INSTRUCTIONS
    return f"{WORKFLOW_PLANNING_INSTRUCTIONS}\n\n{pack}"
