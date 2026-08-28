#!/usr/bin/env python
"""FreshFlow task runner.

Windows has no `make`, so this stdlib-only script plays that role. It works
unchanged in CI, which a Makefile would not.

    python tasks.py --help
    python tasks.py simulate --days 365
    python tasks.py all
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
TRANSFORM = ROOT / "transform"
DATA = ROOT / "data"
WAREHOUSE = DATA / "warehouse" / "freshflow.duckdb"
DEMO = ROOT / "serving" / "demo" / "freshflow_demo.duckdb"

DATA_DIRS = [DATA / "raw", DATA / "warehouse", DEMO.parent]

# targets that are not built yet - each is claimed by a task in docs/EXECUTION_PLAN.md
PENDING = {
    "simulate": "S1.7",
    "forecast": "S3.3",
    "recommend": "S4.6",
    "experiment": "S5.1",
    "demo-slice": "S2.8",
}


# ----------------------------------------------------------------- helpers
def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> int:
    """Run a command, streaming output. Returns its exit code."""
    printable = " ".join(cmd)
    print(f"\n\033[36m$ {printable}\033[0m", flush=True)
    started = time.time()
    code = subprocess.run(cmd, cwd=cwd or ROOT).returncode
    elapsed = time.time() - started
    if code == 0:
        print(f"\033[32m  ok\033[0m ({elapsed:.1f}s)")
    else:
        print(f"\033[31m  failed with exit {code}\033[0m ({elapsed:.1f}s)")
        if check:
            sys.exit(code)
    return code


def py(*args: str) -> int:
    """Run a module/script with the current interpreter."""
    return run([sys.executable, *args])


def not_yet(target: str) -> int:
    task = PENDING.get(target, "?")
    print(
        f"\n\033[33m'{target}' is not implemented yet.\033[0m\n"
        f"  Claimed by task {task} in docs/EXECUTION_PLAN.md.\n"
    )
    return 0


# ----------------------------------------------------------------- targets
def t_setup(_: argparse.Namespace) -> int:
    """Create local data directories and check the toolchain."""
    for d in DATA_DIRS:
        d.mkdir(parents=True, exist_ok=True)
        print(f"  ensured {d.relative_to(ROOT)}")

    missing = []
    for mod in ("duckdb", "pandas", "numpy", "yaml"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        print(f"\n\033[31mmissing packages: {', '.join(missing)}\033[0m")
        print("  run:  uv sync")
        return 1
    print("\n\033[32mtoolchain OK\033[0m")
    return 0


def t_simulate(args: argparse.Namespace) -> int:
    """Generate the synthetic event data into data/raw/."""
    if not (ROOT / "simulator" / "run.py").exists():
        return not_yet("simulate")
    return py("-m", "simulator.run", "--days", str(args.days), "--seed", str(args.seed))


def dbt(*args: str, check: bool = True) -> int:
    """Invoke dbt as a module so it resolves from the active venv, not PATH."""
    # The staging sources are read_parquet() calls, and a relative path in one
    # resolves against whatever process opens the view - not against dbt. An
    # absolute base path keeps them readable from a notebook, a test or the
    # metrics API instead of only from transform/.
    os.environ.setdefault("FRESHFLOW_RAW_DIR", (DATA / "raw").as_posix())
    return run(
        [sys.executable, "-m", "dbt.cli.main", *args, "--profiles-dir", "."],
        cwd=TRANSFORM,
        check=check,
    )


def _ensure_dbt_deps() -> None:
    """Install dbt packages once, if packages.yml declares any."""
    if (TRANSFORM / "packages.yml").exists() and not (TRANSFORM / "dbt_packages").exists():
        dbt("deps")


def t_build(args: argparse.Namespace) -> int:
    """Run dbt: seeds -> models -> tests."""
    _ensure_dbt_deps()
    extra: list[str] = ["--target", args.dbt_target]
    if args.select:
        extra += ["--select", args.select]
    if args.full_refresh:
        extra += ["--full-refresh"]
    return dbt("build", *extra)


def t_test(_: argparse.Namespace) -> int:
    """Run the python test suite."""
    return py("-m", "pytest")


def t_lint(args: argparse.Namespace) -> int:
    """Lint python and SQL."""
    fix = ["--fix"] if args.fix else []
    code = run([sys.executable, "-m", "ruff", "check", ".", *fix], check=False)
    code |= run(
        [sys.executable, "-m", "ruff", "format", "." if args.fix else "--check", "."], check=False
    )
    # models AND macros, because that is what CI lints. A local lint narrower
    # than the CI one is worse than none: it reports green on exactly the files
    # nobody checked, and the divergence cost three commits of red builds
    # before anyone looked at why.
    sql_paths = [d for d in ("models", "macros") if any((TRANSFORM / d).rglob("*.sql"))]
    if sql_paths:
        code |= run(
            [sys.executable, "-m", "sqlfluff", "lint", *sql_paths], cwd=TRANSFORM, check=False
        )
    return 1 if code else 0


def t_forecast(_: argparse.Namespace) -> int:
    """Train the demand forecast and write predictions."""
    if not (ROOT / "analytics" / "forecasting" / "train.py").exists():
        return not_yet("forecast")
    return py("-m", "analytics.forecasting.train")


def t_recommend(_: argparse.Namespace) -> int:
    """Run the decision engine and write the rec_* action tables."""
    if not (ROOT / "analytics" / "optimization" / "markdown.py").exists():
        return not_yet("recommend")
    return py("-m", "analytics.optimization.run_all")


def t_experiment(_: argparse.Namespace) -> int:
    """Run the policy A/B backtest and write mart_experiment_readout."""
    if not (ROOT / "analytics" / "experiment" / "readout.py").exists():
        return not_yet("experiment")
    return py("-m", "analytics.experiment.readout")


def t_demo_slice(_: argparse.Namespace) -> int:
    """Build the <80MB warehouse that Streamlit Cloud actually deploys."""
    if not (ROOT / "analytics" / "demo_slice.py").exists():
        return not_yet("demo-slice")
    code = py("-m", "analytics.demo_slice")
    if DEMO.exists():
        mb = DEMO.stat().st_size / 1_048_576
        flag = "\033[32mok\033[0m" if mb < 80 else "\033[31mTOO BIG\033[0m"
        print(f"  demo warehouse: {mb:.1f} MB  {flag} (limit 80 MB)")
        if mb >= 80:
            return 1
        # the slice is gitignored - building it locally does not change what the
        # deployed app reads until it is published
        print("  next: `python tasks.py publish-demo` to make this the deployed build")
    return code


def t_publish_demo(args: argparse.Namespace) -> int:
    """Upload the demo warehouse to its GitHub Release and update the manifest."""
    extra = ["--dry-run"] if args.dry_run else []
    return py("-m", "serving.publish_demo", *extra)


def t_gate(_: argparse.Namespace) -> int:
    """Run checkpoint gate G1 against the emitted raw layer."""
    return py("-m", "simulator.verify")


def t_profile(_: argparse.Namespace) -> int:
    """Render the Sprint 1 data profile from the emitted raw layer."""
    return py("-m", "simulator.profile_report")


def t_api(args: argparse.Namespace) -> int:
    """Serve the metrics API locally."""
    return run(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "serving.api.main:app",
            "--reload",
            "--port",
            str(args.port),
        ]
    )


def t_app(args: argparse.Namespace) -> int:
    """Serve the Streamlit control tower locally."""
    return run(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            "serving/web/streamlit/Home.py",
            "--server.port",
            str(args.port),
        ]
    )


def t_dagster(_: argparse.Namespace) -> int:
    """Open the Dagster UI."""
    os.environ.setdefault("DAGSTER_HOME", str(ROOT / "dagster_home"))
    Path(os.environ["DAGSTER_HOME"]).mkdir(exist_ok=True)
    return run([sys.executable, "-m", "dagster", "dev", "-m", "orchestration.definitions"])


def t_docs(args: argparse.Namespace) -> int:
    """Generate dbt docs and the metric dictionary."""
    # The catalog is a live query against a warehouse, so docs can only be
    # generated for a target that has actually been built. CI has the ci slice
    # and no dev warehouse at all.
    _ensure_dbt_deps()
    code = dbt("docs", "generate", "--target", args.dbt_target, check=False)
    gen = ROOT / "semantic" / "generate_docs.py"
    if gen.exists():
        code |= py("-m", "semantic.generate_docs")
    else:
        print("  (semantic/generate_docs.py missing)")
    return 1 if code else 0


def t_clean(_: argparse.Namespace) -> int:
    """Remove build artefacts. Does NOT touch data/raw."""
    for p in [
        TRANSFORM / "target",
        TRANSFORM / "dbt_packages",
        ROOT / ".pytest_cache",
        ROOT / ".ruff_cache",
        ROOT / "logs",
    ]:
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
            print(f"  removed {p.relative_to(ROOT)}")
    for db in WAREHOUSE.parent.glob("*.duckdb*"):
        db.unlink()
        print(f"  removed {db.relative_to(ROOT)}")
    return 0


def t_all(args: argparse.Namespace) -> int:
    """Full rebuild: simulate -> build -> forecast -> recommend."""
    for fn in (t_setup, t_simulate, t_build, t_forecast, t_recommend):
        if fn(args) != 0:
            return 1
    return 0


# ----------------------------------------------------------------- cli
TARGETS = {
    "setup": t_setup,
    "simulate": t_simulate,
    "build": t_build,
    "test": t_test,
    "gate": t_gate,
    "profile": t_profile,
    "lint": t_lint,
    "forecast": t_forecast,
    "recommend": t_recommend,
    "experiment": t_experiment,
    "demo-slice": t_demo_slice,
    "publish-demo": t_publish_demo,
    "api": t_api,
    "app": t_app,
    "dagster": t_dagster,
    "docs": t_docs,
    "clean": t_clean,
    "all": t_all,
}


def build_parser() -> argparse.ArgumentParser:
    """The CLI, built separately so tests can validate callers against it.

    CI and the docs workflow invoke this script by string. A flag renamed here
    breaks them silently - the workflow keeps passing an argument argparse no
    longer knows, argparse exits 2, and the only place that shows up is a red
    build. tests/test_task_runner.py parses every invocation out of the
    workflow files and checks it against this parser.
    """
    parser = argparse.ArgumentParser(
        prog="tasks.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="target", required=True, metavar="TARGET")

    for name, fn in TARGETS.items():
        p = sub.add_parser(name, help=(fn.__doc__ or "").strip().split("\n")[0])
        if name in ("simulate", "all"):
            p.add_argument("--days", type=int, default=365)
            p.add_argument("--seed", type=int, default=42)
        if name in ("build", "all"):
            p.add_argument("--select", default=None, help="dbt node selector")
            p.add_argument("--full-refresh", action="store_true")
        if name in ("build", "all", "docs"):
            # dest is renamed because the subparser already owns args.target
            p.add_argument(
                "--target", dest="dbt_target", default="dev", choices=["dev", "ci", "demo"]
            )
        if name == "lint":
            p.add_argument("--fix", action="store_true")
        if name == "publish-demo":
            p.add_argument(
                "--dry-run",
                action="store_true",
                help="hash the slice and print the manifest without uploading",
            )
        if name in ("api", "app"):
            p.add_argument("--port", type=int, default=8000 if name == "api" else 8501)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    # give t_all the flags its children expect
    for attr, default in (
        ("select", None),
        ("full_refresh", False),
        ("dbt_target", "dev"),
        ("days", 365),
        ("seed", 42),
        ("fix", False),
        ("port", 8000),
    ):
        if not hasattr(args, attr):
            setattr(args, attr, default)
    return TARGETS[args.target](args)


if __name__ == "__main__":
    sys.exit(main())
