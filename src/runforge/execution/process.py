"""Child-process execution and output handling for one experiment.

Separated from worker orchestration because it concerns pipes, buffering, and
console mirroring rather than experiment lifecycle. Nothing here knows what an
experiment is beyond where its logs live.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import BinaryIO, TextIO

from runforge.infrastructure.storage import ExperimentDirectory
from runforge.schemas.experiment import ExperimentConfiguration


class CommandExecutionError(RuntimeError):
    """Raised when the recorded command cannot be started or its output written."""


def run_command(
    experiment: ExperimentDirectory,
    working_directory: Path,
    configuration: ExperimentConfiguration,
    *,
    stream_output: bool,
) -> int:
    environment = os.environ.copy()
    environment.update(configuration.environment)
    environment["RUNFORGE_ARTIFACT_DIR"] = str(experiment.artifacts)
    environment["RUNFORGE_INPUT_DIR"] = str(experiment.inputs)
    paths = [str(working_directory / "src"), str(working_directory)]
    if environment.get("PYTHONPATH"):
        paths.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    command = configuration.command.script if configuration.command.mode == "shell" else configuration.command.arguments
    with experiment.stdout_log.open("wb") as stdout, experiment.stderr_log.open("wb") as stderr:
        try:
            if stream_output:
                process = subprocess.Popen(
                    command,
                    shell=configuration.command.mode == "shell",
                    cwd=working_directory,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                exit_code = _stream_process(process, stdout, stderr)
            else:
                exit_code = subprocess.run(
                    command,
                    shell=configuration.command.mode == "shell",
                    cwd=working_directory,
                    env=environment,
                    stdout=stdout,
                    stderr=stderr,
                    check=False,
                ).returncode
        except OSError as error:
            raise CommandExecutionError(f"Could not start command: {error}") from error
        stdout.flush()
        stderr.flush()
        os.fsync(stdout.fileno())
        os.fsync(stderr.fileno())
    return exit_code


def _stream_process(
    process: subprocess.Popen[bytes],
    stdout_log: BinaryIO,
    stderr_log: BinaryIO,
) -> int:
    assert process.stdout is not None
    assert process.stderr is not None
    errors: list[OSError | ValueError] = []
    # Drain both pipes concurrently so neither child stream can block the other.
    threads = [
        threading.Thread(target=_pump_output, args=(process.stdout, stdout_log, sys.stdout, errors)),
        threading.Thread(target=_pump_output, args=(process.stderr, stderr_log, sys.stderr, errors)),
    ]
    for thread in threads:
        thread.start()
    exit_code = process.wait()
    for thread in threads:
        thread.join()
    if errors:
        raise CommandExecutionError(f"Could not write command output: {errors[0]}")
    return exit_code


def _pump_output(
    source: BinaryIO,
    log: BinaryIO,
    console: TextIO,
    errors: list[OSError | ValueError],
) -> None:
    log_available = True
    console_available = True
    try:
        while chunk := source.read1(8192):
            if log_available:
                try:
                    log.write(chunk)
                    log.flush()
                except (OSError, ValueError) as error:
                    errors.append(error)
                    log_available = False
            if console_available:
                try:
                    _write_console(console, chunk)
                except (OSError, ValueError):
                    console_available = False
    except (OSError, ValueError) as error:
        errors.append(error)
    finally:
        source.close()


def _write_console(console: TextIO, chunk: bytes) -> None:
    buffer = getattr(console, "buffer", None)
    if buffer is not None:
        buffer.write(chunk)
        buffer.flush()
        return
    console.write(chunk.decode("utf-8", errors="replace"))
    console.flush()
