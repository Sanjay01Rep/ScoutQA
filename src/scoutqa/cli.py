"""`scoutqa` command line."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from scoutqa import pipeline
from scoutqa.config.example import example_yaml
from scoutqa.config.loader import DEFAULT_CONFIG_NAME, load_config
from scoutqa.config.models import ProjectConfig
from scoutqa.errors import ScoutQAError
from scoutqa.log import setup_logging
from scoutqa.template.loader import describe
from scoutqa.workspace import workspace_for

app = typer.Typer(help="Crawl a web app and generate test cases.", no_args_is_help=True, add_completion=False)
console = Console()

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="Path to scoutqa.yaml")]
VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")]
HeadedOpt = Annotated[bool, typer.Option("--headed", help="Show the browser window")]


def _fail(exc: ScoutQAError) -> typer.Exit:
    console.print(f"[red]Error:[/red] {exc}")
    return typer.Exit(code=1)


@app.command()
def init(
    project: Annotated[str, typer.Option(help="Project name (letters, digits, - _ .)")] = "myapp",
    base_url: Annotated[str, typer.Option(help="Start URL of the app under test")] = "https://myapp.example.com/",
    path: Annotated[Path, typer.Option(help="Where to write the config")] = Path(DEFAULT_CONFIG_NAME),
    force: Annotated[bool, typer.Option(help="Overwrite an existing file")] = False,
) -> None:
    """Write a commented scoutqa.yaml."""
    if path.exists() and not force:
        console.print(f"[yellow]{path} already exists[/yellow] (use --force to overwrite)")
        raise typer.Exit(code=1)
    path.write_text(example_yaml(project, base_url), encoding="utf-8")
    console.print(f"Wrote [bold]{path}[/bold]. Edit the auth section, then run [bold]scoutqa login[/bold].")


RoleOpt = Annotated[str | None, typer.Option("--role", "-r", help="Login profile (auth.profiles); default: first")]


def _roles(cfg: ProjectConfig, role: str | None, all_roles: bool) -> list[str | None]:
    return list(cfg.auth.profile_names()) if all_roles else [role]


@app.command()
def login(
    config: ConfigOpt = Path(DEFAULT_CONFIG_NAME),
    role: RoleOpt = None,
    all_roles: Annotated[bool, typer.Option("--all-roles", help="Log in with every profile")] = False,
    force: Annotated[bool, typer.Option(help="Ignore the saved session and log in again")] = False,
    headed: HeadedOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Log in once and save the session for later crawls."""
    setup_logging(verbose)
    try:
        cfg = load_config(config)
        for r in _roles(cfg, role, all_roles):
            report = asyncio.run(pipeline.login(cfg, role=r, force=force, headed=headed))
            console.print(f"[{report.role}] session: [bold]{report.outcome.value}[/bold]")
            if report.storage_state:
                console.print(f"  saved to {report.storage_state} (contains session cookies — keep it private)")
    except ScoutQAError as exc:
        raise _fail(exc) from None


@app.command()
def crawl(
    config: ConfigOpt = Path(DEFAULT_CONFIG_NAME),
    role: RoleOpt = None,
    all_roles: Annotated[bool, typer.Option("--all-roles", help="Crawl once per profile")] = False,
    max_pages: Annotated[int | None, typer.Option(help="Override scope.max_pages")] = None,
    max_depth: Annotated[int | None, typer.Option(help="Override scope.max_depth")] = None,
    headed: HeadedOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Crawl the app within scope (read-only by default) and update the app model."""
    try:
        cfg = load_config(config, {"scope.max_pages": max_pages, "scope.max_depth": max_depth})
        ws = workspace_for(cfg.project)
        setup_logging(verbose)
        for r in _roles(cfg, role, all_roles):
            report = asyncio.run(pipeline.crawl(cfg, role=r, headed=headed, workspace=ws))
            result = report.result
            table = Table(title=f"Crawl {result.run_id} [{result.role}] — {result.stopped_reason}", show_header=False)
            table.add_row("session", result.session.value)
            for key, value in result.stats().items():
                table.add_row(key, str(value))
            table.add_row("states (new/changed/unchanged/removed)",
                          "/".join(str(report.deltas.get(k, 0)) for k in ("new", "changed", "unchanged", "removed")))
            console.print(table)
            console.print(f"Result: {report.result_path}")
    except ScoutQAError as exc:
        raise _fail(exc) from None


@app.command()
def generate(
    config: ConfigOpt = Path(DEFAULT_CONFIG_NAME),
    rules_only: Annotated[bool, typer.Option("--rules-only", help="Zero-token rule packs only")] = True,
    list_cases: Annotated[bool, typer.Option("--list", help="Print every generated case")] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Generate test cases from the app model (rule packs: zero tokens)."""
    setup_logging(verbose)
    try:
        cfg = load_config(config)
        report = pipeline.generate(cfg, rules_only=rules_only)
    except ScoutQAError as exc:
        raise _fail(exc) from None
    summary = Table(title=f"{len(report.cases)} test cases (0 tokens)", show_header=True)
    summary.add_column("Module")
    summary.add_column("Cases", justify="right")
    for module, count in report.by_module.items():
        summary.add_row(module, str(count))
    console.print(summary)
    console.print("By priority: " + ", ".join(f"{k} {v}" for k, v in sorted(report.by_priority.items())))
    console.print("By type:     " + ", ".join(f"{k} {v}" for k, v in sorted(report.by_type.items())))
    console.print(f"Need review (contain assumptions): {report.needs_review}")
    if list_cases:
        cases = Table(show_header=True)
        for column in ("ID", "Priority", "Title", "Rule"):
            cases.add_column(column)
        for case in report.cases:
            cases.add_row(case.id, case.priority.value, case.title, case.source.generator)
        console.print(cases)
    console.print(f"Cases: {report.cases_path}")


@app.command()
def template(
    path: Annotated[str | None, typer.Argument(help="Template file; default: template.path in scoutqa.yaml")] = None,
    config: ConfigOpt = Path(DEFAULT_CONFIG_NAME),
) -> None:
    """Show how a template's columns map to test case fields (nothing is written)."""
    try:
        cfg = load_config(config)
        spec = pipeline.template_info(cfg, path)
    except ScoutQAError as exc:
        raise _fail(exc) from None
    table = Table(title=f"Template: {spec.source} ({spec.layout})")
    for column in ("Column", "Filled from", "Why"):
        table.add_column(column)
    for name, field, reason in describe(spec):
        table.add_row(name, field, reason)
    console.print(table)
    if spec.custom_columns:
        console.print("[yellow]Left blank:[/yellow] " + ", ".join(c.name for c in spec.custom_columns)
                      + "  (set template.columns or template.defaults in scoutqa.yaml; the LLM stage can fill them)")


@app.command()
def export(
    config: ConfigOpt = Path(DEFAULT_CONFIG_NAME),
    fmt: Annotated[str, typer.Option("--format", "-f", help="xlsx | csv | md | json")] = "xlsx",
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Output file")] = None,
    template_path: Annotated[str | None, typer.Option("--template", "-t", help="Template file (overrides config)")]
    = None,
    verbose: VerboseOpt = False,
) -> None:
    """Write the generated test cases in your template, with Coverage, Limitations and Trace."""
    setup_logging(verbose)
    if fmt not in ("xlsx", "csv", "md", "json"):
        console.print(f"[red]Error:[/red] unknown format {fmt!r} (xlsx, csv, md, json)")
        raise typer.Exit(code=1)
    try:
        cfg = load_config(config)
        report = pipeline.export(cfg, fmt=fmt, output=output, template=template_path)  # type: ignore[arg-type]
    except ScoutQAError as exc:
        raise _fail(exc) from None
    console.print(f"Wrote [bold]{report.path}[/bold]: {report.cases} cases, {report.rows} rows "
                  f"({report.needs_review} need review) using template {report.template}")
    if report.custom_columns:
        console.print("[yellow]Left blank:[/yellow] " + ", ".join(report.custom_columns))


@app.command()
def serve(
    config: ConfigOpt = Path(DEFAULT_CONFIG_NAME),
    port: Annotated[int, typer.Option(help="Port on 127.0.0.1")] = 8765,
    verbose: VerboseOpt = False,
) -> None:
    """Run the local service for the ScoutQA browser extension (Record mode)."""
    from scoutqa.distill.extract import extension_dir
    from scoutqa.service.app import ScoutQAService

    setup_logging(verbose)
    try:
        cfg = load_config(config)
        service = ScoutQAService(cfg, workspace_for(cfg.project), port=port)
    except ScoutQAError as exc:
        raise _fail(exc) from None
    except OSError as exc:
        console.print(f"[red]Error:[/red] cannot listen on 127.0.0.1:{port} ({exc}). Try --port.")
        raise typer.Exit(code=1) from None
    console.print(f"ScoutQA service for [bold]{cfg.project}[/bold] on {service.url} (this computer only)")
    console.print(f"Pairing code: [bold reverse] {service.pairing_code} [/bold reverse]  (single use, 15 minutes)")
    console.print(f"Extension folder (chrome://extensions → Developer mode → Load unpacked):\n  {extension_dir()}")
    console.print("Press Ctrl+C to stop.")
    try:
        service.serve_forever()
    except KeyboardInterrupt:
        console.print("Stopped.")
    finally:
        service.stop()


@app.command()
def extension() -> None:
    """Show where the browser extension is and how to install it."""
    from scoutqa.distill.extract import extension_dir

    console.print(f"Extension folder:\n  {extension_dir()}\n")
    console.print("Chrome / Edge: open chrome://extensions (edge://extensions), enable Developer mode, click "
                  "'Load unpacked' and choose that folder. Then run `scoutqa serve` and pair from the side panel.")


@app.command(name="map")
def map_(
    config: ConfigOpt = Path(DEFAULT_CONFIG_NAME),
    role: RoleOpt = None,
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Write the map to a file")] = None,
) -> None:
    """Print the compact app map (what later stages — and the LLM — work from)."""
    try:
        cfg = load_config(config)
        result = pipeline.app_map(cfg, role=role)
    except ScoutQAError as exc:
        raise _fail(exc) from None
    if output:
        output.write_text(result.text, encoding="utf-8")
        console.print(f"Wrote {output}")
    else:
        console.print(result.text, markup=False, highlight=False, soft_wrap=True)
    console.print(f"\n[dim]{result.states} states, ~{result.approx_tokens} tokens[/dim]")


@app.command()
def models(
    config: ConfigOpt = Path(DEFAULT_CONFIG_NAME),
    test: Annotated[bool, typer.Option("--test", help="Send a trivial live request to check it works")] = False,
    stage: Annotated[str, typer.Option(help="Generation stage to test (default: llm.default_profile)")] = "smoke",
    verbose: VerboseOpt = False,
) -> None:
    """List configured model profiles and routing, or (--test) send one real request to check a profile."""
    setup_logging(verbose)
    try:
        cfg = load_config(config)
        if not test:
            _print_profiles(cfg)
            return
        report = asyncio.run(pipeline.test_model(cfg, stage=stage))
    except ScoutQAError as exc:
        raise _fail(exc) from None
    console.print(f"[bold]{report.provider}/{report.model}[/bold] (stage '{report.stage}', profile "
                  f"'{report.profile}'){' [dim](cached)[/dim]' if report.cached else ''}")
    console.print(f"  Reply: {report.reply}")
    cost = f"${report.cost_usd:.5f}" if report.cost_usd is not None else "unknown (no price on file)"
    console.print(f"  {report.input_tokens} in ({report.cached_input_tokens} cached) + {report.output_tokens} "
                  f"out · {cost} · {report.latency_s:.1f}s")


def _print_profiles(cfg: ProjectConfig) -> None:
    if not cfg.llm.profiles:
        console.print("No model profiles configured. Add one under [bold]llm.profiles[/bold] in "
                      f"{DEFAULT_CONFIG_NAME} — see scoutqa.example.yaml.")
        return
    table = Table(title="Model profiles")
    for column in ("Profile", "Provider", "Model", "Stages routed here"):
        table.add_column(column)
    stages_by_profile: dict[str, list[str]] = {}
    for stage_name, profile_name in cfg.llm.routing.items():
        stages_by_profile.setdefault(profile_name, []).append(stage_name)
    for name, profile in cfg.llm.profiles.items():
        stages = list(stages_by_profile.get(name, []))
        if name == cfg.llm.default_profile:
            stages.insert(0, "(default)")
        table.add_row(name, profile.provider, profile.model, ", ".join(stages) or "—")
    console.print(table)
    if cfg.llm.default_profile is None and not cfg.llm.routing:
        console.print("[yellow]No stage is routed yet[/yellow] — set llm.default_profile in scoutqa.yaml.")


@app.command()
def usage(
    config: ConfigOpt = Path(DEFAULT_CONFIG_NAME),
) -> None:
    """Show LLM tokens and estimated cost spent on this project so far."""
    try:
        cfg = load_config(config)
        report = pipeline.usage_report(cfg)
    except ScoutQAError as exc:
        raise _fail(exc) from None
    if not report.rows:
        console.print(f"No LLM calls recorded yet for [bold]{cfg.project}[/bold].")
        return
    table = Table(title=f"LLM usage — {cfg.project}")
    for column in ("Stage", "Provider", "Model", "Calls", "Input", "Cached", "Output", "Cost"):
        table.add_column(column, justify="right" if column not in ("Stage", "Provider", "Model") else "left")
    for row in report.rows:
        table.add_row(row.stage, row.provider, row.model, str(row.calls), f"{row.input_tokens:,}",
                      f"{row.cached_input_tokens:,}", f"{row.output_tokens:,}",
                      f"${row.cost_usd:.4f}" if row.cost_usd is not None else "—")
    console.print(table)
    total_cost = f"${report.total_cost_usd:.4f}" if report.total_cost_usd is not None else "unknown"
    console.print(f"Total: {report.total_input_tokens:,} in + {report.total_output_tokens:,} out · {total_cost}")
    console.print(f"[dim]{report.cache_entries} response(s) cached (free on their next exact re-use)[/dim]")


if __name__ == "__main__":
    app()
