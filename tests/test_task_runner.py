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
        argv = argv[index + 1 :]
        # A pipeline's later commands are not this one's arguments. warehouse.yml
        # tees its reports into the build artifact - `tasks.py published | tee
        # reports/...` - and without this the extractor handed argparse the pipe
        # and the filename, then reported the workflow as broken when it was the
        # test that could not read a shell.
        for terminator in ("|", ">", ">>", "&&", ";"):
            if terminator in argv:
                argv = argv[: argv.index(terminator)]
        found.append((f"{name}: {line}", argv))
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


# ==================================================== artifact names
# GitHub rejects these in an artifact name. The list is short and the failure is
# late: the job runs to completion first and is refused only at upload.
INVALID_IN_ARTIFACT_NAME = set('":<>|*?\r\n\/')


def _matrix_values(job: dict) -> dict[str, list[str]]:
    matrix = job.get("strategy", {}).get("matrix", {})
    return {
        key: [str(v) for v in values]
        for key, values in matrix.items()
        if isinstance(values, list) and key not in ("include", "exclude")
    }


def _expanded_names(job: dict) -> list[str]:
    """Every artifact name this job could upload, with matrix values filled in."""
    values = _matrix_values(job)
    names: list[str] = []
    for step in job.get("steps", []):
        if "upload-artifact" not in str(step.get("uses", "")):
            continue
        template = str(step.get("with", {}).get("name", ""))
        if not template:
            continue
        candidates = [template]
        for key, options in values.items():
            token = re.compile(r"\$\{\{\s*matrix\." + re.escape(key) + r"\s*\}\}")
            candidates = [token.sub(option, c) for c in candidates for option in options]
        names.extend(candidates)
    return names


@pytest.mark.parametrize("path", WORKFLOWS, ids=[p.name for p in WORKFLOWS])
def test_no_workflow_can_build_an_invalid_artifact_name(path: Path) -> None:
    """Matrix values reach artifact names, and some characters are refused there.

    sweep.yml's settings were written `disposal_cost:0` so the job labels read
    well, and the same string was interpolated into the artifact name. All
    eighty jobs simulated for sixteen minutes and were then rejected at upload,
    because a colon is not allowed - and GitHub expressions have no `replace()`
    to sanitise it after the fact, so the separator itself had to change.

    Checked by expanding the matrix rather than by eyeballing the template: the
    template contains no invalid character, only the values it interpolates do.
    """
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    for job_name, job in (workflow.get("jobs") or {}).items():
        for name in _expanded_names(job):
            bad = sorted(INVALID_IN_ARTIFACT_NAME & set(name))
            assert not bad, f"{path.name}:{job_name} can upload {name!r}, invalid: {bad}"


def _module_targets() -> list[tuple[str, str]]:
    """Every `py("-m", "<module>")` in tasks.py, paired with the function around it.

    Read off the syntax tree rather than by importing and calling, because
    calling them would run the pipeline. The AST sees exactly what the runner
    will hand to `python -m` and nothing else.
    """
    import ast

    source = (ROOT / "tasks.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    found: list[tuple[str, str]] = []
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "py"):
                continue
            literals = [a.value for a in node.args if isinstance(a, ast.Constant)]
            if "-m" in literals:
                index = literals.index("-m")
                if index + 1 < len(literals):
                    found.append((function.name, literals[index + 1]))
    return found


def test_the_task_runner_actually_runs_modules() -> None:
    """Guards the extractor above, which is otherwise the thing that can go quiet."""
    targets = _module_targets()
    assert len(targets) > 15, (
        f"only found {len(targets)} `py('-m', ...)` calls in tasks.py, which means the "
        "AST walk stopped matching rather than that the runner stopped running modules"
    )


@pytest.mark.parametrize(
    ("function", "module"), _module_targets(), ids=lambda v: v if isinstance(v, str) else str(v)
)
def test_every_task_names_a_module_that_exists(function: str, module: str) -> None:
    """A target that names a module nobody wrote fails only when somebody runs it.

    `t_recommend` called `analytics.optimization.run_all` for two sprints. The
    module was never written, and the `not_yet` guard in front of it tested for
    `markdown.py` - which has existed since S4.2 - so the guard stopped firing
    while the thing it guarded stayed missing. `tasks.py all` and
    `tasks.py recommend` both died with ModuleNotFoundError, and neither is on
    any workflow, so nothing caught it until the Definition of Done asked
    whether a clean clone reproduces the project.

    `find_spec` rather than `import_module`: this resolves the name without
    executing the module, so a target cannot be verified by running the
    pipeline it triggers.
    """
    from importlib.util import find_spec

    assert find_spec(module) is not None, (
        f"tasks.py::{function} runs `python -m {module}`, and that module does not exist. "
        "The target fails for anyone who invokes it."
    )


def test_recommend_runs_every_engine_in_dependency_order(monkeypatch) -> None:
    """The order is a dependency, and nothing in CI exercises this function.

    `warehouse.yml` spells the same six engines out as separate steps, so a
    reordering here would not show up in any build - expiry risk scores the
    batches the others reason about, and elasticity has to be fitted before the
    markdown optimiser can read a coefficient. Running them for real takes
    minutes and needs a warehouse; recording the calls takes neither.
    """
    called: list[str] = []

    for name in (
        "t_expiry_risk",
        "t_elasticity",
        "t_markdown",
        "t_deal_slots",
        "t_transfers",
        "t_newsvendor",
    ):
        monkeypatch.setattr(tasks, name, (lambda n: lambda _args: (called.append(n), 0)[1])(name))

    args = tasks.build_parser().parse_args(["recommend"])
    assert tasks.t_recommend(args) == 0
    assert called == [
        "t_expiry_risk",
        "t_elasticity",
        "t_markdown",
        "t_deal_slots",
        "t_transfers",
        "t_newsvendor",
    ]


def test_recommend_stops_at_the_first_engine_that_fails(monkeypatch) -> None:
    """Continuing past a failure would build the later tables on missing inputs.

    The markdown optimiser reads elasticity's output; if elasticity failed and
    the chain carried on, `rec_markdown` would be written from whatever the
    previous run left behind and would look like a successful build.
    """
    called: list[str] = []

    monkeypatch.setattr(tasks, "t_expiry_risk", lambda _a: (called.append("expiry"), 0)[1])
    monkeypatch.setattr(tasks, "t_elasticity", lambda _a: (called.append("elasticity"), 3)[1])
    for name in ("t_markdown", "t_deal_slots", "t_transfers", "t_newsvendor"):
        monkeypatch.setattr(tasks, name, lambda _a: (called.append("later"), 0)[1])

    args = tasks.build_parser().parse_args(["recommend"])
    assert tasks.t_recommend(args) == 3, "the failing engine's exit code has to survive"
    assert called == ["expiry", "elasticity"], f"the chain continued past a failure: {called}"


def test_recommend_carries_the_declared_assumptions() -> None:
    """The chained engines read these off the namespace, so it has to carry them.

    Each is a declared assumption rather than a measurement - rupees per van
    trip, the markdown budget - and calling the engines from `recommend`
    without them raises AttributeError at the point the engine is invoked,
    which is several minutes into a rebuild.
    """
    args = tasks.build_parser().parse_args(["recommend"])
    for assumption in ("budget", "slots", "fixed_trip_cost", "retention_cost"):
        assert hasattr(args, assumption), (
            f"`recommend` does not carry --{assumption.replace('_', '-')}, so the engine "
            "that reads it fails partway through the chain"
        )
