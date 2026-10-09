.RECIPEPREFIX = >
.PHONY: soundcheck emit lint validate run clean

# `validate` is the last step and it is not optional: ictus exists to produce
# Conductor workflows, so a green build that never asked Conductor whether the
# output loads has checked nothing. It was removed once; that is how five
# commits of unloadable YAML shipped green.
soundcheck:
> uv run ruff check .
> uv run ruff format --check .
# `ruff check .` above honours .gitignore, and .gitignore keeps every demo folder
# but `smoke-events` out of the repo — so the line above lints one of the five.
# These are the files somebody copies to write their first pipeline, so they are
# named explicitly, for the same reason the mypy loop below names them: an
# example carrying an unused import teaches that the import is needed.
> uv run ruff check --no-respect-gitignore demo_work
> uv run ruff format --check --no-respect-gitignore demo_work
# `smoke/` is named explicitly because it is not under src or tests. Most of it
# drives a live engine, which `pytest` cannot run and `soundcheck` therefore
# never executes — so type checking is the only gate those files have, and
# leaving it off one meant six errors sat in it unnoticed. `fake_channel.py` is
# the exception and is not exempt: it stands in for Slack rather than driving an
# engine, so `tests/test_stand_ins.py` runs the production reader against it.
# Taking it to be covered by the sentence above is how it came to answer a shape
# ictus could parse and never act on.
> uv run mypy src tests smoke
# Each pipeline folder holds a file called pipeline.py, so mypy sees four modules
# with one name. Checking them a folder at a time keeps the folder names readable
# (a hyphen is not a valid module component, so package-based disambiguation is
# not available) without giving up type checking on the demos.
> for d in demo_work/pipelines/*/; do uv run mypy "$$d"pipeline.py || exit 1; done
> uv run pytest -q
> $(MAKE) emit
> $(MAKE) validate

emit:
> uv run ictus emit ./demo_work/pipelines

lint:
> uv run ictus lint ./demo_work/pipelines

# Every folder's build/, in one pass.
validate:
> uv run ictus validate ./demo_work/pipelines

# WF is the folder name, e.g. `make run WF=smoke-events`. The run works in the
# directory you invoke it from unless the folder's input.md pins a `repo:`.
run:
> uv run ictus run ./demo_work/pipelines/$(WF)

clean:
> rm -rf demo_work/pipelines/*/build
