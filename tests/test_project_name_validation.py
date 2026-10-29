"""项目名校验。

项目名同时是目录名（``OUTPUT_DIR/<user>/<name>``）与展示名；URL 用的是项目
id，所以放开到 Unicode 字母是安全的，只要挡住会改变路径语义的字符。
"""

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from novelvideo.api.deps import validate_project_name
from novelvideo.api.schemas import ProjectCreate


@pytest.mark.parametrize("name", ["a", "a" * 64, "project_01"])
def test_schema_accepts_valid_project_name_lengths(name):
    assert ProjectCreate(name=name).name == name


def test_schema_accepts_unicode_project_name():
    assert ProjectCreate(name="我的剧").name == "我的剧"


def test_schema_rejects_65_character_project_name():
    with pytest.raises(ValidationError, match="64"):
        ProjectCreate(name="a" * 65)


def test_shared_validator_rejects_65_character_project_name():
    with pytest.raises(HTTPException) as exc:
        validate_project_name("a" * 65)
    assert exc.value.status_code == 400
    assert exc.value.detail == "Project name must be at most 64 characters long"


def test_shared_validator_rejects_65_multibyte_characters():
    with pytest.raises(HTTPException) as exc:
        validate_project_name("中" * 65)
    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "name",
    [
        "",
        "has space",
        "has-dash",
        "has/slash",
        "has\\backslash",
        "_hidden",
        "has.dot",
        "..",
        "trailing..dot",
        "wild*card",
        "colon:name",
    ],
)
def test_shared_validator_rejects_path_unsafe_names(name):
    """路径分隔符、通配符、控制字符与 ``..`` 一律拒绝。"""
    with pytest.raises(HTTPException):
        validate_project_name(name)


@pytest.mark.parametrize(
    "name",
    ["中文", "我的剧", "剧集2026", "第一季_上", "中" * 64, "abc_123"],
)
def test_shared_validator_accepts_unicode_letters(name):
    """中文（及其它 Unicode 字母）不再是非法项目名。"""
    validate_project_name(name)  # 不抛异常即通过
