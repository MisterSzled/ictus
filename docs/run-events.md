# Run events

Side effects attach to a live run over the dashboard WebSocket.

Verified against conductor-cli 0.1.41 (`microsoft/conductor`), and exercised
end to end against two live runs. Version-specific. Fixtures:
`tests/fixtures/run-events-{approved,rejected}.jsonl`.

## Surface

- `/ws` on the run's dashboard. Bidirectional.
- `WebDashboard` subscribes to the engine's `WorkflowEventEmitter` and
  rebroadcasts every event (`web/server.py`, `WebDashboard.__init__`). Same feed
  the dashboard renders.
- An event is `{type, timestamp, data}`.
- A run-scoped `RunRedactor` scrubs payloads before dispatch. Secrets do not
  reach subscribers.
- REST alongside: `/api/state` (history), `/api/gate-status`, `/api/gate-respond`,
  `/api/guidance`, `/api/stop`, `/api/kill`, `/api/resume`, `/api/logs`.

**The socket carries live events only.** It replays nothing on connect, so a
subscriber that attaches to a run already parked at a gate sees nothing and
waits forever. Connect first, then seed from `GET /api/state`, then dedupe on
`(type, timestamp)` — seeding first loses whatever is emitted in between.

Token is needed for the WebSocket handshake and for mutating routes only.
`GET /api/state`, `/api/gate-status`, `/api/info` and `/api/logs` answer with no
token, guarded by Origin/Host alone. History needs no credential; the live
socket and answering both do.

The socket and the JSONL event log carry the same events in the same order —
verified by comparing both for one run — so a fixture recorded from either is
valid for both. The bytes differ: the log escapes non-ASCII, the socket does not.

## Events received

| Event | Carries |
|---|---|
| `gate_presented` | `agent_name`, `prompt`, `options`, `option_details` |
| `gate_resolved` | `agent_name`, `selected_option`, `route`, `additional_input` |
| `route_taken` | `from_agent`, `to_agent` |
| `questions_presented` / `questions_completed` | ask-human steps |
| `workflow_started` / `workflow_completed` / `workflow_failed` | run lifecycle |
| `agent_started` / `agent_completed` / `agent_failed` | step lifecycle |
| `agent_paused`, `agent_timeout`, `agent_retry`, `agent_validation_failed` | step trouble |
| `budget_exceeded` | spend ceiling hit |
| `checkpoint_saved` / `checkpoint_save_failed` | resumability |
| `guidance_received` | mid-run steer |

Also `script_*`, `set_*`, `wait_*`, `mcp_*`, `subworkflow_*`, `parallel_*`,
`for_each_*` per step kind. ~70 types total.

`option_details` entries carry `label`, `value`, `route`, `prompt_for`,
`multiline`, so free text on a choice arrives with the gate.

`agent_started` fires for every step kind, gates and terminals included;
`agent_type` distinguishes them. `workflow_started` carries the whole topology —
`agents`, `routes`, `entry_point`, `run_id`, `yaml_source`.

A human gate's `gate_presented` carries **no `prompt_id`** — that field is on the
questions variant only. Both are emitted from `engine/workflow.py`; the one that
sets `step_type: questions` is the one that carries it. `/api/gate-status`
reports `prompt_id: null` for a waiting gate.

## Messages sent

- `gate_response` — approval, choice, free text via `prompt_for` / `multiline`.
- `dialog_message` / `dialog_decline` — multi-turn conversation. The surface
  ictus's `remediate` node uses via Conductor's `dialog`.
- `iteration_limit_response` — answers "`max_iterations` reached, continue?".

`gate_response` is `{type, agent_name, selected_value, additional_input?,
prompt_id?}`. The field is **`selected_value`**, not `value`.

`additional_input` is sent as a bare string and read back on `gate_resolved` as a
dict keyed by the option's `prompt_for` — send `"no thanks"`, observe
`{"notes": "no thanks"}`. A gate with no `prompt_for` reports `{}`.

A response is rejected unless `agent_name` matches the waiting gate; it is logged,
not applied. `prompt_id` is optional on both sides and only compared when both
have one, so a response that omits it is accepted against any prompt
(`_validate_gate_target`).

## Reaping

A `--web-bg` process exits only when all four hold
(`_maybe_start_grace_timer`, in `web/server.py`):

1. the run is `--web-bg`
2. `_workflow_completed` — root-level `workflow_completed` / `workflow_failed` seen
3. `_connections` is empty — zero WebSocket clients
4. no grace timer already armed

Then `_BG_GRACE_SECONDS = 30`, then `_bg_event` fires and the process exits.
Measured: 31.5s from `workflow_completed` to process exit, with the port closed
and the run record archived.

- A held `/ws` connection never satisfies (3). The process never exits.
- Leaked per run: one process holding `_event_history` (uncapped, retained for
  process life), one TCP listener, one unsettled fleet run record.

**Close the socket on `workflow_completed` / `workflow_failed`.**

- A new connection cancels a pending grace timer.
- `POST /api/kill` sets `_bg_event` directly.
- The timer also arms from `_on_event` on terminal events, so a run nobody
  connected to still shuts down.

## Discovery

- Fleet run record at `~/.conductor/runs/<run_id>.json`: `run_id`, `pid`,
  `workflow_path`, `workflow_name`, `started_at`, `event_log_path`, `port`
  (nullable), `mode`, `checkpoint_dir`.
- On a graceful exit the record is **moved** to
  `~/.conductor/runs/terminal/<run_id>.json`. A killed or crashed run leaves its
  record behind, so a glob of `~/.conductor/runs/*.json` is filtered on the pid —
  never on age.
- Dashboard port defaults to `0` — OS auto-select. Concurrent runs do not collide.
- Auth: per-run `secrets.token_urlsafe(32)`, plus Origin/Host validation on every
  HTTP and WebSocket request.
- Token file `~/.conductor/runs/dashboard-<port>.token`, mode 0600, deleted on
  reap. `CONDUCTOR_GATE_TOKEN` overrides.
- Event log at `$TMPDIR/conductor/conductor-<name>-<ts>-<run_id>.events.jsonl`,
  and named in the run record as `event_log_path` — do not reconstruct it.
- `fleet/summary.py` derives `GateInfo` from the most recent unresolved
  `gate_presented`, cleared on a matching `gate_resolved`.

## Rules

- A node has no `hooks=` field. The engine executes no side effect at a step
  boundary and rejects `workflow.hooks:` outright (`config/schema.py`,
  `_REMOVED_WORKFLOW_FIELDS`).
- A side effect that belongs in the graph is a node: costed against
  `max_iterations`, routed, visible in the dashboard and in `ictus trace`.
- A side effect that cannot be a node is a subscriber, and never enters the
  emitted YAML. Reporting is both: an integration's announcements are steps,
  inserted when the pipeline loads and present in the YAML; what no step can see
  is the watcher's.
- An integration is declared in ictus with `pipeline.integrate(...)`, beside
  `require_mcp` / `require_executable`, and is preflight-checked.
- Event-vocabulary parsing lives in `interfaces/conductor/`, beside `trace.py`.
- Delivery — Slack, Jira, webhook — sits above that and is not Conductor-shaped.

## Starting a run from a message

Its own page, because it answers a different question from the rest of this one:
this page is about attaching to a run that exists, that one about what brings a
run into being. See [starting-from-a-channel.md](starting-from-a-channel.md).
