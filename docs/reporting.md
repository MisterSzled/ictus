# Reporting out of a run

Read this when a run has to tell somebody what it is doing — a channel, a
ticket — or when you are wiring `announce` by hand instead of letting
`pipeline.integrate(...)` place it.

Most pipelines never call `announce`. `pipeline.integrate(service)` attaches a
destination when the pipeline is loaded — one line to add a service, one line to
remove it, and nothing about tokens or threads in the composition. Reach for
`announce` when one point deserves one particular sentence.

What `integrate` attaches, after the start policy so the start gate is
included. A service names the moments it wants in `reports=`; one that names
none is attached by hand and gets none of this:

- an opener, for a service that threads, which the whole run hangs under; one
  without threads opens only if it asked for `RUN_STARTED`;
- for `DECISION_NEEDED`, an announcement in front of every gate and every
  question, in the pipeline and in every stage nested in it — a gate's own
  choices as its buttons;
- for `RUN_FINISHED` and `RUN_FAILED`, an announcement in front of every way
  the top-level graph ends: each explicit exit, and every route to `END`.

Each announcement reads what the node it stands in front of reads, so a prompt
renders in the channel as it does in the dashboard. A stage gets the thread as
an optional parameter, added when it has something to announce. An explicit
`max_iterations` is raised by exactly the steps added; a loop with no
`loop_passes` to price them by is refused at load.

`to` is an `Integration`, built by a constructor under `ictus.notify` —
`slack_channel` or `slack_webhook`, imported from `ictus.notify.slack`, today.
Nothing in `graph/` or `stdlib/` knows
which service it is: the integration carries an opaque program that sends one
report, and a second destination is a new module under `notify` and no change
anywhere else. `test_no_service_is_named_outside_an_adapter` is what
keeps that true.

A report never fails the run. The step always succeeds; one that could not
deliver says `posted: "false"` and leaves its reason on stderr, where the
dashboard shows it; `ictus trace` lists the step, not its stderr. A channel
being down says nothing about whether the work succeeded. What a step cannot
report is what no step can see — a budget tripping, a step failing, the engine
being killed — which is what `ictus watch` is for, and preflight says so to an
integration that asks for one.

`answers=<gate>` puts that gate's choices in as buttons, read off the gate: a
renamed option cannot leave a button that answers nothing, and no button can
carry a value the gate does not offer. Buttons and threads both need a service
that can carry an answer back; `slack_webhook` cannot, and says so at
composition.
