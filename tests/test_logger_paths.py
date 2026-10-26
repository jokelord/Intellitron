from __future__ import annotations

from uuid import uuid4

from backend.utils.logger import get_logger


def test_logger_file_path_accepts_home_spelling(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    logger = get_logger(f"home-path-{uuid4()}", log_file="~/app.log", colorize=False)
    try:
        logger.info("home path works")
        file_handlers = [handler for handler in logger.handlers if hasattr(handler, "baseFilename")]
        assert len(file_handlers) == 1
        assert file_handlers[0].baseFilename == str(tmp_path / "app.log")
        file_handlers[0].flush()
        assert "home path works" in (tmp_path / "app.log").read_text(encoding="utf-8")
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()
