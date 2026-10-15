from __future__ import annotations

from types import SimpleNamespace

from novelvideo.pipeline_status import character_needs_portrait


def test_main_character_always_needs_a_portrait() -> None:
    character = SimpleNamespace(name="Lead", is_main=True, face_prompt="face", identities=[])
    assert character_needs_portrait(character) is True


def test_non_main_character_without_identities_is_skipped() -> None:
    character = SimpleNamespace(name="Extra", is_main=False, face_prompt="face", identities=[])
    assert character_needs_portrait(character) is False


def test_non_main_character_with_identities_needs_a_portrait() -> None:
    """Same-age identity images are rendered from the character portrait, so an
    inert non-main character that owns identities still requires one."""
    character = SimpleNamespace(
        name="Villager",
        is_main=False,
        face_prompt="face",
        identities=[SimpleNamespace(identity_id="Villager_中年时期")],
    )
    assert character_needs_portrait(character) is True


def test_character_without_face_prompt_never_needs_a_portrait() -> None:
    """A portrait cannot be rendered without a face prompt, so demanding one
    would only wedge the pipeline on an unsatisfiable prerequisite."""
    assert (
        character_needs_portrait(
            SimpleNamespace(
                name="Lead",
                is_main=True,
                face_prompt="   ",
                identities=[SimpleNamespace(identity_id="Lead_中年时期")],
            )
        )
        is False
    )
