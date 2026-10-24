from __future__ import annotations

from dataclasses import replace

import pytest

from backend.config import ReadFileConfig
from backend.security.path_policy import PathPolicy, PathPolicyError
from backend.tools.read_file.errors import ReadFileError, ReadFileErrorCode
from backend.tools.read_file.file_validator import validate_source
from backend.tools import search_ops


def test_read_and_image_policy_agree_on_outside_root_containment(tmp_path):
    """Using a search-root union for image/read authority would allow this file."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    policy = PathPolicy(file_operation_roots=[root], image_input_roots=[root])
    config = replace(ReadFileConfig(), allowed_roots=[str(root)])

    with pytest.raises(ReadFileError) as read_error:
        validate_source(outside, config)
    with pytest.raises(PathPolicyError) as image_error:
        policy.validate_image_input(outside)

    assert read_error.value.code is ReadFileErrorCode.PATH_NOT_ALLOWED
    assert image_error.value.code == "path_not_allowed"


def test_search_roots_remain_independent_from_read_file_roots(tmp_path, monkeypatch):
    """Unioning read roots into search would make this search request succeed."""
    read_root = tmp_path / "read"
    read_root.mkdir()
    search_root = tmp_path / "search"
    search_root.mkdir()
    config = type("Config", (), {
        "agent": type("Agent", (), {
            "search_whitelist_paths": [str(search_root)],
            "search_exclude_globs": [],
            "glob_default_limit": 10,
            "glob_max_limit": 10,
            "ripgrep_timeout_seconds": 1,
            "grep_default_head_limit": 10,
            "grep_max_head_limit": 10,
            "grep_max_columns": 200,
        })(),
    })()
    monkeypatch.setattr(search_ops, "get_config", lambda: config)

    result = search_ops.glob_files("*.txt", path=str(read_root))

    assert "不在白名单" in result


def test_read_and_image_reject_escape_symlink_while_search_preserves_listing_behavior(
    tmp_path, monkeypatch
):
    """Changing search to mutation no-follow semantics would hide its established listing result."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = root / "linked.txt"
    link.symlink_to(outside)
    config = replace(ReadFileConfig(), allowed_roots=[str(root)])
    policy = PathPolicy(file_operation_roots=[root], image_input_roots=[root])
    search_config = type("Config", (), {
        "agent": type("Agent", (), {
            "search_whitelist_paths": [str(root)],
            "search_exclude_globs": [],
            "glob_default_limit": 10,
            "glob_max_limit": 10,
            "ripgrep_timeout_seconds": 1,
            "grep_default_head_limit": 10,
            "grep_max_head_limit": 10,
            "grep_max_columns": 200,
        })(),
    })()
    monkeypatch.setattr(search_ops, "get_config", lambda: search_config)

    with pytest.raises(ReadFileError) as read_error:
        validate_source(link, config)
    with pytest.raises(PathPolicyError) as image_error:
        policy.validate_image_input(link)
    listing = search_ops.glob_files("linked.txt", path=str(root))

    assert read_error.value.code is ReadFileErrorCode.PATH_NOT_ALLOWED
    assert image_error.value.code == "symlink_not_allowed"
    assert str(link) in listing
