# Starting a run from a channel

What turns a message somebody types into a run: what a pipeline declares, what
the compiler writes beside the workflow, and the two ways a listener can hear
it — as an app that was invited, or as a person who is already there. Read this
when a pipeline should be startable by somebody who is not at a terminal.
Attaching to a run that already exists is [run-events.md](run-events.md).

## What a pipeline declares

- Declared with `pipeline.listen_on(prefix=..., into=...)`. **The service is
  optional, and omitting it is the ordinary case.** Being startable is not a
  reason to hold a credential: the listener reads the channel with its own, and
  a run that says nothing there needs none. Such a pipeline declares no `env`,
  so `trigger.missing()` has nothing to refuse and it starts on any machine.
- Name a service — `listen_on(SLACK, ...)` — when the run itself reports back.
  It must be integrated first, and the conversation then comes from that same
  `integrate(thread=)`, never from a second argument here, so the two can never
  disagree about where a run answers. Without a service there is no thread,
  because there is nothing to answer under.
- Refused at composition: a named service that is not integrated, one with
  `listens=False`, a blank prefix, an `into` the pipeline does not declare or
  that is not a string, an `into` that is also the thread, a second listener on
  one service — or a second naming no service, since one prefix starts it.
- The manifest's `service` is `""` when the pipeline reports nowhere. No
  listener reads that field; a prefix is what claims a message.
- `inputs.thread` is in the manifest exactly when a thread was declared, and
  `Trigger.thread_input` is `""` otherwise — a default would hand every run a
  value under a name it never chose.
- `slack_channel` sets `listens=True`; `slack_webhook` does not.
- Compiles to `build/<pipeline_id>.listen.json`, version `1`. Root pipelines
  only — a stage has no run of its own to start.
- Manifest holds: `workflow` (sibling filename), `pipeline`, `description`,
  `workspace_instructions`, `listeners[]` (`service`, `prefix`,
  `inputs.question`, `inputs.thread`), and `requires` (`commands`, `env`) —
  every declared executable, and every env var an integration, MCP server or
  datasource declares, deduplicated by name.
- `requires` exists because preflight is a command, not an artifact: the workflow
  YAML records no declared executable, and names an env var only where an MCP
  server passes one through.
- `ictus-bridge listen [FOLDER]` reads manifests under `FOLDER` recursively. No folder
  answers gates only.
- The listener runs `conductor run <workflow> -i ...`, never `ictus run`. It
  needs the built artifact, not the pipeline source, its config, or the compiler.
- `launch_command` in `interfaces/conductor/control/` builds that argv for both the CLI
  and the listener. `--web-bg` detaches and serves the dashboard.
- **A string input is handed over on `--input-json`, not `-i`.** `-i` runs a
  value through `coerce_value`, which guesses a type — wanted for a number and
  ruinous for a string that looks like one. A Slack `ts` of `1700000000.000200`
  arrived as a float and came back `1700000000.0002`, which matches no message,
  so a run's reports landed at the top of the channel while the sending program
  blamed a deleted message. About one timestamp in ten ends in a zero.
- `launch_command(verbatim=...)` names the inputs that must arrive as the text
  they were given. The listener passes both of its own, since `listen_on`
  refuses an `into` or a thread that is not a string; `ictus run` passes
  whichever the pipeline declared `STRING`, so the engine still coerces the
  rest and an `int` input still gets an int.
- Conductor marks `--input-json` hidden and internal while calling
  `coerce_value` a public contract that must not change, so it is used only
  where the public one would corrupt the value.
  `test_conformance.py::test_a_string_input_survives_the_engine_verbatim` runs
  the installed engine to check it is still honoured — the failure is otherwise
  silent, and reports simply go to the wrong conversation.
- A manifest whose version differs, whose JSON is unreadable, or whose workflow
  is not beside it is skipped with a warning; the other pipelines still serve.
- First matching trigger wins, in sorted-path order. One message, one run.
- Prefix matching skips `*`, `_`, `~` and backticks wherever whitespace is
  allowed: Slack sends `*Bold:*`, and emphasis is in the text an app receives.

## Hearing it as a person rather than as an app

- `ictus-bridge overhear FOLDER --channel C0ABC123` polls `conversations.history`
  with the first of `$SLACK_USER_TOKEN` or `$SLACK_BOT_TOKEN` that is set, and
  says which it used. Either kind will do — a bot token for a channel the bot
  was invited to, a user token for one you are in — and it needs
  `channels:history`, or `groups:history` for a private channel.
- **The difference from `listen` is the transport, not the credential.** This
  asks on a timer; `listen` holds a socket open and is told. So this needs no
  app-level token, no Socket Mode and no event subscription, and nothing has to
  be reachable. The reason it exists is a channel you cannot get a bot into:
  your own credential reads it and nobody has to invite anything.
- **It cannot answer a gate.** An interaction is pushed to the app that posted
  the button and never appears in a channel's history, so no amount of reading
  finds one. `overheard` therefore returns `Asked` and nothing else — the type,
  not a note. A run started this way waits at its
  gates for its dashboard, or for a `listen` running alongside.
- Two commands rather than a flag, for that reason: a `--as-me` on `listen`
  would leave `--allow` and the bot token inert in half of their own command.
- `request_in` in `bridge/slack/requests.py` is the one recogniser both ways in
  call, so a prefix that starts a run over the socket starts the same one here.
  `asked` is now an envelope-unwrapper over it. A history message names no
  channel, so the channel is passed rather than read.
- The folder is required here and optional for `listen`: with no manifests
  `listen` still answers gates, and this would have nothing left to do.
- `--pipeline <id>` points a listener at one of the manifests it found, rather
  than all of them; repeatable, and on `listen` too. A name nothing claims
  stops the listener at startup and says what *was* found — without that, a
  typo or a pipeline never emitted leaves it running and watching for a prefix
  nothing will send. On `listen`, naming one with no folder is refused rather
  than defaulted: there is nowhere to look.
- It is also how two pipelines claiming one prefix is settled. Prefix alone,
  the first in sorted-path order wins and the other never fires, silently.
- **No replay, and the cursor starts at the newest message.** A restart starts
  nothing; `latest` is called once per channel to fix the cursor, and a channel
  whose first poll failed is read from whenever one lands, never from the
  beginning. Starting at zero would launch a fortnight of backlog in parallel,
  each run costing money. The socket loses what arrives while it is down for
  the same reason.
- **Top-level messages only.** `conversations.history` answers with parents, and
  a reply to an old thread never brings its parent back into the window — so a
  request typed inside a thread starts a run over the socket and not here.
- Slack sets the latency floor: `conversations.history` is rate-limited hard
  outside the Marketplace. A 429 is obeyed for exactly the `Retry-After` Slack
  names rather than doubled, and said once; `--every` is the interval when it
  is not throttling. A poll that merely failed leaves the cursor alone and is
  tried again; only a refused credential or an unreadable channel stops it.
- `conversations.history` is read with a GET and query parameters, not the JSON
  POST `api_call` makes. It honours `$SLACK_API_URL` like everything else, so
  `smoke/fake_channel.py` can stand in for the workspace.
- One poll walks at most `MOST_PAGES` pages. Past that the oldest unread
  messages are dropped, and a warning says so — never silently.
