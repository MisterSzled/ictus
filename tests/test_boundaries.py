"""The edges ictus is not allowed to acquire, asserted rather than documented.

Three rules:

* **No service above the adapters.**
* **Nothing in ictus imports the bridge**, which ships in the same wheel but
  must stay separable.
* **The adapters stay leaves**: nothing in ictus imports a constructor by name.
* **No engine above the engine boundary.** ``interfaces/conductor`` is the only
  package allowed to know Conductor's spelling or to build its shapes.

Tokenising rather than reading lines, so prose naming a service is fine and an
identifier is not.
"""

from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "ictus"

#: The two boundary package roots. Everything *under* them may name a service —
#: that is what an adapter is — and these two files may not, which is what makes
#: them boundaries rather than folders. Named rather than left to an exclusion
#: list, so a third adapter folder needs no edit here and a flat re-export into
#: either root is caught the day somebody adds one.
BOUNDARY_ROOTS = ("notify/__init__.py", "sources/__init__.py")

#: Where a service may be named. ``notify/<service>`` and ``sources/<service>``
#: are the adapters; ``bridge`` is the daemon.
ADAPTER_TREES = ("notify/", "sources/")
ALLOWED_TREES = ("bridge/",)

VENDOR = (
    "slack",
    "thread_ts",
    "chat.postmessage",
    "xoxb",
    "block kit",
    # Every destination and every source, not only the first one written. A
    # boundary that only knows the service it was built for stops being a rule
    # and becomes a note about history.
    "jira",
    "atlassian",
    "adf",
    "postgres",
    "psql",
    "pgoptions",
    "sqlite",
    "dsn",
)


def _is_adapter(where: str) -> bool:
    """Under an adapter tree, but not the tree's own ``__init__``."""
    return any(where.startswith(tree) for tree in ADAPTER_TREES) and where not in BOUNDARY_ROOTS


def _core() -> list[Path]:
    """Every module that is ictus proper — not an adapter, not the bridge.

    The two boundary roots are *in* this list on purpose. They sit inside the
    adapter trees and are still held to the rule, because a flat re-export in
    either is exactly how a boundary stops being one.
    """
    return sorted(
        path
        for path in SRC.rglob("*.py")
        if not _is_adapter(where := path.relative_to(SRC).as_posix())
        and not any(where.startswith(tree) for tree in ALLOWED_TREES)
    )


@pytest.mark.parametrize("root", BOUNDARY_ROOTS)
def test_a_boundary_root_names_no_service(root: str) -> None:
    """``ictus.notify`` and ``ictus.sources`` are the two files the rule is about.

    A pipeline imports ``ictus.notify.slack``; nothing imports a service *from*
    ``ictus.notify``. The flat re-export is the tempting change here, and it
    would put every destination's spelling into the one file whose job is not to
    have it — so the rule is stated about these two by name rather than left to
    an exclusion list that happened not to cover them.
    """
    path = SRC / root
    assert path.is_file(), f"{root} moved; this rule is now asserting nothing"
    offending = [
        f"{root}:{line}: {name}"
        for name, line in _code_of(path)
        if any(word in name.lower() for word in VENDOR)
    ]
    assert not offending, "a service named in a boundary root:\n" + "\n".join(offending)


#: Everything that is prose rather than code. ``FSTRING_MIDDLE`` is the reason
#: this is a named set: since 3.12 an f-string is tokenised in pieces and its
#: literal text is *not* a ``STRING``, so a docstring-style sentence inside one
#: read as an identifier. That cost four false positives the day the engine rule
#: was added, and it would have read as the rule being wrong rather than the
#: tokeniser being incomplete.
NOT_CODE = (
    tokenize.COMMENT,
    tokenize.STRING,
    tokenize.FSTRING_MIDDLE,
    tokenize.NL,
    tokenize.NEWLINE,
)


def _code_of(path: Path) -> list[tuple[str, int]]:
    """Every token that is code, with its line. Comments and strings dropped."""
    kept: list[tuple[str, int]] = []
    with path.open("rb") as handle:
        for token in tokenize.tokenize(io.BytesIO(handle.read()).readline):
            if token.type in NOT_CODE:
                continue
            if token.string.strip():
                kept.append((token.string, token.start[0]))
    return kept


def _imports(path: Path) -> list[tuple[str, int]]:
    """Every module this one imports, with the line it is imported on."""
    tree = ast.parse(path.read_bytes())
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(alias.name, node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.append((node.module, node.lineno))
    return found


def test_there_is_something_to_check() -> None:
    """A glob that matches nothing passes every test below it."""
    assert len(_core()) > 40


@pytest.mark.parametrize("path", _core(), ids=lambda p: p.relative_to(SRC).as_posix())
def test_no_service_is_named_outside_an_adapter(path: Path) -> None:
    offending = [
        f"{path.relative_to(SRC)}:{line}: {name}"
        for name, line in _code_of(path)
        if any(word in name.lower() for word in VENDOR)
    ]
    assert not offending, "a service's spelling in ictus proper:\n" + "\n".join(offending)


@pytest.mark.parametrize("path", _core(), ids=lambda p: p.relative_to(SRC).as_posix())
def test_nothing_in_ictus_imports_the_bridge(path: Path) -> None:
    """The edge is one-way: the bridge may import ictus, never the reverse."""
    offending = [
        f"{path.relative_to(SRC)}:{line}: {module}"
        for module, line in _imports(path)
        if module == "ictus.bridge" or module.startswith("ictus.bridge.")
    ]
    assert not offending, (
        "ictus imported its own bridge, which is a daemon and not part of it:\n"
        + "\n".join(offending)
    )


#: The constructors that name a service. Importing one *by name* is what makes a
#: module know which service there is; ``Integration`` and ``Datasource`` are the
#: shapes and are imported freely.
ADAPTERS = (
    "slack_channel",
    "slack_webhook",
    "jira_cloud",
    "readonly_jira",
    "readonly_postgres",
    "readonly_postgres_fleet",
    "readonly_sqlite",
)


@pytest.mark.parametrize("path", _core(), ids=lambda p: p.relative_to(SRC).as_posix())
def test_no_adapter_is_imported_by_ictus_proper(path: Path) -> None:
    """A pipeline names its service; the library that compiles it does not."""
    tree = ast.parse(path.read_bytes())
    offending = [
        f"{path.relative_to(SRC)}:{node.lineno}: {alias.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name in ADAPTERS
    ]
    assert not offending, "ictus reached for a concrete service:\n" + "\n".join(offending)


# --- the engine boundary ---------------------------------------------------------

#: Packages that model a pipeline without knowing what will run it. The rule
#: AGENTS.md states about them was prose until two things had already slipped
#: through: ``render_output_schema``, whose docstring said "lower to Conductor's
#: ``output:`` block" while living in ``graph/node.py``, and ``add_subworkflow``,
#: using the engine's noun for a thing the graph itself calls a sub-graph.
ENGINE_FREE = (
    "graph",
    "stdlib",
    "lint",
    "runspec",
    "assemble",
    # Below or beside the backend rather than above it, and engine-free for a
    # different reason: an adapter is data a pipeline declares, `net` is
    # protocol with no ictus in it, and `bridge` is a service's users driving
    # runs on whatever happens to be executing them. Each said so in its own
    # docstring and was held by nothing — `bridge/slack/listen.py` promises
    # "nothing here knows which engine runs a gate", which is exactly the kind
    # of claim that stops being true quietly.
    "notify",
    "sources",
    "bridge",
    "net",
    "plugins",
    "prompting",
)
#: Not `runs` or `cli`: the layering puts both downstream of `interfaces`
#: (`interfaces <- runs <- bridge`, and everything <- cli), so naming the engine
#: there is the design rather than a leak. What holds them is that they reach it
#: only through `interfaces.conductor` — never by spelling a flag or a field.

#: Conductor's own spelling. Deliberately only words with no other meaning —
#: ``wait``, ``script`` and ``set`` are Conductor step types *and* ordinary
#: English, so including them would catch ``stdlib/steps/wait.py`` and teach
#: everyone to ignore the rule.
ENGINE_WORDS = (
    "subworkflow",
    "agentdef",
    "human_gate",
    "for_each",
    "web_bg",
    "gate_presented",
    "gate_resolved",
    "workflow_completed",
    "conductor",
)


def _engine_free() -> list[Path]:
    return sorted(p for pkg in ENGINE_FREE for p in (SRC / pkg).rglob("*.py"))


def test_there_are_engine_free_packages_to_check() -> None:
    assert len(_engine_free()) > 30


@pytest.mark.parametrize("path", _engine_free(), ids=lambda p: p.relative_to(SRC).as_posix())
def test_no_engine_spelling_above_the_backend(path: Path) -> None:
    """A Conductor field name here is a defect with a name, not a style preference."""
    offending = [
        f"{path.relative_to(SRC)}:{line}: {name}"
        for name, line in _code_of(path)
        if any(word in name.lower() for word in ENGINE_WORDS)
    ]
    assert not offending, "the engine's vocabulary above interfaces/conductor:\n" + "\n".join(
        offending
    )


@pytest.mark.parametrize("path", _engine_free(), ids=lambda p: p.relative_to(SRC).as_posix())
def test_nothing_above_the_backend_builds_a_yaml_document(path: Path) -> None:
    """Returning ``YamlDict`` is lowering, and lowering is the backend's job.

    ``graph/values.py`` defines the type, because a pipeline carries values of
    it. A *function* assembling one from ports or nodes is building Conductor's
    document shape, which is how ``render_output_schema`` came to sit in the
    composition model with a docstring admitting exactly that.
    """
    if path.name == "values.py":
        return
    tree = ast.parse(path.read_bytes())
    offending = [
        f"{path.relative_to(SRC)}:{node.lineno}: {node.name}"
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and isinstance(node.returns, ast.Name)
        and node.returns.id in ("YamlDict", "YamlValue")
    ]
    assert not offending, "a YAML document built above interfaces/conductor:\n" + "\n".join(
        offending
    )


# --- the analysis never reaches into a pipeline ----------------------------------

TRAVERSAL = SRC / "graph" / "traversal.py"


def test_the_analysis_never_reaches_into_a_pipeline() -> None:
    """``graph/traversal.py`` gets a graph through its public accessors or not at all.

    That is the rule the split was for: these used to be methods, so each one
    could read ``self._edges`` and nothing said it should not. Eight of them
    did. A function that cannot touch a private store cannot accidentally
    depend on an ordering the stores happen to have, and is the one kind of
    code you can read without checking whether it mutates.

    ``ast.Attribute`` only, deliberately. Widening this to ``ast.Name`` was
    tried and fires on the five ``_End`` reads the isinstance checks need and
    on ``_span`` in ``loop_cost``'s destructuring — on everything except the
    hazard. A rule needing exemptions on its first day is one more thing to
    drift.
    """
    tree = ast.parse(TRAVERSAL.read_bytes())
    offending = [
        f"traversal.py:{node.lineno}: .{node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr.startswith("_")
        and not node.attr.startswith("__")
    ]
    assert not offending, "the analysis reaching past a public accessor:\n" + "\n".join(offending)


# --- the public surface is stated, not inferred ----------------------------------

#: Modules where ``__all__`` would be noise. A command module's surface is the
#: commands it registers on the Typer app by decorator, and `__main__` has none.
NO_SURFACE = ("cli/", "bridge/cli.py", "__main__.py")


def _library_modules() -> list[Path]:
    return sorted(
        p
        for p in SRC.rglob("*.py")
        if p.name != "__init__.py"
        and not any(part in p.relative_to(SRC).as_posix() for part in NO_SURFACE)
    )


@pytest.mark.parametrize("path", _library_modules(), ids=lambda p: p.relative_to(SRC).as_posix())
def test_every_module_states_its_public_surface(path: Path) -> None:
    """``__all__`` everywhere, so what a module offers is a decision not a leftover.

    71 of 77 modules already had one; the six that did not were ``errors`` and
    five of ``graph``, which is to say the core public API — the modules where
    the surface matters most and the convention was least visible.
    """
    tree = ast.parse(path.read_bytes())
    stated = any(
        isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
        for node in tree.body
    )
    assert stated, f"{path.relative_to(SRC)} defines no __all__"


# --- emission does not reach for a running engine --------------------------------

EMIT = SRC / "interfaces" / "conductor" / "emit"
CONTROL_PREFIX = "ictus.interfaces.conductor.control"


def test_there_is_an_emit_package() -> None:
    assert list(EMIT.glob("*.py"))


@pytest.mark.parametrize(
    "path", sorted(EMIT.glob("*.py")), ids=lambda p: p.relative_to(SRC).as_posix()
)
def test_emission_never_imports_run_control(path: Path) -> None:
    """`ictus emit` has to work with no credentials, no network and no engine.

    That is the whole reason the two are separate packages. One import this way
    and compiling a pipeline starts depending on a machine that can run one —
    which is exactly how they came to share a package in the first place.
    """
    offending = [
        f"{path.relative_to(SRC)}:{line}: {module}"
        for module, line in _imports(path)
        if module.startswith(CONTROL_PREFIX)
    ]
    assert not offending, "emission reached for a live run:\n" + "\n".join(offending)


# --- __all__ says what the module actually offers ---------------------------------


def _src_imports() -> dict[str, set[str]]:
    """What each ictus module imports from each other ictus module."""
    found: dict[str, set[str]] = {}
    for path in SRC.rglob("*.py"):
        for module, _ in _imports(path):
            if module.startswith("ictus."):
                found.setdefault(module, set())
        for node in ast.walk(ast.parse(path.read_bytes())):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if module.startswith("ictus."):
                found.setdefault(module, set()).update(alias.name for alias in node.names)
    return found


def _declared(path: Path) -> list[str] | None:
    """A module's ``__all__``, or ``None`` when it has none."""
    for node in ast.parse(path.read_bytes()).body:
        if not isinstance(node, ast.Assign):
            continue
        names = any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
        if names and isinstance(node.value, ast.List):
            return [
                e.value
                for e in node.value.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            ]
    return None


def _module_name(path: Path) -> str:
    dotted = "ictus." + path.relative_to(SRC).with_suffix("").as_posix().replace("/", ".")
    return dotted.removesuffix(".__init__").rstrip(".")


@pytest.mark.parametrize("path", _library_modules(), ids=lambda p: p.relative_to(SRC).as_posix())
def test_all_declares_what_other_modules_import(path: Path) -> None:
    """``__all__`` understating the surface is a comment that disagrees with the code.

    Only names another module *in ictus* imports: a test reaching for an
    internal is reaching for an internal, and should not drag it into the
    public surface. Submodule names are excluded — importing a sibling module
    is not an ``__all__`` question.
    """
    declared = _declared(path)
    if declared is None:
        pytest.skip("covered by test_every_module_states_its_public_surface")
    siblings = {q.stem for q in path.parent.glob("*.py")} | {
        q.name for q in path.parent.iterdir() if q.is_dir()
    }
    wanted = _src_imports().get(_module_name(path), set())
    missing = sorted(
        n for n in wanted - set(declared) if not n.startswith("_") and n not in siblings
    )
    assert not missing, (
        f"{path.relative_to(SRC)} is imported for {missing} and declares none of them in __all__"
    )
