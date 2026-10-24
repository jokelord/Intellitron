from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from backend.config import ReadFileConfig

from .errors import ReadFileError, ReadFileErrorCode


@dataclass
class TemporaryPDF:
    path: Path
    source_format: str
    _temporary_directory: tempfile.TemporaryDirectory[str]

    def close(self) -> None:
        self._temporary_directory.cleanup()

    def __enter__(self) -> "TemporaryPDF":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _build_office_command(
    source: Path, profile: Path, output: Path, executable: str
) -> list[str]:
    extension = source.suffix.lower()
    if extension in {".doc", ".docx"}:
        export_filter = "writer_pdf_Export"
    elif extension in {".ppt", ".pptx"}:
        export_filter = "impress_pdf_Export"
    else:
        raise ReadFileError(
            ReadFileErrorCode.UNSUPPORTED_FORMAT,
            f"LibreOffice Writer/Impress does not accept {extension}",
        )
    profile.mkdir(parents=True, exist_ok=True)
    profile_user = profile / "user"
    profile_user.mkdir(parents=True, exist_ok=True)
    (profile_user / "registrymodifications.xcu").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry">
  <item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurity" oor:op="fuse"><value>3</value></prop></item>
  <item oor:path="/org.openoffice.Office.Common/Load"><prop oor:name="UpdateDocMode" oor:op="fuse"><value>0</value></prop></item>
</oor:items>
""",
        encoding="utf-8",
    )
    output.mkdir(parents=True, exist_ok=True)
    return [
        executable,
        "--headless",
        "--nologo",
        "--nodefault",
        "--nolockcheck",
        "--norestore",
        "--safe-mode",
        f"-env:UserInstallation={profile.resolve().as_uri()}",
        "--convert-to",
        f"pdf:{export_filter}",
        "--outdir",
        str(output),
        str(source),
    ]


def _terminate_owned_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()


def render_office_to_pdf(
    source: str | Path,
    config: ReadFileConfig,
    check_abort: Callable[[], bool] | None,
) -> TemporaryPDF:
    source_path = Path(source).resolve()
    if source_path.suffix.lower() not in {".doc", ".docx", ".ppt", ".pptx"}:
        raise ReadFileError(
            ReadFileErrorCode.UNSUPPORTED_FORMAT,
            "only Writer and Impress conversion is supported",
        )
    if check_abort and check_abort():
        raise ReadFileError(ReadFileErrorCode.ABORTED, "request was cancelled")
    executable = shutil.which("libreoffice")
    if executable is None:
        raise ReadFileError(
            ReadFileErrorCode.OFFICE_UNAVAILABLE,
            "LibreOffice Writer/Impress is not installed",
        )
    temporary = tempfile.TemporaryDirectory(prefix="read-file-office-")
    root = Path(temporary.name)
    home = root / "home"
    scratch = root / "tmp"
    home.mkdir()
    scratch.mkdir()
    command = _build_office_command(source_path, root / "profile", root / "output", executable)
    environment = os.environ.copy()
    environment.update(
        {
            "HOME": str(home),
            "TMPDIR": str(scratch),
            "http_proxy": "http://127.0.0.1:9",
            "https_proxy": "http://127.0.0.1:9",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "",
            "no_proxy": "",
        }
    )
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
        cwd=str(root),
        env=environment,
    )
    deadline = time.monotonic() + config.office_timeout_seconds
    stdout = b""
    stderr = b""
    while process.poll() is None:
        if check_abort and check_abort():
            _terminate_owned_process_group(process)
            temporary.cleanup()
            raise ReadFileError(ReadFileErrorCode.ABORTED, "request was cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_owned_process_group(process)
            temporary.cleanup()
            raise ReadFileError(
                ReadFileErrorCode.OFFICE_CONVERSION_FAILED,
                "LibreOffice conversion timed out",
            )
        try:
            stdout, stderr = process.communicate(timeout=min(0.1, remaining))
        except subprocess.TimeoutExpired:
            continue
    output_path = root / "output" / f"{source_path.stem}.pdf"
    if process.returncode != 0 or not output_path.is_file() or output_path.stat().st_size == 0:
        detail = (stderr or stdout).decode("utf-8", errors="replace")[-300:]
        temporary.cleanup()
        raise ReadFileError(
            ReadFileErrorCode.OFFICE_CONVERSION_FAILED,
            f"LibreOffice did not produce a PDF: {detail}",
        )
    from .pdf_chunker import inspect_pdf

    inspect_pdf(output_path, None, config)
    return TemporaryPDF(output_path, source_path.suffix.lower().lstrip("."), temporary)
