"""``ictus-bridge`` — answer gates, and start runs, from a chat service.

Its own command rather than a verb on ``ictus``: every ``ictus`` verb
finishes, and these two wait.

Two verbs rather than a flag on one, because the credential decides what is
possible and not merely how it is fetched. ``listen`` is an app being told,
and answers gates. ``overhear`` is you reading a channel you are in, and
cannot: no user token receives a button press. A flag would have left
``--allow`` and the bot token inert in half its own command.

Nothing in ``ictus`` imports this module, so the edge only runs one way.
"""

from __future__ import annotations

import os
from collections.abc import Collection  # noqa: TC003
from concurrent.futures import ThreadPoolExecutor

# Runtime, not a type-checking block: typer resolves a command's annotations
# at import to build the parser.
from pathlib import Path  # noqa: TC003
from typing import Annotated

import typer

from ictus.bridge.slack.errors import SlackError
from ictus.bridge.slack.listen import (
    Click,
    Note,
    open_form,
    presses,
    retire,
    say,
    verdict,
)
from ictus.bridge.slack.watch import POLL_SECONDS, overheard
from ictus.notify.slack.api import reply
from ictus.runs.answer import resolve, submit
from ictus.runs.launch import Asked, Started, start
from ictus.runs.triggers import Trigger, triggers_in

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Drive ictus runs from a chat service: answer gates, and start runs.",
    # Typer prints every local of every frame on an uncaught exception, and
    # every frame below here is holding both Slack tokens.
    pretty_exceptions_show_locals=False,
)


def _fail(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


APP_TOKEN_ENV = "SLACK_APP_TOKEN"
#: Replies under the question, asks for a choice's text, and retires buttons.
BOT_TOKEN_ENV = "SLACK_BOT_TOKEN"

#: Reads a channel, and replies in the thread as whoever owns the token.
#:
#: Either kind works, and which you hold is not what this command is about:
#: `conversations.history` takes a bot token for a channel the bot was invited
#: to, or a user token for one you are in. What `overhear` avoids is Socket
#: Mode — no app-level token, no event subscriptions, nothing to reach it.
#: Tried in order, so a user token set on purpose wins; which one was used is
#: printed, since the two behave differently in whose name things are said.
READ_TOKEN_ENVS = ("SLACK_USER_TOKEN", "SLACK_BOT_TOKEN")
#: The channel `overhear` watches when none is named. The same variable a
#: pipeline's `slack_channel` posts into, so one export usually serves both.
CHANNEL_ENV = "SLACK_CHANNEL"

#: Presses handled at once. The socket thread only acknowledges and hands on.
LISTEN_WORKERS = 4


@app.callback()
def main() -> None:
    """Typer collapses a single-command app into a bare one, which made
    `ictus-bridge listen` parse `listen` as the folder to watch rather than as a
    verb — identical output to `ictus-bridge` with no arguments, and six places
    in the documentation saying otherwise. A second command makes that
    impossible now, and the callback stays so adding or removing one never
    silently changes how the first is parsed."""


@app.command()
def listen(
    where: Annotated[
        Path | None,
        typer.Argument(help="A built pipeline folder, or a directory of them, to start runs from"),
    ] = None,
    allow: Annotated[
        list[str] | None,
        typer.Option("--allow", help="Slack user id that may answer; repeatable"),
    ] = None,
    pipeline: Annotated[
        list[str] | None,
        typer.Option(
            "--pipeline", help="Pipeline id to start; repeatable. Default: every one found"
        ),
    ] = None,
) -> None:
    """Answer gates from Slack, by listening for button presses.

    Opens a websocket outward to Slack, so nothing here has to be publicly
    reachable. Reads $SLACK_APP_TOKEN and $SLACK_BOT_TOKEN.

    A press is answered on the run that posted the button, and only if that
    message is the newest time the question was asked. A choice that asks for
    text opens a form for it. What happened is posted back where the button
    was, including when nothing happened, and an answered question loses its
    buttons.

    The connection is redialled whenever it drops; only Slack refusing the
    token stops it.

    Without `--allow`, anyone who can see the button may answer.
    """
    token = os.environ.get(APP_TOKEN_ENV)
    bot = os.environ.get(BOT_TOKEN_ENV, "")
    if not token:
        _fail(
            f"${APP_TOKEN_ENV} is not set. Enable Socket Mode on the Slack app, generate "
            "an app-level token with connections:write, and export it."
        )
        return
    if not bot:
        _fail(
            f"${BOT_TOKEN_ENV} is not set. Without it a choice that needs text cannot ask "
            "for it, nothing can be said in the thread, and an answered question keeps "
            "its buttons. Export the same bot token the pipeline posts with."
        )
        return
    permitted = frozenset(allow or ())
    if pipeline and where is None:
        _fail(
            f"--pipeline {', '.join(pipeline)} names what to start, and no folder says "
            "where to find it. Pass the built pipeline folder too, or drop --pipeline to "
            "answer gates only."
        )
        return
    watching = triggers_in(where) if where is not None else []
    if where is not None and not watching:
        _fail(
            f"no manifests under {where}. A pipeline is startable from a channel once it "
            "declares `listen_on(...)` and has been emitted; without that this would "
            "listen for a prefix nothing claims."
        )
    watching, why = only(watching, pipeline or ())
    if why:
        _fail(why)
    typer.secho(
        "listening for button presses"
        + (f"; only {', '.join(sorted(permitted))} may answer" if permitted else ""),
        fg=typer.colors.CYAN,
    )
    _announce(watching)
    seen: set[str] = set()
    with ThreadPoolExecutor(max_workers=LISTEN_WORKERS) as pool:
        try:
            for event in presses(token, triggers=watching):
                if isinstance(event, Asked):
                    _ask_once(event, seen, pool, bot)
                    continue
                pool.submit(_handle_press, event, permitted, bot)
        except KeyboardInterrupt:
            typer.secho("\nstopped listening; the runs are untouched", fg=typer.colors.BRIGHT_BLACK)
        except SlackError as exc:
            _fail(str(exc))


def only(watching: list[Trigger], wanted: Collection[str]) -> tuple[list[Trigger], str]:
    """The triggers for the named pipelines, and why not when a name claims none.

    Pointing at a folder and naming a pipeline is not the same as pointing at
    that pipeline's own folder. The name is checked against what was actually
    found, because the alternative is silent: a typo, or a pipeline that was
    never emitted, would otherwise leave a listener running happily and
    watching for a prefix nothing will ever send.

    Naming nothing keeps everything, which is what one listener serving a whole
    folder has always done.
    """
    if not wanted:
        return watching, ""
    found = {trigger.pipeline for trigger in watching}
    unknown = sorted(set(wanted) - found)
    if unknown:
        return [], (
            f"no pipeline called {', '.join(unknown)} here. Found: "
            f"{', '.join(sorted(name for name in found if name)) or 'nothing'}. "
            "A pipeline is startable once it declares `listen_on(...)` and has been emitted."
        )
    return [trigger for trigger in watching if trigger.pipeline in wanted], ""


def _announce(watching: list[Trigger]) -> None:
    """What this listener will start, and what this machine cannot supply."""
    for trigger in watching:
        typer.secho(
            f'starting {trigger.pipeline or trigger.workflow.name} on "{trigger.prefix} ..."',
            fg=typer.colors.CYAN,
        )
        # At startup, not at the first message.
        for gap in trigger.missing():
            typer.secho(f"  warn  {gap}", fg=typer.colors.YELLOW)


@app.command()
def overhear(
    where: Annotated[
        Path,
        typer.Argument(help="A built pipeline folder, or a directory of them, to start runs from"),
    ],
    channel: Annotated[
        list[str] | None,
        typer.Option(
            "--channel", help="Channel id to read; repeatable. Defaults to $SLACK_CHANNEL"
        ),
    ] = None,
    every: Annotated[
        float,
        typer.Option("--every", help="Seconds between polls, when Slack allows them that often"),
    ] = POLL_SECONDS,
    pipeline: Annotated[
        list[str] | None,
        typer.Option(
            "--pipeline", help="Pipeline id to start; repeatable. Default: every one found"
        ),
    ] = None,
) -> None:
    """Start runs from a channel, by reading it rather than being told.

    Polls `conversations.history` with the first of $SLACK_USER_TOKEN or
    $SLACK_BOT_TOKEN that is set. Either kind will do — a bot token for a
    channel the bot is in, a user token for one you are in — and it needs
    channels:history, or groups:history for a private channel.

    What this avoids is Socket Mode: no app-level token, no event
    subscriptions, and nothing that has to be reachable. A channel you cannot
    get a bot into can be read with your own credential; a channel the bot is
    already in needs no credential of yours.

    What it cannot do is answer a gate. A press reaches the app that posted the
    button and no user token subscribes to one, so a run started this way waits
    at its gates for its dashboard, or for `ictus-bridge listen`.

    Listening starts at the newest message, so what was said before this was
    running starts nothing and a restart replays nothing. Top-level messages
    only: a request typed inside a thread is not seen.

    A folder is required, unlike `listen` — with no manifests there would be
    nothing left for this to do.
    """
    held = [name for name in READ_TOKEN_ENVS if os.environ.get(name)]
    if not held:
        _fail(
            f"none of {', '.join('$' + name for name in READ_TOKEN_ENVS)} is set. Either "
            "kind will do: a bot token for a channel the bot is in, or a user token for "
            "one you are in. It needs channels:history — groups:history for a private "
            "channel — under the matching Scopes heading, and a scope added after the app "
            "was installed is not granted until you reinstall."
        )
        return
    using = held[0]
    token = os.environ[using]
    named = channel or [os.environ.get(CHANNEL_ENV, "")]
    channels = [one.strip() for one in named if one.strip()]
    if not channels:
        _fail(
            f"no channel to read. Pass --channel, or export ${CHANNEL_ENV}. A channel id "
            "looks like C0ABC123 and is at the bottom of View channel details. Unlike a "
            "socket, this has to be told where to look."
        )
        return
    watching = triggers_in(where)
    if not watching:
        _fail(
            f"no manifests under {where}. A pipeline is startable from a channel once it "
            "declares `listen_on(...)` and has been emitted; without that this would read "
            "a channel for a prefix nothing claims."
        )
    watching, why = only(watching, pipeline or ())
    if why:
        _fail(why)
    typer.secho(
        f"reading {', '.join(channels)} with ${using}, every {every:g}s — starting from "
        "what is said next; gates are not answerable from here",
        fg=typer.colors.CYAN,
    )
    _announce(watching)
    seen: set[str] = set()
    with ThreadPoolExecutor(max_workers=LISTEN_WORKERS) as pool:
        try:
            for request in overheard(token, channels=channels, triggers=watching, every=every):
                _ask_once(request, seen, pool, token)
        except KeyboardInterrupt:
            typer.secho("\nstopped reading; the runs are untouched", fg=typer.colors.BRIGHT_BLACK)
        except SlackError as exc:
            _fail(str(exc))


def _ask_once(request: Asked, seen: set[str], pool: ThreadPoolExecutor, token: str) -> None:
    """Start a run for one request, unless its thread already started one.

    Both ways in can deliver the same ask twice and for different reasons:
    Slack redelivers what it thinks was not acknowledged, and one poll's window
    can overlap the last one's. Either reads exactly like somebody asking
    twice, and two copies of the rule is where that drifts apart — the two
    verbs stay two verbs, but this they share.
    """
    if request.thread in seen or request.trigger is None:
        return
    seen.add(request.thread)
    pool.submit(_handle_ask, request, token)


def became_of(request: Asked, started: Started) -> str:
    """The line posted back where the request was made.

    Public and separate because it has to be checked, not only read: `overhear`
    posts it as the person listening, so unlike `listen`'s bot message it comes
    back as an ordinary message on the next poll. Nothing marks it as the
    listener's own — it starts no second run only because it never begins with
    a prefix, which is a property of this wording and so belongs in a test.
    """
    line = (
        f"Working on it — <@{request.who}> asked about *{request.question[:120]}*"
        if started.ok
        else f"Could not start: {started.why}"
    )
    if started.dashboard:
        # The only moment anybody can learn it: the port is assigned when the
        # run binds.
        line += f"\n{started.dashboard}"
    return line


def _handle_ask(request: Asked, token: str) -> None:
    """Start a run for one request, and say in its thread what became of it.

    ``token`` posts that line: the bot's when an app is listening, the asker's
    own when they are. Either way it is the only place the dashboard's address
    can be learned.
    """
    typer.echo(f"  ask from {request.who}: {request.question[:60]}")
    if request.trigger is None:  # pragma: no cover - the caller already checked
        return
    started = start(request, request.trigger)
    line = became_of(request, started)
    typer.secho(
        f"    -> {line.splitlines()[0]}",
        fg=typer.colors.BRIGHT_BLACK if started.ok else typer.colors.RED,
    )
    if started.dashboard:
        typer.secho(f"    -> {started.dashboard}", fg=typer.colors.CYAN)
    said = reply(token=token, channel=request.channel, thread_ts=request.thread, text=line)
    if said:
        typer.secho(f"    -> could not say so in the thread: {said}", fg=typer.colors.RED)


def _handle_press(event: Click | Note, permitted: frozenset[str], bot: str) -> None:
    """Answer one press or one submitted form, and say what became of it.

    Runs on a worker thread, so every failure is printed rather than raised.
    """
    click = event.click if isinstance(event, Note) else event
    try:
        if isinstance(event, Note):
            outcome = submit(event.click.press, event.text, allowed=permitted)
        else:
            outcome = resolve(event.press, allowed=permitted)
            if outcome.needs_note:
                why = open_form(bot, event)
                if why:
                    say(
                        event,
                        verdict(
                            event, answered=False, reason=f"could not ask for {event.ask}: {why}"
                        ),
                        token=bot,
                    )
                return
        line = verdict(
            click, answered=outcome.answered, reason=outcome.reason, run_id=outcome.run_id
        )
        typer.echo(f"  {outcome.run_id or '-'} {click.gate}={click.choice} -> {line}")
        say(click, line, token=bot)
        if outcome.answered:
            why = retire(bot, click, line)
            if why:
                typer.secho(f"  could not take the buttons off: {why}", fg=typer.colors.YELLOW)
    except Exception as exc:
        typer.secho(
            f"  {click.gate}={click.choice}: {type(exc).__name__} while answering: {exc}",
            fg=typer.colors.RED,
            err=True,
        )


if __name__ == "__main__":
    app()
