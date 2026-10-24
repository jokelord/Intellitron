from pathlib import Path


def _resolve_shared_workspace_root(script_dir: Path) -> Path:
    git_dir = (script_dir / ".." / ".git").resolve()
    if git_dir.is_file():
        content = git_dir.read_text(encoding="utf-8").strip()
        prefix = "gitdir: "
        if content.startswith(prefix):
            common_dir = (script_dir / ".." / content[len(prefix):]).resolve()
            if common_dir.name == ".git":
                return common_dir.parent
            if common_dir.parent.name == ".git":
                git_root = common_dir.parent.parent
                if git_root.name == ".git":
                    return git_root.parent
                return git_root
            if common_dir.parent.parent.name == ".git":
                return common_dir.parent.parent.parent
    return (script_dir / "..").resolve()


def test_resolve_shared_workspace_root_from_worktree_git_file(tmp_path: Path):
    repo_root = tmp_path / "repo"
    worktree_root = repo_root / ".worktrees" / "multi-agent-backend"
    script_dir = worktree_root / "scripts"
    script_dir.mkdir(parents=True)
    common_dir = repo_root / ".git" / "worktrees" / "multi-agent-backend"
    common_dir.mkdir(parents=True)
    (worktree_root / ".git").write_text(f"gitdir: {common_dir}\n", encoding="utf-8")

    resolved = _resolve_shared_workspace_root(script_dir)

    assert resolved == repo_root


def test_resolve_shared_workspace_root_from_regular_repo_checkout(tmp_path: Path):
    repo_root = tmp_path / "repo"
    script_dir = repo_root / "scripts"
    git_dir = repo_root / ".git"
    script_dir.mkdir(parents=True)
    git_dir.mkdir()

    resolved = _resolve_shared_workspace_root(script_dir)

    assert resolved == repo_root


def test_workspace_path_helper_script_contains_worktree_resolution_logic():
    helper = (Path(__file__).resolve().parents[1] / "scripts" / "_workspace_paths.sh").read_text(encoding="utf-8")

    assert "get_shared_workspace_root" in helper
    assert ".git/worktrees" not in helper or "gitdir:" in helper
    assert "get_shared_sglang_activate" in helper
