"""CLI wrappers for planning and running Git-backed experiments."""

from __future__ import annotations

import argparse
import shlex
import sys
import warnings
from collections.abc import Mapping, Sequence
from pathlib import Path

from runforge.experiment_schema import ExperimentCommand, ExperimentConfiguration, ExperimentStatus
from runforge.git_ops import GitOperationError, GitRepository
from runforge.json_store import load_json_object
from runforge.planner import MatrixPlanRequest, PlanningError, PlanRequest, plan_experiment, plan_matrix
from runforge.retry import RetryError, prepare_retry
from runforge.source_metadata import PinnedGitSource
from runforge.worker import WorkerError, run_experiment


class _SemanticDefaultsHelpFormatter(argparse.ArgumentDefaultsHelpFormatter):
    """Show literal defaults automatically while preserving semantic descriptions."""

    def _get_help_string(self, action: argparse.Action) -> str:
        help_text = action.help or ""
        if action.required or "(default:" in help_text:
            return help_text
        return super()._get_help_string(action)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the RunForge CLI and return its process exit code."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.subcommand == "matrix":
            request = _matrix_request(arguments)
            _print_planning_arguments(
                arguments,
                request.template,
                extra=(
                    ("matrix file", _path_text(arguments.matrix_file)),
                    ("matrix combinations", str(len(request.combinations))),
                ),
            )
            experiments = plan_matrix(request)
            print(f"Experiment plans created ({len(experiments)}):", flush=True)
            for experiment in experiments:
                print(f"  {experiment}", flush=True)
            return 0
        if arguments.subcommand in {"plan", "launch"}:
            request = _planning_request(arguments)
            _print_planning_arguments(arguments, request)
            experiment = _plan_with_warnings(request)
            print(f"Experiment plan created at: {experiment}", flush=True)
            if arguments.subcommand == "launch":
                return run_experiment(experiment, stream_output=arguments.stream_output)
            return 0
        if arguments.subcommand == "retry":
            _print_retry_arguments(arguments)
            preparation = prepare_retry(arguments.experiment, force=arguments.force)
            if preparation.forced:
                print(
                    "warning: Forced retry cannot prove that the previous inprogress worker has stopped",
                    file=sys.stderr,
                    flush=True,
                )
            print(f"Previous attempt archived at: {preparation.archive}", flush=True)
            return run_experiment(preparation.experiment, stream_output=arguments.stream_output)
        _print_run_arguments(arguments)
        return run_experiment(arguments.experiment, stream_output=arguments.stream_output)
    except (PlanningError, RetryError, WorkerError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="runforge",
        description="Plan, inspect, and run reproducible Git-backed experiments.",
        epilog="Use 'runforge SUBCOMMAND --help' for detailed subcommand options.",
        formatter_class=_SemanticDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    plan = subparsers.add_parser(
        "plan",
        help="create one Git-backed experiment directory without execution",
        description="Create one Git-backed experiment directory without executing its command.",
        formatter_class=_SemanticDefaultsHelpFormatter,
    )
    _add_planning_arguments(plan)

    launch = subparsers.add_parser(
        "launch",
        help="create and immediately run one Git-backed experiment",
        description="Create one Git-backed experiment directory and execute it immediately.",
        formatter_class=_SemanticDefaultsHelpFormatter,
    )
    _add_planning_arguments(launch)
    _add_stream_output_argument(launch)

    matrix = subparsers.add_parser(
        "matrix",
        help="create a pinned-source Cartesian experiment matrix",
        description="Create a Cartesian matrix of plans using one required pinned Git source.",
        formatter_class=_SemanticDefaultsHelpFormatter,
    )
    matrix.add_argument(
        "--matrix-file",
        type=Path,
        required=True,
        help="JSON object defining matrix parameters (required)",
    )
    _add_planning_arguments(matrix, pinned_only=True)

    run = subparsers.add_parser(
        "run",
        help="execute one explicit planned experiment directory",
        description="Execute one explicit planned experiment directory.",
        formatter_class=_SemanticDefaultsHelpFormatter,
    )
    _add_stream_output_argument(run)
    run.add_argument("experiment", type=Path, help="planned experiment directory to execute")

    retry = subparsers.add_parser(
        "retry",
        help="archive and rerun one failed or interrupted experiment",
        description=(
            "Archive the previous outputs and immediately rerun one failed experiment. "
            "An inprogress experiment additionally requires --force."
        ),
        formatter_class=_SemanticDefaultsHelpFormatter,
    )
    _add_stream_output_argument(retry)
    retry.add_argument(
        "--force",
        action="store_true",
        help="retry an inprogress experiment after independently confirming its worker stopped (default: disabled)",
    )
    retry.add_argument("experiment", type=Path, help="failed or interrupted experiment directory to retry")
    return parser


def _add_planning_arguments(parser: argparse.ArgumentParser, *, pinned_only: bool = False) -> None:
    parser.add_argument("--name", default="exp", help="short experiment name")
    parser.add_argument(
        "--out-dir",
        type=Path,
        help="root directory for experiment outputs (default: SOURCE_REPOSITORY/reports)",
    )
    parser.add_argument(
        "--source-path",
        type=Path,
        default=Path("."),
        help="path within the source Git repository (default: current directory)",
    )
    if pinned_only:
        parser.set_defaults(source_mode="pinned-git")
    else:
        parser.add_argument(
            "--source-mode",
            choices=("current-head", "pinned-git"),
            default="current-head",
            help="source selection mode",
        )
    commit_requirement = "required" if pinned_only else "default: not set"
    parser.add_argument(
        "--commit",
        required=pinned_only,
        help=f"commit or ref for a pinned Git source ({commit_requirement})",
    )
    parser.add_argument(
        "--patch",
        type=Path,
        help="optional patch for a pinned Git source (default: not set)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="KEY=VALUE environment override file (default: not set)",
    )
    parser.add_argument(
        "--shell",
        action="store_true",
        help="interpret one command string as an explicit shell pipeline (default: disabled)",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER, help="experiment command placed after --")


def _add_stream_output_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--stream-output",
        action="store_true",
        help="stream stdout and stderr while preserving log files (default: disabled)",
    )


def _plan_with_warnings(request: PlanRequest) -> Path:
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        experiment = plan_experiment(request)
    for captured_warning in captured:
        print(f"warning: {captured_warning.message}", file=sys.stderr)
    return experiment


def _matrix_request(arguments: argparse.Namespace) -> MatrixPlanRequest:
    try:
        parameters = load_json_object(arguments.matrix_file)
    except ValueError as error:
        raise PlanningError(str(error)) from error
    return MatrixPlanRequest(template=_planning_request(arguments), parameters=parameters)


def _planning_request(arguments: argparse.Namespace) -> PlanRequest:
    parts = list(arguments.command)
    if parts and parts[0] == "--":
        parts.pop(0)
    if not parts:
        raise PlanningError("No command provided; place it after --")
    if arguments.shell:
        if len(parts) != 1:
            raise PlanningError("--shell requires one quoted command string after --")
        command = ExperimentCommand.shell(parts[0])
    else:
        command = ExperimentCommand.argv(parts)
    return PlanRequest(
        name=arguments.name,
        command=command,
        output_root=arguments.out_dir,
        source_path=arguments.source_path,
        source=_pinned_source(arguments),
        environment=_environment(arguments.env_file),
    )


def _pinned_source(arguments: argparse.Namespace) -> PinnedGitSource | None:
    if arguments.source_mode == "current-head":
        if arguments.commit is not None or arguments.patch is not None:
            raise PlanningError("--commit and --patch require --source-mode pinned-git")
        return None
    if arguments.commit is None:
        raise PlanningError("--source-mode pinned-git requires --commit")
    return PinnedGitSource(
        repository=arguments.source_path,
        commit=arguments.commit,
        patch=arguments.patch,
    )


def _environment(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise PlanningError(f"Could not read environment file {path}: {error}") from error
    environment: dict[str, str] = {}
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise PlanningError(f"Invalid environment entry at {path}:{number}; expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise PlanningError(f"Empty environment key at {path}:{number}")
        environment[key] = value.strip().strip("'").strip('"')
    return environment


def _print_planning_arguments(
    arguments: argparse.Namespace,
    request: PlanRequest,
    *,
    extra: tuple[tuple[str, str], ...] = (),
) -> None:
    rows = [
        ("name", request.name),
        ("output root", _effective_output_root(request)),
        ("source path", _path_text(request.source_path)),
        ("source mode", arguments.source_mode),
        ("commit/ref", request.source.commit if request.source is not None else "not set"),
        ("patch", _optional_path_text(request.source.patch if request.source is not None else None)),
        ("environment file", _optional_path_text(arguments.env_file)),
        ("environment keys", _keys_text(request.environment)),
        ("command mode", request.command.mode),
        ("shell mode", _boolean_text(request.command.mode == "shell")),
        ("command", _command_text(request.command)),
    ]
    if hasattr(arguments, "stream_output"):
        rows.append(("stream output", _boolean_text(arguments.stream_output)))
    rows.extend(extra)
    _print_effective_arguments(arguments.subcommand, rows)


def _print_run_arguments(arguments: argparse.Namespace) -> None:
    experiment = Path(arguments.experiment).expanduser().resolve()
    _print_effective_arguments(
        "run",
        [
            ("experiment", str(experiment)),
            ("stream output", _boolean_text(arguments.stream_output)),
            *_execution_summary_rows(experiment),
        ],
    )


def _print_retry_arguments(arguments: argparse.Namespace) -> None:
    experiment = Path(arguments.experiment).expanduser().resolve()
    status = _status_for_summary(experiment)
    current_state = status.state if status is not None else "not available"
    current_attempt = str(status.attempt) if status is not None else "not available"
    next_attempt = str(status.attempt + 1) if status is not None else "not available"
    _print_effective_arguments(
        "retry",
        [
            ("experiment", str(experiment)),
            ("force", _boolean_text(arguments.force)),
            ("stream output", _boolean_text(arguments.stream_output)),
            ("current state", current_state),
            ("current attempt", current_attempt),
            ("next attempt", next_attempt),
            *_execution_summary_rows(experiment),
        ],
    )


def _print_effective_arguments(subcommand: str, rows: Sequence[tuple[str, str]]) -> None:
    print(f"RunForge {subcommand} effective arguments:", flush=True)
    for label, value in rows:
        print(f"  {label}: {value}", flush=True)


def _configuration_for_summary(experiment: Path) -> ExperimentConfiguration | None:
    try:
        return ExperimentConfiguration.from_dict(load_json_object(experiment / "config.json"))
    except ValueError:
        return None


def _execution_summary_rows(experiment: Path) -> list[tuple[str, str]]:
    configuration = _configuration_for_summary(experiment)
    command = _command_text(configuration.command) if configuration is not None else "not available"
    environment_keys = _keys_text(configuration.environment) if configuration is not None else "not available"
    return [
        ("recorded command", command),
        ("environment keys", environment_keys),
        ("artifact directory", str(experiment / "artifacts")),
        ("stdout log", str(experiment / "stdout.log")),
        ("stderr log", str(experiment / "stderr.log")),
    ]


def _status_for_summary(experiment: Path) -> ExperimentStatus | None:
    try:
        return ExperimentStatus.from_dict(load_json_object(experiment / "status.json"))
    except ValueError:
        return None


def _effective_output_root(request: PlanRequest) -> str:
    if request.output_root is not None:
        return _path_text(request.output_root)
    source_path = request.source.repository if request.source is not None else request.source_path
    try:
        return str(GitRepository.locate(source_path).root / "reports")
    except GitOperationError:
        return "not resolved (SOURCE_REPOSITORY/reports)"


def _command_text(command: ExperimentCommand) -> str:
    if command.mode == "shell":
        return command.script or ""
    return shlex.join(command.arguments)


def _path_text(path: Path) -> str:
    return str(Path(path).expanduser().resolve())


def _optional_path_text(path: Path | None) -> str:
    return _path_text(path) if path is not None else "not set"


def _keys_text(values: Mapping[str, object]) -> str:
    keys = sorted(values)
    return ", ".join(keys) if keys else "none"


def _boolean_text(value: bool) -> str:
    return "enabled" if value else "disabled"
