"""CalMove CLI — Typer app and composition root."""

import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
from googleapiclient.discovery import build
from rich.console import Console
from rich.table import Table

from calmove.application.migrate_calendar import (
    MigrateCalendarUseCase,
    MigrationRequest,
    MigrationResult,
)
from calmove.domain.model import CalendarEvent
from calmove.infrastructure.google.auth import (
    DESTINATION_SCOPES,
    SOURCE_SCOPES,
    KeyringTokenStore,
    authenticate,
)
from calmove.infrastructure.google.gateway import GoogleCalendarGateway

from . import prompts

app = typer.Typer(help="Migrate a Google Calendar between Google Workspaces.")
console = Console()


class SendUpdates(StrEnum):
    none = "none"
    all = "all"
    externalOnly = "externalOnly"


class _SilentReporter:
    """Reporter for the dry-run planning pass; the plan table speaks instead."""

    def event_created(self, event: CalendarEvent) -> None:
        pass

    def event_forwarded(self, event: CalendarEvent) -> None:
        pass

    def event_updated(self, event: CalendarEvent) -> None:
        pass

    def event_deleted(self, source_id: str) -> None:
        pass

    def event_skipped(self, event: CalendarEvent) -> None:
        pass

    def event_failed(self, event: CalendarEvent, error: Exception) -> None:
        pass


class ConsoleReporter:
    """Prints one line per migrated event during the real run."""

    def __init__(self, console: Console) -> None:
        self._console = console

    @staticmethod
    def _label(event: CalendarEvent) -> str:
        return event.summary or event.id

    def event_created(self, event: CalendarEvent) -> None:
        self._console.print(f"[green]created[/green]  {self._label(event)}")

    def event_forwarded(self, event: CalendarEvent) -> None:
        self._console.print(f"[blue]forwarded[/blue] {self._label(event)}")

    def event_updated(self, event: CalendarEvent) -> None:
        self._console.print(f"[cyan]updated[/cyan]  {self._label(event)}")

    def event_deleted(self, source_id: str) -> None:
        self._console.print(f"[magenta]deleted[/magenta]  {source_id}")

    def event_skipped(self, event: CalendarEvent) -> None:
        self._console.print(f"[yellow]skipped[/yellow]  {self._label(event)}")

    def event_failed(self, event: CalendarEvent, error: Exception) -> None:
        self._console.print(f"[red]failed[/red]   {self._label(event)} — {error}")


def _build_gateway(
    role: str, scopes: list[str], client_secret: Path, store: KeyringTokenStore
) -> GoogleCalendarGateway:
    creds = authenticate(role, scopes, client_secret, store)
    service: Any = build("calendar", "v3", credentials=creds)
    return GoogleCalendarGateway(service)


def _primary_address(calendars: list[Any]) -> str | None:
    """The account's primary calendar id is its e-mail address."""
    return next((c.id for c in calendars if c.primary), None)


def _load_room_map(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        console.print(f"[red]Could not read room map {path}: {error}[/red]")
        raise typer.Exit(code=1) from error
    if not isinstance(payload, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in payload.items()
    ):
        console.print(f"[red]Room map {path} must be a JSON object of string → string.[/red]")
        raise typer.Exit(code=1)
    return payload


def _print_plan(result: MigrationResult) -> None:
    table = Table(title="Migration plan (dry run)")
    table.add_column("Outcome")
    table.add_column("Events", justify="right")
    table.add_row("Would create", str(result.created))
    table.add_row("Would update (changed at source)", str(result.updated))
    table.add_row("Would delete (gone from source)", str(result.deleted))
    table.add_row("Would forward (invited events)", str(result.forwarded))
    table.add_row("Skip (unchanged)", str(result.skipped))
    table.add_row("Cannot migrate", str(len(result.failed)))
    console.print(table)
    _print_details(result)


def _print_report(result: MigrationResult) -> None:
    table = Table(title="Migration report")
    table.add_column("Outcome")
    table.add_column("Events", justify="right")
    table.add_row("Created", str(result.created))
    table.add_row("Updated", str(result.updated))
    table.add_row("Deleted", str(result.deleted))
    table.add_row("Forwarded (invited events)", str(result.forwarded))
    table.add_row("Skipped", str(result.skipped))
    table.add_row("Failed", str(len(result.failed)))
    console.print(table)
    _print_details(result)


def _print_details(result: MigrationResult) -> None:
    if result.unmapped_rooms:
        console.print("[yellow]Unmapped rooms (kept verbatim — extend your --room-map):[/yellow]")
        for room in sorted(result.unmapped_rooms):
            console.print(f"  • {room}")
    if result.failed:
        console.print("[red]Failures:[/red]")
        for event_id, message in result.failed:
            console.print(f"  • {event_id}: {message}")


@app.command()
def migrate(
    client_secret: Annotated[
        Path,
        typer.Option(
            "--client-secret",
            envvar="CALMOVE_CLIENT_SECRET",
            help="Path to the OAuth Desktop client_secret.json.",
        ),
    ],
    room_map: Annotated[
        Path | None,
        typer.Option("--room-map", help="JSON map of old → new room resource e-mails."),
    ] = None,
    send_updates: Annotated[
        SendUpdates,
        typer.Option("--send-updates", help="Whether Google notifies attendees."),
    ] = SendUpdates.none,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show the plan only; never write."),
    ] = False,
) -> None:
    """Interactively migrate one calendar from an old workspace to a new one."""
    if not client_secret.is_file():
        console.print(
            f"[red]OAuth client secret not found: {client_secret}[/red]\n"
            "Provide it via --client-secret or the CALMOVE_CLIENT_SECRET environment "
            "variable (create a Desktop OAuth client in the Google Cloud console)."
        )
        raise typer.Exit(code=2)

    rooms = _load_room_map(room_map) if room_map is not None else {}
    store = KeyringTokenStore()

    console.print("[bold]Step 1 — sign in to the old (source) workspace[/bold]")
    source = _build_gateway("source", SOURCE_SCOPES, client_secret, store)
    source_calendars = source.list_calendars()
    source_calendar = prompts.pick_calendar(console, source_calendars, role_label="source")

    console.print("[bold]Step 2 — sign in to the new (destination) workspace[/bold]")
    destination = _build_gateway("destination", DESTINATION_SCOPES, client_secret, store)
    destination_calendars = destination.list_calendars()
    destination_calendar = prompts.pick_calendar(
        console, destination_calendars, role_label="destination", require_writable=True
    )
    source_address = _primary_address(source_calendars)
    forward_address = _primary_address(destination_calendars)
    if forward_address is None:
        console.print(
            "[yellow]No primary calendar found on the destination account — invited "
            "events cannot be forwarded and will be reported as failures.[/yellow]"
        )

    console.print("[bold]Step 3 — pick a date range[/bold]")
    date_range = prompts.prompt_date_range(console)

    plan_request = MigrationRequest(
        source_calendar_id=source_calendar.id,
        destination_calendar_id=destination_calendar.id,
        date_range=date_range,
        dry_run=True,
        send_updates=send_updates.value,
        room_map=rooms,
        source_address=source_address,
        forward_address=forward_address,
    )
    console.print("[bold]Step 4 — dry run[/bold]")
    plan = MigrateCalendarUseCase(source, destination, _SilentReporter(), forwarder=source).execute(
        plan_request
    )
    _print_plan(plan)

    if dry_run:
        console.print("Dry run requested — nothing was written.")
        return

    if plan.created == plan.updated == plan.deleted == plan.forwarded == 0:
        console.print("Nothing to migrate.")
        return

    if not prompts.confirm_migration(console):
        console.print("Aborted — nothing was written.")
        raise typer.Exit(code=1)

    console.print("[bold]Step 5 — migrating[/bold]")
    request = MigrationRequest(
        source_calendar_id=source_calendar.id,
        destination_calendar_id=destination_calendar.id,
        date_range=date_range,
        dry_run=False,
        send_updates=send_updates.value,
        room_map=rooms,
        source_address=source_address,
        forward_address=forward_address,
    )
    result = MigrateCalendarUseCase(
        source, destination, ConsoleReporter(console), forwarder=source
    ).execute(request)
    _print_report(result)


@app.command()
def logout() -> None:
    """Remove the stored source and destination tokens from the OS keychain."""
    store = KeyringTokenStore()
    store.delete("source")
    store.delete("destination")
    console.print("Stored credentials removed for both roles.")


if __name__ == "__main__":
    app()
