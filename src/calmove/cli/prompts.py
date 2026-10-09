"""Interactive wizard prompts (rich) for the migrate command."""

from datetime import UTC, datetime

import typer
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from calmove.domain.date_range import DateRange
from calmove.domain.errors import InvalidDateRangeError
from calmove.domain.model import Calendar

WRITABLE_ROLES = frozenset({"writer", "owner"})


def pick_calendar(
    console: Console,
    calendars: list[Calendar],
    *,
    role_label: str,
    require_writable: bool = False,
) -> Calendar:
    """Show a numbered calendar table and prompt until a valid pick is made."""
    if not calendars:
        console.print(f"[red]No calendars found for the {role_label} account.[/red]")
        raise typer.Exit(code=1)

    table = Table(title=f"{role_label.capitalize()} calendars")
    table.add_column("#", justify="right")
    table.add_column("Calendar")
    table.add_column("Access role")
    table.add_column("Primary")
    for index, calendar in enumerate(calendars, start=1):
        table.add_row(
            str(index),
            calendar.summary or calendar.id,
            calendar.access_role,
            "yes" if calendar.primary else "",
        )
    console.print(table)

    while True:
        raw = Prompt.ask(f"Pick the {role_label} calendar by number", console=console)
        try:
            index = int(raw)
        except ValueError:
            console.print("[red]Please enter a number.[/red]")
            continue
        if not 1 <= index <= len(calendars):
            console.print(f"[red]Enter a number between 1 and {len(calendars)}.[/red]")
            continue
        calendar = calendars[index - 1]
        if require_writable and calendar.access_role not in WRITABLE_ROLES:
            console.print(
                f"[red]'{calendar.summary or calendar.id}' has access role "
                f"'{calendar.access_role}'; the destination needs 'writer' or 'owner'.[/red]"
            )
            continue
        return calendar


def _prompt_date(console: Console, label: str) -> datetime:
    while True:
        raw = Prompt.ask(f"{label} (YYYY-MM-DD)", console=console)
        try:
            return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            console.print("[red]Invalid date; use YYYY-MM-DD.[/red]")


def prompt_date_range(console: Console) -> DateRange:
    """Prompt for one of the three range presets."""
    console.print("Date range:")
    console.print("  1. From today onward")
    console.print("  2. Everything")
    console.print("  3. Custom from/to dates")
    choice = Prompt.ask("Pick a preset", choices=["1", "2", "3"], default="1", console=console)
    if choice == "1":
        return DateRange.future()
    if choice == "2":
        return DateRange.everything()
    while True:
        start = _prompt_date(console, "From")
        end = _prompt_date(console, "To")
        try:
            return DateRange.between(start, end)
        except InvalidDateRangeError:
            console.print("[red]The end date must lie after the start date.[/red]")


def confirm_migration(console: Console) -> bool:
    """Require the user to type the literal word 'migrate'."""
    answer = Prompt.ask(
        "Type [bold]migrate[/bold] to start the migration (anything else aborts)",
        console=console,
    )
    return answer.strip() == "migrate"
