"""The CI workflows and the task runner have to agree (task S2.9).

Both GitHub workflows drive this project by invoking `tasks.py` as a string.
Nothing connects those strings to the argparse definition they depend on, so a
flag renamed in one place fails only in the other - and only on a runner, in a
red build, after a push.

That happened twice in one session. First `tasks.py lint` ran
`sqlfluff lint models` while CI ran `sqlfluff lint models macros`, so three
commits went green locally and red on GitHub against files the local command
never opened. Then the docs workflow called `--dbt-target` for an argument
argparse spells `--target`, and the build failed after successfully doing all
the expensive work before it.

Both are the same class of bug: a check that runs somewhere else, against
something slightly different from what runs here. These tests close it by
reading the workflows and validating what they actually say.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import yaml

import tasks

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def workflow_run_lines() -> list[tuple[str, str]]:
    """Every shell line in every workflow, paired with its file name."""
    lines: list[tuple[str, str]] = []
    for path in WORKFLOWS:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job in (document.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                script = step.get("run")
                if not script:
                    continue
                for line in script.splitlines():
                    if line.strip():
                        lines.append((path.name, line.strip()))
    return lines


def task_invocations() -> list[tuple[str, list[str]]]:
    """Every `tasks.py ...` call a workflow makes, as an argv list."""
    found: list[tuple[str, list[str]]] = []
    for name, line in workflow_run_lines():
        if "tasks.py" not in line:
            continue
        argv = shlex.split(line)
        index = next(i for i, token in enumerate(argv) if token.endswith("tasks.py"))
        found.append((f"{name}: {line}", argv[index + 1 :]))
    return found


def test_the_workflows_actually_invoke_the_task_runner() -> None:
    """Guard against this whole file passing vacuously."""
    assert WORKFLOWS, "no workflow files found"
    assert task_invocations(), "no tasks.py invocations found - has CI stopped using it?"


@pytest.mark.parametrize(
    "label,argv", task_invocations(), ids=[label for label, _ in task_invocations()]
)
def test_every_workflow_invocation_parses(label: str, argv: list[str]) -> None:
    """The exact argv CI passes must be one argparse accepts.

    argparse exits rather than raising on a bad flag, so the failure is a
    SystemExit - which is precisely what the runner saw as exit code 2.
    """
    try:
        tasks.build_parser().parse_args(argv)
    except SystemExit as exit_signal:
        pytest.fail(f"{label}\n  argparse rejected {argv!r} (exit {exit_signal.code})")


def test_local_sql_lint_covers_everything_ci_lints() -> None:
    """A local check narrower than the CI one is worse than no local check.

    It reports green on exactly the files nobody looked at. Comparing the
    directories rather than the whole command keeps this honest without
    pinning the exact invocation.
    """
    ci_paths: set[str] = set()
    for _, line in workflow_run_lines():
        if "sqlfluff" in line and "lint" in line:
            argv = shlex.split(line)
            ci_paths.update(t for t in argv[argv.index("lint") + 1 :] if not t.startswith("-"))

    if not ci_paths:
        pytest.skip("no sqlfluff invocation in the workflows")

    source = (ROOT / "tasks.py").read_text(encoding="utf-8")
    local = set(re.findall(r'"(models|macros|snapshots|seeds|tests)"', source))

    missing = ci_paths - local
    assert not missing, (
        f"CI lints {sorted(ci_paths)} but tasks.py lint does not cover {sorted(missing)} - "
        "local runs will pass on files CI rejects"
    )
