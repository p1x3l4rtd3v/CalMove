"""CalMove CLI: Typer app and composition root."""

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
from calmove.application.reporting import NullReporter
from calmove.domain.errors import CalMoveError, InvalidConfigError
from calmove.domain.model import CalendarEvent, primary_address
from calmove.infrastructure.config import load_address_map, parse_domain_rules
from calmove.infrastructure.google.auth import (
    DESTINATION_SCOPES,
    SOURCE_SCOPES,
    KeyringTokenStore,
    authenticate,
)
from calmove.infrastructure.google.gateway import GoogleCalendarGateway

from . import prompts
from .prompts import _safe

app = typer.Typer(help="Migrate a Google Calendar between Google Workspaces.")
console = Console()


class SendUpdates(StrEnum):
    none = "none"
    all = "all"
    externalOnly = "externalOnly"


class ConsoleReporter:
    """Prints one line per migrated event during the real run."""

    def __init__(self, console: Console) -> None:
        self._console = console

    @staticmethod
    def _label(event: CalendarEvent) -> str:
        return _safe(event.summary or event.id)

    def event_created(self, event: CalendarEvent) -> None:
        self._console.print(f"[green]created[/green]  {self._label(event)}")

    def event_forwarded(self, event: CalendarEvent) -> None:
        self._console.print(f"[blue]forwarded[/blue] {self._label(event)}")

    def event_updated(self, event: CalendarEvent) -> None:
        self._console.print(f"[cyan]updated[/cyan]  {self._label(event)}")

    def event_deleted(self, source_id: str) -> None:
        self._console.print(f"[magenta]deleted[/magenta]  {_safe(source_id)}")

    def event_skipped(self, event: CalendarEvent) -> None:
        self._console.print(f"[yellow]skipped[/yellow]  {self._label(event)}")

    def event_failed(self, event: CalendarEvent, error: Exception) -> None:
        self._console.print(f"[red]failed[/red]   {self._label(event)}: {_safe(str(error))}")


def _build_gateway(
    role: str, scopes: list[str], client_secret: Path, store: KeyringTokenStore
) -> GoogleCalendarGateway:
    creds = authenticate(role, scopes, client_secret, store)
    service: Any = build("calendar", "v3", credentials=creds)
    return GoogleCalendarGateway(service)


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
        console.print("[yellow]Unmapped rooms (kept verbatim; extend your --room-map):[/yellow]")
        for room in sorted(result.unmapped_rooms):
            console.print(f"  • {_safe(room)}")
    if result.failed:
        console.print("[red]Failures:[/red]")
        for event_id, message in result.failed:
            console.print(f"  • {_safe(event_id)}: {_safe(message)}")


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
    attendee_map: Annotated[
        Path | None,
        typer.Option("--attendee-map", help="JSON map of old → new attendee e-mails."),
    ] = None,
    map_domain: Annotated[
        list[str] | None,
        typer.Option(
            "--map-domain",
            help="Rewrite attendee domains, e.g. old-company.com=new-company.com (repeatable).",
        ),
    ] = None,
    send_updates: Annotated[
        SendUpdates,
        typer.Option("--send-updates", help="Whether Google notifies attendees."),
    ] = SendUpdates.none,
    resend_invitations: Annotated[
        bool,
        typer.Option(
            "--resend-invitations",
            help="Re-write unchanged events so Google sends invitation e-mails "
            "(use with --send-updates all).",
        ),
    ] = False,
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

    try:
        rooms = load_address_map(room_map, "Room map") if room_map is not None else {}
        attendees = (
            load_address_map(attendee_map, "Attendee map") if attendee_map is not None else {}
        )
        domains = parse_domain_rules(map_domain or [])
    except InvalidConfigError as error:
        console.print(f"[red]{_safe(str(error))}[/red]")
        raise typer.Exit(code=1) from error

    try:
        _run_wizard(
            client_secret,
            rooms=rooms,
            attendees=attendees,
            domains=domains,
            send_updates=send_updates,
            resend_invitations=resend_invitations,
            dry_run=dry_run,
        )
    except CalMoveError as error:
        console.print(f"[red]{_safe(str(error))}[/red]")
        raise typer.Exit(code=1) from error


def _run_wizard(
    client_secret: Path,
    *,
    rooms: dict[str, str],
    attendees: dict[str, str],
    domains: dict[str, str],
    send_updates: SendUpdates,
    resend_invitations: bool,
    dry_run: bool,
) -> None:
    store = KeyringTokenStore()

    console.print("[bold]Step 1: sign in to the old (source) workspace[/bold]")
    source = _build_gateway("source", SOURCE_SCOPES, client_secret, store)
    source_calendars = source.list_calendars()
    source_calendar = prompts.pick_calendar(console, source_calendars, role_label="source")

    console.print("[bold]Step 2: sign in to the new (destination) workspace[/bold]")
    destination = _build_gateway("destination", DESTINATION_SCOPES, client_secret, store)
    destination_calendars = destination.list_calendars()
    destination_calendar = prompts.pick_calendar(
        console, destination_calendars, role_label="destination", require_writable=True
    )
    source_address = primary_address(source_calendars)
    # Invitations are forwarded to the chosen destination calendar's address: the account
    # itself for a primary-calendar migration, the group calendar for a team migration.
    forward_address = destination_calendar.id
    mirror_rsvp = destination_calendar.primary  # calendars don't RSVP; people do
    if not mirror_rsvp:
        console.print(
            "[yellow]Group-calendar destination: invited events will be forwarded to "
            f"'{destination_calendar.id}' and no RSVP is mirrored.[/yellow]"
        )

    console.print("[bold]Step 3: pick a date range[/bold]")
    date_range = prompts.prompt_date_range(console)

    plan_request = MigrationRequest(
        source_calendar_id=source_calendar.id,
        destination_calendar_id=destination_calendar.id,
        date_range=date_range,
        dry_run=True,
        send_updates=send_updates.value,
        room_map=rooms,
        attendee_map=attendees,
        domain_map=domains,
        source_address=source_address,
        forward_address=forward_address,
        mirror_rsvp=mirror_rsvp,
        resend_invitations=resend_invitations,
    )
    console.print("[bold]Step 4: dry run[/bold]")
    plan = MigrateCalendarUseCase(
        source, destination, NullReporter(), forwarder=source, responder=destination
    ).execute(plan_request)
    _print_plan(plan)

    if dry_run:
        console.print("Dry run requested; nothing was written.")
        return

    if plan.created == plan.updated == plan.deleted == plan.forwarded == 0 and not (
        resend_invitations and plan.skipped
    ):
        if not plan.failed:
            console.print("Nothing to migrate.")
            return
        console.print(
            f"[yellow]Nothing can be migrated automatically: {len(plan.failed)} "
            "item(s) need attention (see the failures above).[/yellow]"
        )
        raise typer.Exit(code=1)

    if not prompts.confirm_migration(console):
        console.print("Aborted; nothing was written.")
        raise typer.Exit(code=1)

    console.print("[bold]Step 5: migrating[/bold]")
    request = MigrationRequest(
        source_calendar_id=source_calendar.id,
        destination_calendar_id=destination_calendar.id,
        date_range=date_range,
        dry_run=False,
        send_updates=send_updates.value,
        room_map=rooms,
        attendee_map=attendees,
        domain_map=domains,
        source_address=source_address,
        forward_address=forward_address,
        mirror_rsvp=mirror_rsvp,
        resend_invitations=resend_invitations,
    )
    result = MigrateCalendarUseCase(
        source, destination, ConsoleReporter(console), forwarder=source, responder=destination
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
