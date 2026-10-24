from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import subprocess


def _make_agent_config(tmp_path: Path) -> SimpleNamespace:
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        agent=SimpleNamespace(
            search_whitelist_paths=[str(allowed_root), "/tmp"],
            search_exclude_globs=[".git", "__pycache__"],
            glob_default_limit=3,
            glob_max_limit=10,
            ripgrep_timeout_seconds=1,
            grep_default_head_limit=2,
            grep_max_head_limit=10,
            grep_max_columns=400,
        )
    )


class TestGlobFiles:
    def test_run_ripgrep_uses_supervised_argv_and_minimal_environment(self, monkeypatch):
        from backend.tools import search_ops

        captured = {}

        class FakeSupervisor:
            def __init__(self, **kwargs):
                captured["init"] = kwargs

            def run_argv(self, argv, **kwargs):
                captured["argv"] = argv
                captured["kwargs"] = kwargs
                return SimpleNamespace(
                    stdout="",
                    stderr="",
                    returncode=1,
                    timed_out=False,
                    aborted=False,
                )

        monkeypatch.setattr(search_ops, "ProcessSupervisor", FakeSupervisor, raising=False)
        monkeypatch.setattr(search_ops.shutil, "which", lambda _: "/usr/bin/rg")

        result = search_ops._run_ripgrep(["rg", "--files"], timeout_seconds=1)

        assert result.returncode == 1
        assert captured["argv"][0] == "/usr/bin/rg"
        assert captured["kwargs"]["timeout"] == 1.0
        assert set(captured["kwargs"]["env"]) <= {"PATH", "LANG", "LC_ALL"}

    def test_glob_files_matches_within_whitelist(self, tmp_path: Path):
        cfg = _make_agent_config(tmp_path)
        allowed_root = Path(cfg.agent.search_whitelist_paths[0])
        pkg = allowed_root / "pkg"
        pkg.mkdir(parents=True)
        (pkg / "a.py").write_text("print('a')\n", encoding="utf-8")
        (pkg / "b.py").write_text("print('b')\n", encoding="utf-8")
        (pkg / "note.txt").write_text("hello\n", encoding="utf-8")

        with patch("backend.tools.search_ops.get_config", return_value=cfg):
            from backend.tools.search_ops import glob_files

            result = glob_files(pattern="**/*.py", path=str(allowed_root), limit=5)

        assert "Glob search summary" in result
        assert str(pkg / "a.py") in result
        assert str(pkg / "b.py") in result
        assert str(pkg / "note.txt") not in result

    def test_glob_files_rejects_path_outside_whitelist(self, tmp_path: Path):
        cfg = _make_agent_config(tmp_path)
        cfg.agent.search_whitelist_paths = [str(Path(cfg.agent.search_whitelist_paths[0]))]
        outside = tmp_path / "outside"
        outside.mkdir(parents=True)

        with patch("backend.tools.search_ops.get_config", return_value=cfg):
            from backend.tools.search_ops import glob_files

            result = glob_files(pattern="**/*.py", path=str(outside), limit=5)

        assert "不在白名单" in result or "whitelist" in result.lower()

    def test_glob_files_truncates_results(self, tmp_path: Path):
        cfg = _make_agent_config(tmp_path)
        allowed_root = Path(cfg.agent.search_whitelist_paths[0])
        data_dir = allowed_root / "data"
        data_dir.mkdir(parents=True)
        for idx in range(5):
            (data_dir / f"f{idx}.py").write_text(f"print({idx})\n", encoding="utf-8")

        with patch("backend.tools.search_ops.get_config", return_value=cfg):
            from backend.tools.search_ops import glob_files

            result = glob_files(pattern="**/*.py", path=str(allowed_root), limit=2)

        assert "showing: 2" in result
        assert "truncated" in result.lower()

    def test_glob_files_accepts_absolute_pattern_under_whitelist_root(self, tmp_path: Path):
        cfg = _make_agent_config(tmp_path)
        allowed_root = Path(cfg.agent.search_whitelist_paths[0])
        nested = allowed_root / "nested"
        nested.mkdir(parents=True)
        target = nested / "stop-marker.txt"
        target.write_text("stop\n", encoding="utf-8")

        with patch("backend.tools.search_ops.get_config", return_value=cfg):
            from backend.tools.search_ops import glob_files

            result = glob_files(pattern=f"{allowed_root}/**/*stop*", limit=10)

        assert str(target) in result

    def test_glob_files_falls_back_when_ripgrep_hits_permission_error(self, tmp_path: Path):
        cfg = _make_agent_config(tmp_path)
        allowed_root = Path(cfg.agent.search_whitelist_paths[0])
        nested = allowed_root / "nested"
        nested.mkdir(parents=True)
        target = nested / "stop-marker.txt"
        target.write_text("stop\n", encoding="utf-8")

        with patch("backend.tools.search_ops.get_config", return_value=cfg), patch(
            "backend.tools.search_ops._has_ripgrep", return_value=True
        ), patch(
            "backend.tools.search_ops._run_ripgrep",
            return_value=subprocess.CompletedProcess(
                args=["rg"], returncode=2, stdout="", stderr="Permission denied"
            ),
        ):
            from backend.tools.search_ops import glob_files

            result = glob_files(pattern="**/*stop*", path=str(allowed_root), limit=10)

        assert str(target) in result

    def test_glob_files_matches_symlinked_file_within_whitelist(self, tmp_path: Path):
        cfg = _make_agent_config(tmp_path)
        allowed_root = Path(cfg.agent.search_whitelist_paths[0])
        source_dir = tmp_path / "source"
        source_dir.mkdir(parents=True)
        source = source_dir / "NH4NO3.md"
        source.write_text("ammonium nitrate\n", encoding="utf-8")
        link = allowed_root / "NH4NO3.md"
        link.symlink_to(source)

        with patch("backend.tools.search_ops.get_config", return_value=cfg):
            from backend.tools.search_ops import glob_files

            result = glob_files(pattern="**/NH4NO3*", limit=10)

            assert str(link) in result

    def test_grep_fallback_does_not_read_symlinked_file_outside_whitelist(
        self, tmp_path: Path
    ):
        cfg = _make_agent_config(tmp_path)
        allowed_root = Path(cfg.agent.search_whitelist_paths[0])
        outside = tmp_path / "outside"
        outside.mkdir(parents=True)
        external = outside / "secret.txt"
        external.write_text("outside-secret", encoding="utf-8")
        link = allowed_root / "linked.txt"
        link.symlink_to(external)

        with patch("backend.tools.search_ops.get_config", return_value=cfg), patch(
            "backend.tools.search_ops._has_ripgrep", return_value=False
        ):
            from backend.tools.search_ops import grep_search

            result = grep_search(
                pattern="outside-secret",
                path=str(allowed_root),
                output_mode="content",
            )

        assert str(link) not in result
        assert ":1:outside-secret" not in result

    def test_glob_files_keeps_successful_rg_results_when_later_root_hits_permission_error(
        self, tmp_path: Path
    ):
        cfg = _make_agent_config(tmp_path)
        allowed_root = Path(cfg.agent.search_whitelist_paths[0])
        target = allowed_root / "NH4NO3.md"
        target.write_text("ammonium nitrate\n", encoding="utf-8")

        rg_results = iter(
            [
                subprocess.CompletedProcess(
                    args=["rg"], returncode=0, stdout="NH4NO3.md\n", stderr=""
                ),
                subprocess.CompletedProcess(
                    args=["rg"],
                    returncode=2,
                    stdout="",
                    stderr="Permission denied",
                ),
            ]
        )

        with patch("backend.tools.search_ops.get_config", return_value=cfg), patch(
            "backend.tools.search_ops._has_ripgrep", return_value=True
        ), patch(
            "backend.tools.search_ops._run_ripgrep", side_effect=lambda *args, **kwargs: next(rg_results)
        ):
            from backend.tools.search_ops import glob_files

            result = glob_files(pattern="**/NH4NO3*", limit=10)

        assert str(target) in result

    def test_glob_files_keeps_rg_stdout_when_returncode_is_2_and_stderr_is_empty(
        self, tmp_path: Path
    ):
        cfg = _make_agent_config(tmp_path)
        allowed_root = Path(cfg.agent.search_whitelist_paths[0])
        target = allowed_root / "NH4NO3.md"
        target.write_text("ammonium nitrate\n", encoding="utf-8")

        with patch("backend.tools.search_ops.get_config", return_value=cfg), patch(
            "backend.tools.search_ops._has_ripgrep", return_value=True
        ), patch(
            "backend.tools.search_ops._run_ripgrep",
            return_value=subprocess.CompletedProcess(
                args=["rg"], returncode=2, stdout="NH4NO3.md\n", stderr=""
            ),
        ), patch(
            "backend.tools.search_ops._glob_with_python"
        ) as python_fallback:
            python_fallback.return_value = ([], None)
            from backend.tools.search_ops import glob_files

            result = glob_files(pattern="**/NH4NO3*", path=str(allowed_root), limit=10)

        python_fallback.assert_not_called()
        assert str(target) in result

    def test_glob_files_python_fallback_matches_top_level_with_double_star_prefix(
        self, tmp_path: Path
    ):
        cfg = _make_agent_config(tmp_path)
        allowed_root = Path(cfg.agent.search_whitelist_paths[0])
        target = allowed_root / "NH4NO3.md"
        target.write_text("ammonium nitrate\n", encoding="utf-8")

        with patch("backend.tools.search_ops.get_config", return_value=cfg), patch(
            "backend.tools.search_ops._has_ripgrep", return_value=False
        ):
            from backend.tools.search_ops import glob_files

            result = glob_files(pattern="**/NH4NO3*", path=str(allowed_root), limit=10)

        assert str(target) in result

    def test_glob_files_summary_lists_searched_root_paths(self, tmp_path: Path):
        cfg = _make_agent_config(tmp_path)
        allowed_root = Path(cfg.agent.search_whitelist_paths[0])
        cfg.agent.search_whitelist_paths = [str(allowed_root)]

        with patch("backend.tools.search_ops.get_config", return_value=cfg):
            from backend.tools.search_ops import glob_files

            result = glob_files(pattern="**/missing-file.py", limit=10)

        assert "- searched root paths:" in result
        assert f"  - {allowed_root}" in result

    def test_glob_files_suggests_keyword_matches_for_segment_prefix_miss(
        self, tmp_path: Path
    ):
        cfg = _make_agent_config(tmp_path)
        allowed_root = Path(cfg.agent.search_whitelist_paths[0])
        target = allowed_root / "42《反物质飞船》作者：[美] 杰克·威廉森.txt"
        target.write_text("matter\n", encoding="utf-8")

        with patch("backend.tools.search_ops.get_config", return_value=cfg):
            from backend.tools.search_ops import glob_files

            result = glob_files(pattern="**/反物质*", path=str(allowed_root), limit=10)

        assert "- matched files: 0" in result
        assert "Possible filename keyword matches" in result
        assert "**/*反物质*" in result
        assert str(target) in result


class TestGrepSearch:
    def test_grep_files_with_matches_mode(self, tmp_path: Path):
        cfg = _make_agent_config(tmp_path)
        allowed_root = Path(cfg.agent.search_whitelist_paths[0])
        src = allowed_root / "src"
        src.mkdir(parents=True)
        (src / "one.py").write_text("class AgentRunner:\n    pass\n", encoding="utf-8")
        (src / "two.py").write_text("no match\n", encoding="utf-8")
        (src / "three.py").write_text("class AgentRunner:\n    pass\n", encoding="utf-8")

        with patch("backend.tools.search_ops.get_config", return_value=cfg):
            from backend.tools.search_ops import grep_search

            result = grep_search(
                pattern=r"class\s+AgentRunner",
                path=str(allowed_root),
                include="*.py",
                output_mode="files_with_matches",
            )

        assert "Grep search summary" in result
        assert str(src / "one.py") in result
        assert str(src / "three.py") in result
        assert str(src / "two.py") not in result

    def test_grep_content_mode_returns_matching_lines(self, tmp_path: Path):
        cfg = _make_agent_config(tmp_path)
        allowed_root = Path(cfg.agent.search_whitelist_paths[0])
        src = allowed_root / "src"
        src.mkdir(parents=True)
        target = src / "sample.py"
        target.write_text(
            "first line\nclass AgentRunner:\n    pass\nlast line\n",
            encoding="utf-8",
        )

        with patch("backend.tools.search_ops.get_config", return_value=cfg):
            from backend.tools.search_ops import grep_search

            result = grep_search(
                pattern=r"class\s+AgentRunner",
                path=str(allowed_root),
                include="*.py",
                output_mode="content",
                line_numbers=True,
            )

        assert "Content:" in result
        assert str(target) in result
        assert "2:" in result or ":2:" in result
        assert "class AgentRunner:" in result

    def test_grep_count_mode_reports_counts(self, tmp_path: Path):
        cfg = _make_agent_config(tmp_path)
        allowed_root = Path(cfg.agent.search_whitelist_paths[0])
        src = allowed_root / "src"
        src.mkdir(parents=True)
        a = src / "a.py"
        b = src / "b.py"
        a.write_text("final_answer\nfinal_answer\n", encoding="utf-8")
        b.write_text("final_answer\n", encoding="utf-8")

        with patch("backend.tools.search_ops.get_config", return_value=cfg):
            from backend.tools.search_ops import grep_search

            result = grep_search(
                pattern="final_answer",
                path=str(allowed_root),
                include="*.py",
                output_mode="count",
            )

        assert "Counts:" in result
        assert str(a) in result
        assert str(b) in result

    def test_grep_supports_offset_and_head_limit(self, tmp_path: Path):
        cfg = _make_agent_config(tmp_path)
        allowed_root = Path(cfg.agent.search_whitelist_paths[0])
        src = allowed_root / "src"
        src.mkdir(parents=True)
        for idx in range(4):
            (src / f"f{idx}.py").write_text("needle\n", encoding="utf-8")

        with patch("backend.tools.search_ops.get_config", return_value=cfg):
            from backend.tools.search_ops import grep_search

            result = grep_search(
                pattern="needle",
                path=str(allowed_root),
                include="*.py",
                output_mode="files_with_matches",
                head_limit=2,
                offset=1,
            )

        assert "showing files: 2" in result
        assert "offset: 1" in result

    def test_grep_rejects_outside_whitelist(self, tmp_path: Path):
        cfg = _make_agent_config(tmp_path)
        cfg.agent.search_whitelist_paths = [str(Path(cfg.agent.search_whitelist_paths[0]))]
        outside = tmp_path / "outside"
        outside.mkdir(parents=True)
        (outside / "x.py").write_text("needle\n", encoding="utf-8")

        with patch("backend.tools.search_ops.get_config", return_value=cfg):
            from backend.tools.search_ops import grep_search

            result = grep_search(
                pattern="needle",
                path=str(outside),
                include="*.py",
                output_mode="files_with_matches",
            )

        assert "不在白名单" in result or "whitelist" in result.lower()

    def test_grep_falls_back_when_ripgrep_hits_permission_error(self, tmp_path: Path):
        cfg = _make_agent_config(tmp_path)
        allowed_root = Path(cfg.agent.search_whitelist_paths[0])
        src = allowed_root / "src"
        src.mkdir(parents=True)
        target = src / "sample.py"
        target.write_text("needle\n", encoding="utf-8")

        with patch("backend.tools.search_ops.get_config", return_value=cfg), patch(
            "backend.tools.search_ops._has_ripgrep", return_value=True
        ), patch(
            "backend.tools.search_ops._run_ripgrep",
            return_value=subprocess.CompletedProcess(
                args=["rg"], returncode=2, stdout="", stderr="Permission denied"
            ),
        ):
            from backend.tools.search_ops import grep_search

            result = grep_search(
                pattern="needle",
                path=str(allowed_root),
                include="*.py",
                output_mode="files_with_matches",
            )

        assert str(target) in result

    def test_grep_falls_back_when_ripgrep_returns_exit_2_with_empty_stderr(
        self, tmp_path: Path
    ):
        cfg = _make_agent_config(tmp_path)
        allowed_root = Path(cfg.agent.search_whitelist_paths[0])
        src = allowed_root / "src"
        src.mkdir(parents=True)
        target = src / "sample.py"
        target.write_text("stop\n", encoding="utf-8")

        with patch("backend.tools.search_ops.get_config", return_value=cfg), patch(
            "backend.tools.search_ops._has_ripgrep", return_value=True
        ), patch(
            "backend.tools.search_ops._run_ripgrep",
            return_value=subprocess.CompletedProcess(
                args=["rg"], returncode=2, stdout="", stderr=""
            ),
        ):
            from backend.tools.search_ops import grep_search

            result = grep_search(
                pattern="stop",
                path=str(allowed_root),
                output_mode="files_with_matches",
            )

        assert str(target) in result

    def test_grep_rejects_unknown_output_mode_alias(self, tmp_path: Path):
        cfg = _make_agent_config(tmp_path)
        allowed_root = Path(cfg.agent.search_whitelist_paths[0])
        target = allowed_root / "sample.md"
        target.write_text("NH4NO3\n", encoding="utf-8")

        with patch("backend.tools.search_ops.get_config", return_value=cfg):
            from backend.tools.search_ops import grep_search

            result = grep_search(
                pattern="NH4NO3",
                path=str(allowed_root),
                output_mode="files",
            )

        assert str(target) in result

    def test_grep_reports_ripgrep_timeout_without_python_fallback(self, tmp_path: Path):
        cfg = _make_agent_config(tmp_path)
        allowed_root = Path(cfg.agent.search_whitelist_paths[0])
        target = allowed_root / "sample.md"
        target.write_text("NH4NO3\n", encoding="utf-8")

        with patch("backend.tools.search_ops.get_config", return_value=cfg), patch(
            "backend.tools.search_ops._has_ripgrep", return_value=True
        ), patch(
            "backend.tools.search_ops._run_ripgrep",
            side_effect=subprocess.TimeoutExpired(cmd=["rg"], timeout=1),
        ), patch(
            "backend.tools.search_ops._discover_matches_with_python"
        ) as python_fallback:
            from backend.tools.search_ops import grep_search

            result = grep_search(
                pattern="NH4NO3",
                path=str(allowed_root),
                output_mode="files_with_matches",
            )

        python_fallback.assert_not_called()
        assert "ripgrep 搜索超时" in result

    def test_grep_keeps_rg_stdout_when_returncode_is_2_and_stderr_is_empty(
        self, tmp_path: Path
    ):
        cfg = _make_agent_config(tmp_path)
        allowed_root = Path(cfg.agent.search_whitelist_paths[0])
        target = allowed_root / "sample.md"
        target.write_text("NH4NO3\n", encoding="utf-8")

        with patch("backend.tools.search_ops.get_config", return_value=cfg), patch(
            "backend.tools.search_ops._has_ripgrep", return_value=True
        ), patch(
            "backend.tools.search_ops._run_ripgrep",
            return_value=subprocess.CompletedProcess(
                args=["rg"], returncode=2, stdout="sample.md\n", stderr=""
            ),
        ), patch(
            "backend.tools.search_ops._discover_matches_with_python"
        ) as python_fallback:
            python_fallback.return_value = ([], None)
            from backend.tools.search_ops import grep_search

            result = grep_search(
                pattern="NH4NO3",
                path=str(allowed_root),
                output_mode="files_with_matches",
            )

        python_fallback.assert_not_called()
        assert str(target) in result

    def test_grep_summary_lists_searched_root_paths(self, tmp_path: Path):
        cfg = _make_agent_config(tmp_path)
        allowed_root = Path(cfg.agent.search_whitelist_paths[0])
        cfg.agent.search_whitelist_paths = [str(allowed_root)]

        with patch("backend.tools.search_ops.get_config", return_value=cfg):
            from backend.tools.search_ops import grep_search

            result = grep_search(
                pattern="missing_needle",
                output_mode="files_with_matches",
                head_limit=10,
            )

        assert "- searched root paths:" in result
        assert f"  - {allowed_root}" in result
