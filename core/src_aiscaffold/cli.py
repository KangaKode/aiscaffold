"""
aiscaffold CLI - Project scaffold management.

Commands:
    aiscaffold init [name]     Create a new project
    aiscaffold doctor          Validate project structure
    aiscaffold add <module>    Add opt-in module
    aiscaffold update          Pull template updates
"""

import ast
import os
import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="AI project scaffold with 2026 best practices")
console = Console()

TEMPLATE_REPO = "gh:KangaKode/aiscaffold"
LOCAL_TEMPLATE = str(Path(__file__).parent.parent.parent.parent / "aiscaffold-template")


def _get_template_source() -> str:
    """Use local template if available, otherwise GitHub."""
    if Path(LOCAL_TEMPLATE).exists():
        return LOCAL_TEMPLATE
    return TEMPLATE_REPO


# =============================================================================
# INIT
# =============================================================================


@app.command()
def init(
    name: str = typer.Argument(None, help="Project name"),
    template: str = typer.Option(None, help="Template source (default: auto-detect)"),
):
    """Create a new AI tool project with 2026 best practices."""
    source = template or _get_template_source()

    console.print(f"\n[bold blue]aiscaffold init[/bold blue]")
    console.print(f"Template: {source}\n")

    cmd = ["copier", "copy", source, "."]
    if name:
        cmd.extend(["--data", f"project_name={name}"])

    try:
        subprocess.run(cmd, check=True)
        console.print("\n[bold green]Project created successfully![/bold green]")
    except subprocess.CalledProcessError as e:
        console.print(f"\n[bold red]Error:[/bold red] {e}")
        raise typer.Exit(1)
    except FileNotFoundError:
        console.print("[bold red]Error:[/bold red] copier not found. Install it: pip install copier")
        raise typer.Exit(1)


# =============================================================================
# DOCTOR
# =============================================================================


@app.command()
def doctor(
    path: str = typer.Argument(".", help="Project root directory"),
):
    """Validate project structure against scaffold standards."""
    root = Path(path).resolve()
    console.print(f"\n[bold blue]aiscaffold doctor[/bold blue]")
    console.print(f"Checking: {root}\n")

    issues = []
    warnings = []

    # Check required files
    required_files = ["CLAUDE.md", "docs/ARCHITECTURE.md", "tests/test_architecture.py", ".gitignore", "pyproject.toml"]
    for f in required_files:
        if not (root / f).exists():
            issues.append(f"Missing required file: {f}")

    # Check required dirs
    if (root / "tests").is_dir():
        pass
    else:
        issues.append("Missing tests/ directory")

    if (root / "docs").is_dir():
        pass
    else:
        issues.append("Missing docs/ directory")

    if (root / ".cursor" / "agents").is_dir():
        agent_count = len(list((root / ".cursor" / "agents").glob("*.md")))
        if agent_count < 2:
            warnings.append(f"Only {agent_count} subagent(s) installed (recommend 11+)")
    else:
        issues.append("Missing .cursor/agents/ directory")

    # Check for stray markdown in root
    for f in root.iterdir():
        if f.suffix == ".md" and f.name not in ("README.md", "CLAUDE.md"):
            warnings.append(f"Stray markdown in root: {f.name} (move to docs/)")

    # Check for stray scripts in root
    for f in root.iterdir():
        if f.suffix in (".command", ".sh"):
            warnings.append(f"Stray script in root: {f.name} (move to scripts/)")

    # Report
    table = Table(title="Doctor Report")
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Details")

    for issue in issues:
        table.add_row("FAIL", "[red]BLOCKING[/red]", issue)
    for warn in warnings:
        table.add_row("WARN", "[yellow]WARNING[/yellow]", warn)

    if not issues and not warnings:
        table.add_row("ALL", "[green]PASS[/green]", "Project structure is clean")

    console.print(table)

    if issues:
        console.print(f"\n[bold red]{len(issues)} blocking issue(s). Fix before continuing.[/bold red]")
        raise typer.Exit(1)
    elif warnings:
        console.print(f"\n[yellow]{len(warnings)} warning(s). Consider fixing.[/yellow]")
    else:
        console.print("\n[bold green]All checks passed![/bold green]")


# =============================================================================
# ADD
# =============================================================================


@app.command()
def add(
    module: str = typer.Argument(help="Module to add: evals, state, agent:<name>, layer:<name>"),
):
    """Add an opt-in module to the current project."""
    root = Path(".").resolve()

    if module == "evals":
        _add_evals(root)
    elif module == "state":
        _add_state(root)
    elif module.startswith("agent:"):
        agent_name = module.split(":", 1)[1]
        _add_agent(root, agent_name)
    elif module.startswith("layer:"):
        layer_name = module.split(":", 1)[1]
        _add_layer(root, layer_name)
    else:
        console.print(f"[red]Unknown module: {module}[/red]")
        console.print("Available: evals, state, agent:<name>, layer:<name>")
        raise typer.Exit(1)


def _add_evals(root: Path):
    """Add eval infrastructure."""
    dirs = ["evals", "evals/capability", "evals/regression", "evals/graders", "evals/results"]
    for d in dirs:
        (root / d).mkdir(parents=True, exist_ok=True)

    init_files = ["evals/__init__.py", "evals/graders/__init__.py"]
    for f in init_files:
        fp = root / f
        if not fp.exists():
            fp.write_text('"""Eval infrastructure."""\n')

    console.print("[green]Added evals/ directory structure[/green]")
    console.print("Next: Create graders in evals/graders/ and tasks in evals/capability/")


def _add_state(root: Path):
    """Add state management utilities."""
    console.print("[green]State management utilities available via:[/green]")
    console.print("  pip install aiscaffold")
    console.print("  from aiscaffold import TaskList, ProgressNotesManager")


def _add_agent(root: Path, name: str):
    """Add a new subagent."""
    agents_dir = root / ".cursor" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)

    filepath = agents_dir / f"{name}.md"
    if filepath.exists():
        console.print(f"[yellow]Agent {name} already exists[/yellow]")
        return

    filepath.write_text(
        f"---\nname: {name}\ndescription: TODO - describe when to use this agent\n---\n\n"
        f"# {name.replace('-', ' ').title()}\n\nTODO: Add agent instructions.\n"
    )
    console.print(f"[green]Created .cursor/agents/{name}.md[/green]")
    console.print("Edit the file to add your agent's instructions.")


def _add_layer(root: Path, name: str):
    """Add a new architecture layer."""
    layer_dir = root / name
    layer_dir.mkdir(exist_ok=True)
    (layer_dir / "__init__.py").touch()

    console.print(f"[green]Created {name}/ layer[/green]")
    console.print(f"[yellow]IMPORTANT: Update tests/test_architecture.py to add '{name}' to FORBIDDEN_IMPORTS[/yellow]")


# =============================================================================
# UPDATE
# =============================================================================


@app.command()
def update():
    """Pull template updates into the current project."""
    if not Path(".copier-answers.yml").exists():
        console.print("[red]Not a scaffolded project (no .copier-answers.yml)[/red]")
        raise typer.Exit(1)

    console.print("[bold blue]aiscaffold update[/bold blue]")
    console.print("Pulling template updates...\n")

    try:
        subprocess.run(["copier", "update"], check=True)
        console.print("\n[bold green]Update complete![/bold green]")
    except subprocess.CalledProcessError as e:
        console.print(f"\n[bold red]Update failed:[/bold red] {e}")
        raise typer.Exit(1)


# =============================================================================
# VERSION
# =============================================================================


@app.command()
def version():
    """Show aiscaffold version."""
    from aiscaffold import __version__
    console.print(f"aiscaffold v{__version__}")


if __name__ == "__main__":
    app()
