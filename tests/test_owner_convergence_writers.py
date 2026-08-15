"""Two mechanical guards: one over production writers, one over Phase 2 builders.

Phase 1 section 1 ends with a requirement this file makes executable: "Closing
only the named branches is insufficient; owner-episode-v1 needs an enforced
protected-field API or a mechanical call-site allowlist.  Phase 2 needs a
mechanical protected-field writer test over production AST/call sites so a new
sibling writer fails the contract."

The second guard applies the same reasoning to this Phase 2 harness itself.
Three review cycles produced one class twice over: an episode built straight
from ``make_episode``, whose inherited facts sit on that fixture's own literal
clock while the scenario stamps its own facts on another.  Normalising at each
call site moved the defect to whichever site was written next, so the boundary
is now mechanical - ``owner_convergence_fixtures.causal_episode`` is the only
place the raw constructor may be named, and this file fails the moment a new
Phase 2 module reaches around it.

Two failed Fell cycles produced the same class twice - a terminal published by a
private writer that skips the shared duties.  A behavioural test only catches the
sibling someone thought to write a scenario for.  This one catches the next
sibling at the moment it is added.

What counts as a protected write is the full Phase 1 set, not a sample of it:

* ``status`` assigned anything but a literal ``pending``/``running``, and
  ``error``, ``error_kind``, ``result``, ``exit_code``, ``completed_at``,
  ``terminal_origin``, ``terminal_provenance`` written or removed at all;
* ``lifecycle.ownership_state`` released, ``lifecycle.transport_state`` reaped or
  lost, ``lifecycle.normalized_terminal_kind`` at all, and the *removal* of
  ``lifecycle.public_stop_state`` - which is the terminal act on that mirror.
  Setting the stop mirror to ``stopping`` is explicitly nonterminal and never
  trips the guard, because admission is allowed to do it.

and the idioms a real writer uses are all in scope: a literal subscript, a local
dict passed positionally or as ``record_updates=``, ``update()``, a key held in a
variable, a status computed rather than spelled, a projection copied in a loop,
a nested ``lifecycle`` literal, and a dict spread (``{**projection}``) - which is
how a real applicator copies a projection onto a record, and which the scan used
to walk straight past.

Writes inside an explicit legacy branch (``if not self.episode_mode``) are exempt:
they cannot reach an owner-episode-v1 record, which is the only record class this
RSP governs.  Persisting ``release`` is checked separately, because a release fact
is the single act that authorises a public terminal.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.owner_convergence_fixtures import CONVERGENCE_MODULE_FILE

PRODUCTION_PACKAGE = Path(__file__).resolve().parent.parent / "spindle"

#: Public outcome fields.  Any write, and any removal, is a terminal act.
PROTECTED_RECORD_FIELDS = frozenset(
    {
        "completed_at",
        "error",
        "error_kind",
        "exit_code",
        "result",
        "terminal_origin",
        "terminal_provenance",
    }
)
TERMINAL_STATUS_VALUES = frozenset({"error", "timeout", "complete"})
NONTERMINAL_STATUS_VALUES = frozenset({"pending", "running"})
#: Lifecycle fields whose terminal values are a closed set; other values are the
#: live states a producer legitimately writes.
TERMINAL_LIFECYCLE_VALUES = {
    "ownership_state": frozenset({"released"}),
    "transport_state": frozenset({"reaped", "lost"}),
}
#: Every value of this field is a terminal kind.
ALWAYS_TERMINAL_LIFECYCLE = frozenset({"normalized_terminal_kind"})
#: The stop mirror is protected only on removal (or an explicit null).
STOP_MIRROR = "public_stop_state"

PROTECTED_KEYS = (
    PROTECTED_RECORD_FIELDS | {"status", STOP_MIRROR} | frozenset(TERMINAL_LIFECYCLE_VALUES) | ALWAYS_TERMINAL_LIFECYCLE
)

#: The convergence applicator's own module.  Authority is keyed to the module
#: Phase 1 section 6 selected rather than to one private helper name inside it:
#: which functions that module splits itself into is its own business, and
#: pinning a private name would either break on an internal rename or invite an
#: allowlist entry that weakens the rule for everyone else.
APPLICATOR_MODULE = CONVERGENCE_MODULE_FILE


def _module_files():
    return sorted(path for path in PRODUCTION_PACKAGE.glob("*.py") if path.name != "_version.py")


def _qualified(stack) -> str:
    names = [node.name for node in stack if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    return ".".join(names) or "<module>"


def _is_episode_mode(node) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "episode_mode"


def _legacy_guarded(stack, node) -> bool:
    """Whether *node* can only run for a record with no owner episode."""
    for index, parent in enumerate(stack):
        if not isinstance(parent, ast.If):
            continue
        child = stack[index + 1] if index + 1 < len(stack) else node
        in_body = any(child is item or _contains(item, node) for item in parent.body)
        in_orelse = any(child is item or _contains(item, node) for item in parent.orelse)
        test = parent.test
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and _is_episode_mode(test.operand):
            if in_body:
                return True
        if _is_episode_mode(test) and in_orelse:
            return True
    return False


def _contains(tree, node) -> bool:
    return any(item is node for item in ast.walk(tree))


def _constant(node):
    """The literal a node names, or ``_UNKNOWN`` when it is computed."""
    if isinstance(node, ast.Constant):
        return node.value
    return _UNKNOWN


class _Unknown:
    def __repr__(self):  # pragma: no cover - debugging aid
        return "<computed>"


_UNKNOWN = _Unknown()


#: Keys that only a brand-new record literal carries.  Phase 1 exempts "initial
#: pending creation" by name, and a launcher that stamps a fresh ``created_at``
#: is creating a record rather than projecting a terminal onto one.
CREATION_MARKERS = frozenset({"id", "created_at"})


def _is_protected_write(key: str, value, *, removed: bool = False, creating: bool = False) -> bool:
    """Whether writing (or removing) *key* with *value* publishes a terminal."""
    if key not in PROTECTED_KEYS:
        return False
    if removed:
        # Clearing any protected field is itself a terminal projection act.
        return True
    if key == STOP_MIRROR:
        return _constant(value) is None
    if key in PROTECTED_RECORD_FIELDS or key in ALWAYS_TERMINAL_LIFECYCLE:
        return True
    if isinstance(value, ast.IfExp):
        return _is_protected_write(key, value.body, creating=creating) or _is_protected_write(
            key, value.orelse, creating=creating
        )
    literal = _constant(value)
    if literal is _UNKNOWN:
        # A computed status or lifecycle state is exactly how a sibling writer
        # hides one, so it counts - unless this is a record being created, where
        # the launcher's own initial status is the value being passed through.
        return not creating
    if key == "status":
        return literal not in NONTERMINAL_STATUS_VALUES
    return literal in TERMINAL_LIFECYCLE_VALUES[key]


def _creates_a_record(node: ast.Dict) -> bool:
    names = {name for name in (_constant(key) for key in node.keys) if isinstance(name, str)}
    return CREATION_MARKERS <= names


def _dict_write_keys(node, resolve, seen: frozenset = frozenset()) -> set:
    """Protected keys a dict literal writes, following one ``lifecycle`` nesting.

    ``seen`` stops ``spool = {**spool, "status": "error"}`` - a rebind that names
    itself - from resolving to the dict already being walked.
    """
    node = _resolved(node, resolve)
    if not isinstance(node, ast.Dict) or id(node) in seen:
        return set()
    seen = seen | {id(node)}
    creating = _creates_a_record(node)
    written = set()
    for key, value in zip(node.keys, node.values):
        if key is None:
            # ``{**projection}``.  The spread writes whatever the source dict
            # holds, so a writer that assembles its terminal fields in a local
            # dict and spreads it is writing them here.  Following the alias is
            # the difference between a guard and a spelling convention.
            written |= _dict_write_keys(value, resolve, seen)
            continue
        name = _constant(key)
        if not isinstance(name, str):
            continue
        if name == "lifecycle":
            written |= _dict_write_keys(value, resolve, seen)
            continue
        if _is_protected_write(name, value, creating=creating):
            written.add(name)
    return written


def _keyword_write_keys(keywords, resolve) -> set:
    written = set()
    for keyword in keywords:
        if keyword.arg is None:  # ``**mapping``
            written |= _dict_write_keys(keyword.value, resolve)
            continue
        if keyword.arg == "lifecycle":
            written |= _dict_write_keys(keyword.value, resolve)
            continue
        if _is_protected_write(keyword.arg, keyword.value):
            written.add(keyword.arg)
    return written


def _subscript_keys(target, value, resolve_string, loop_keys, *, removed: bool = False) -> set:
    """Protected keys a subscript assignment or deletion writes."""
    if not isinstance(target, ast.Subscript):
        return set()
    candidates: set = set()
    key = target.slice
    literal = _constant(key)
    if isinstance(literal, str):
        candidates = {literal}
    elif isinstance(key, ast.Name):
        # A key held in a variable is still a key whenever the variable can be
        # resolved - to a constant, or to the field names of a projection loop.
        candidates = loop_keys(key.id) | resolve_string(key.id)
    return {name for name in candidates if _is_protected_write(name, value, removed=removed)}


def _resolved(node, resolve):
    """Follow one local ``name = {...}`` binding so a writer cannot hide behind it."""
    if isinstance(node, ast.Name) and resolve is not None:
        return resolve(node.id) or node
    return node


def _walk(tree):
    """Yield (node, ancestor stack) for every node, outermost ancestor first.

    The stack is live and must be read before the next iteration; copying it for
    every node in a 12k-line module is the difference between a guard that runs
    on every test round and one nobody waits for.
    """
    stack = []

    def visit(node):
        for child in ast.iter_child_nodes(node):
            stack.append(node)
            yield child, stack
            yield from visit(child)
            stack.pop()

    yield from visit(tree)


def _bindings(tree) -> tuple[dict, dict, dict]:
    """Local dict, string and loop-variable bindings, keyed by (function, name).

    A writer that assembles its terminal fields in a local dict and passes the
    name to ``record_updates=``, or that copies a projection field by field in a
    loop, is still a writer.  Without these the guard would miss exactly the
    producers Phase 1 inventoried.
    """
    dicts: dict[tuple, ast.Dict] = {}
    strings: dict[tuple, set] = {}
    loops: dict[tuple, set] = {}

    for node, stack in _walk(tree):
        scope = _qualified(stack)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                if isinstance(node.value, ast.Dict):
                    dicts[(scope, target.id)] = node.value
                literal = _constant(node.value)
                if isinstance(literal, str):
                    strings.setdefault((scope, target.id), set()).add(literal)
        elif isinstance(node, ast.For):
            names = _iterated_keys(node.iter, lambda name: dicts.get((scope, name)))
            if not names:
                continue
            target = node.target
            key_name = None
            if isinstance(target, ast.Name):
                key_name = target.id
            elif isinstance(target, ast.Tuple) and target.elts and isinstance(target.elts[0], ast.Name):
                key_name = target.elts[0].id
            if key_name is not None:
                loops.setdefault((scope, key_name), set()).update(names)
    return dicts, strings, loops


def _iterated_keys(node, resolve) -> set:
    """The literal key names a ``for`` loop can bind, when they are knowable."""
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return {value for value in (_constant(item) for item in node.elts) if isinstance(value, str)}
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"items", "keys"}:
        source = _resolved(node.func.value, resolve)
        if isinstance(source, ast.Dict):
            return {value for value in (_constant(key) for key in source.keys) if isinstance(value, str)}
    return set()


_PARSED: dict = {}


def _parsed(path: Path):
    """Parse each module once; both scans read the same trees."""
    if path not in _PARSED:
        tree = ast.parse(path.read_text(), filename=str(path))
        _PARSED[path] = (tree, _bindings(tree))
    return _PARSED[path]


def _terminal_keys(node, resolve, resolve_string, loop_keys) -> set:
    """Protected terminal keys this statement writes."""
    written = set()
    if isinstance(node, ast.Assign):
        for target in node.targets:
            written |= _subscript_keys(target, node.value, resolve_string, loop_keys)
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        written |= _subscript_keys(node.target, node.value, resolve_string, loop_keys)
    elif isinstance(node, ast.Delete):
        for target in node.targets:
            written |= _subscript_keys(target, None, resolve_string, loop_keys, removed=True)
    elif isinstance(node, ast.Call):
        written |= _call_keys(node, resolve)
    return written


def _call_keys(node: ast.Call, resolve) -> set:
    written = set()
    name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
    if name == "update":
        for argument in node.args:
            written |= _dict_write_keys(argument, resolve)
        written |= _keyword_write_keys(node.keywords, resolve)
    elif name == "pop" and node.args:
        removed = _constant(node.args[0])
        if isinstance(removed, str) and _is_protected_write(removed, None, removed=True):
            written.add(removed)
    elif name in {"_write_spool", "_write_spool_unlocked", "_update_spool", "_atomic_json_write"}:
        # The record is published positionally here; a dict assembled anywhere
        # in the same function is still this function's terminal write.
        for argument in node.args:
            written |= _dict_write_keys(argument, resolve)
        written |= _keyword_write_keys(node.keywords, resolve)
    for keyword in node.keywords:
        if keyword.arg in {"record_updates", "fields"}:
            written |= _dict_write_keys(keyword.value, resolve)
    return written


def _release_transition(node, resolve, resolve_string, loop_keys) -> set:
    del resolve, loop_keys
    if not isinstance(node, ast.Call):
        return set()
    name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
    if name != "transition_owner_episode":
        return set()
    for keyword in node.keywords:
        if keyword.arg != "destination":
            continue
        literal = _constant(keyword.value)
        if literal == "released":
            return {"release"}
        if isinstance(keyword.value, ast.Name) and "released" in resolve_string(keyword.value.id):
            return {"release"}
    return set()


def _scan(predicate, files) -> dict:
    """Map ``module.py:function`` to the protected evidence *predicate* matched."""
    found: dict[str, set] = {}
    for path in files:
        tree, (dicts, strings, loops) = _parsed(path)
        for node, stack in _walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.Call, ast.Delete)):
                continue  # only statements that can write a field are candidates
            scope = _qualified(stack)
            matched = predicate(
                node,
                lambda name, scope=scope: dicts.get((scope, name)),
                lambda name, scope=scope: strings.get((scope, name), set()),
                lambda name, scope=scope: loops.get((scope, name), set()),
            )
            if not matched:
                continue
            if _legacy_guarded(stack, node):
                continue
            found.setdefault(f"{path.name}:{scope}", set()).update(matched)
    return found


def _outside_the_applicator(found: dict) -> dict:
    prefix = f"{APPLICATOR_MODULE}:"
    return {name: keys for name, keys in found.items() if not name.startswith(prefix)}


def _report(found: dict, subject: str) -> str:
    outside = _outside_the_applicator(found)
    lines = [f"{subject} must be written only by the convergence applicator ({APPLICATOR_MODULE})."]
    if outside:
        lines.append("Writers outside it:")
        lines += [f"  {name}  writes {sorted(outside[name])}" for name in sorted(outside)]
    if not found:
        lines.append("The scan found no writer anywhere, including the applicator.")
    return "\n".join(lines)


def test_only_the_convergence_applicator_publishes_a_public_terminal():
    """Every terminal field write lives in one module, or is a proven exception.

    A failure here is not a style complaint.  Each name listed is a path that can
    publish a user-visible terminal without the outcome, provenance and owed-duty
    set the record must carry - the exact shape of defect 3.
    """
    found = _scan(_terminal_keys, _module_files())

    assert _outside_the_applicator(found) == {}, _report(found, "Public terminal fields")
    assert found, _report(found, "Public terminal fields")


def test_only_the_convergence_applicator_persists_a_release():
    """Decision 30: the reconciler alone persists release, then projects."""
    found = _scan(_release_transition, _module_files())

    assert _outside_the_applicator(found) == {}, _report(found, "The release fact")
    assert found, _report(found, "The release fact")


# --- the guard's own self-check ---------------------------------------------

#: One sample per realistic writer idiom.  These go through ``_scan`` itself, not
#: through its internals: a future refactor that narrows the scan would otherwise
#: make every production writer disappear while this test still passed.
SIBLING_WRITERS = """
def _literal_terminal(spool):
    spool["status"] = "error"
    spool["completed_at"] = "now"
    spool["error_kind"] = "whatever"


def _computed_status(spool, kind):
    spool["status"] = "timeout" if kind == "timeout" else "error"


def _opaque_status(spool, status):
    spool["status"] = status


def _local_dict_positional(spool_id, spool):
    updates = {"status": "error", "error": "boom", "result": None}
    _write_spool(spool_id, updates)


def _local_dict_keyword(store, spool_id):
    updates = {"completed_at": "now", "exit_code": 3}
    transition_owner_episode(store, spool_id, record_updates=updates)


def _update_call(spool):
    spool.update({"status": "complete", "terminal_origin": "natural_success"})


def _nested_lifecycle(spool):
    spool.update({"lifecycle": {"ownership_state": "released", "transport_state": "reaped"}})


def _variable_key(spool, value):
    field = "terminal_provenance"
    spool[field] = value


def _projection_loop(spool):
    projection = {"status": "error", "error_kind": "owner_transport_loss", "result": None}
    for field, value in projection.items():
        spool[field] = value


def _stop_mirror_cleared(lifecycle):
    lifecycle.pop("public_stop_state", None)


def _deleted_outcome(spool):
    del spool["error"]


def _terminal_dressed_as_a_creation(spool_id):
    record = {"id": spool_id, "created_at": "now", "status": "error", "completed_at": "now"}
    _write_spool(spool_id, record)


def _spread_projection(spool_id):
    projection = {"status": "error", "error": "boom", "completed_at": "now"}
    _write_spool(spool_id, {**projection})


def _spread_into_update(spool):
    projection = {"terminal_origin": "owner_crash_before_cleanup", "exit_code": None}
    spool.update({**projection, "error_kind": "owner_transport_loss"})


def _spread_lifecycle(spool):
    terminal = {"ownership_state": "released", "normalized_terminal_kind": "failed"}
    spool.update({"lifecycle": {**terminal}})


def _spread_over_itself(spool_id, spool):
    spool = {"status": "running"}
    spool = {**spool, "status": "error", "completed_at": "now"}
    _write_spool(spool_id, spool)
"""

#: Writes a producer is allowed to make.  If any of these trip the guard it is
#: over-broad, and an over-broad guard gets narrowed until it sees nothing.
PERMITTED_WRITERS = """
class Owner:
    def legacy_only(self, spool):
        if not self.episode_mode:
            spool["status"] = "error"
            spool["completed_at"] = "now"

    def publish_running(self, spool):
        spool["status"] = "running"
        spool["lifecycle"] = {"ownership_state": "held", "transport_state": "connected"}

    def admit_stop(self, lifecycle):
        lifecycle.update({"public_stop_state": "stopping"})


def create_pending(spool):
    spool["status"] = "pending"


def reserve_a_new_record(spool_id, initial_status, reservation_metadata):
    record = {"id": spool_id, "status": initial_status, "created_at": "now"}
    record.update(reservation_metadata)
    _write_spool(spool_id, record)


def collect_results(spools, results):
    for spool_id, spool in spools.items():
        results[spool_id] = spool.get("result")


def copy_launch_metadata(current, spool):
    for key in ("working_dir", "shard", "harness"):
        current[key] = spool[key]
"""


def _scan_sample(tmp_path, name: str, source: str) -> dict:
    path = tmp_path / name
    path.write_text(source)
    return _scan(_terminal_keys, [path])


def test_the_guard_recognises_every_realistic_sibling_writer(tmp_path):
    """The scan itself must see each idiom a new terminal writer would use."""
    found = _scan_sample(tmp_path, "sibling_writers.py", SIBLING_WRITERS)

    assert found == {
        "sibling_writers.py:_literal_terminal": {"status", "completed_at", "error_kind"},
        "sibling_writers.py:_computed_status": {"status"},
        "sibling_writers.py:_opaque_status": {"status"},
        "sibling_writers.py:_local_dict_positional": {"status", "error", "result"},
        "sibling_writers.py:_local_dict_keyword": {"completed_at", "exit_code"},
        "sibling_writers.py:_update_call": {"status", "terminal_origin"},
        "sibling_writers.py:_nested_lifecycle": {"ownership_state", "transport_state"},
        "sibling_writers.py:_variable_key": {"terminal_provenance"},
        "sibling_writers.py:_projection_loop": {"status", "error_kind", "result"},
        "sibling_writers.py:_stop_mirror_cleared": {"public_stop_state"},
        "sibling_writers.py:_deleted_outcome": {"error"},
        "sibling_writers.py:_terminal_dressed_as_a_creation": {"status", "completed_at"},
        "sibling_writers.py:_spread_projection": {"status", "error", "completed_at"},
        "sibling_writers.py:_spread_into_update": {"terminal_origin", "exit_code", "error_kind"},
        "sibling_writers.py:_spread_lifecycle": {"ownership_state", "normalized_terminal_kind"},
        "sibling_writers.py:_spread_over_itself": {"status", "completed_at"},
    }, "the closed-writer scan stopped recognising a writer idiom"


def test_the_guard_permits_every_write_a_producer_may_still_make(tmp_path):
    """Legacy branches, live states and unrelated dicts are not terminal writes."""
    found = _scan_sample(tmp_path, "permitted_writers.py", PERMITTED_WRITERS)

    assert found == {}, f"the closed-writer scan flagged a permitted producer write: {found}"


def test_the_guard_recognises_a_release_written_through_the_applicator(tmp_path):
    """The release scan runs over the same path the production scan does."""
    source = (
        "def _publish(store, spool_id):\n"
        "    transition_owner_episode(store, spool_id, actor='reconciler', destination='released')\n"
        "\n"
        "def _publish_cleanup(store, spool_id):\n"
        "    transition_owner_episode(store, spool_id, actor='owner', destination='cleanup_proven')\n"
    )
    path = tmp_path / "release_writers.py"
    path.write_text(source)

    found = _scan(_release_transition, [path])

    assert found == {"release_writers.py:_publish": {"release"}}


@pytest.mark.parametrize("module", [path.name for path in _module_files()])
def test_production_modules_parse_for_the_guard(module):
    """The guard is only meaningful while it can read every production module."""
    path = PRODUCTION_PACKAGE / module
    ast.parse(path.read_text(), filename=str(path))


# --- the Phase 2 episode-construction boundary ------------------------------

TEST_PACKAGE = Path(__file__).resolve().parent

#: Every Phase 2 module is scanned, found rather than listed, so a module added
#: later is covered without anyone remembering to enrol it.
PHASE_2_GLOB = "*owner_convergence*.py"

#: The modules that must be in that scan.  A rename which drops one out of the
#: glob would otherwise shrink the guard silently.
PHASE_2_MODULES = frozenset(
    {
        "owner_convergence_fixtures.py",
        "test_owner_convergence_contract.py",
        "test_owner_convergence_crash.py",
        "test_owner_convergence_defects.py",
        "test_owner_convergence_writers.py",
    }
)

#: The raw episode constructor, and the single scope allowed to name it.
RAW_CONSTRUCTOR = "make_episode"
CAUSAL_BUILDER_MODULE = "owner_convergence_fixtures.py"
CAUSAL_BUILDER = f"{CAUSAL_BUILDER_MODULE}:causal_episode"


def _phase_2_files():
    return sorted(TEST_PACKAGE.glob(PHASE_2_GLOB))


def _reflective_lookup(node) -> bool:
    """Whether *node* is ``getattr(<module>, 'make_episode')``.

    A reflective lookup reaches the raw constructor without ever spelling it
    where an attribute or a name would be seen: the value comes back through
    ``getattr``, is bound to a local alias, and is called through that alias.
    The lookup itself is the act this guard is about, so the scope that performs
    it is the scope that reached around the builder - whatever it goes on to do
    with the result.  A computed second argument is deliberately not chased:
    resolving arbitrary expressions is a different guard, and one that would
    never finish being right.
    """
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != "getattr":
        return False
    return len(node.args) >= 2 and _constant(node.args[1]) == RAW_CONSTRUCTOR


def _raw_constructor_uses(path: Path) -> tuple[set, set]:
    """Where a module imports the raw constructor, and where it names it.

    Import and use are separated because they are different acts: the builder's
    module has to import the constructor it wraps, and a module that imports it
    without calling it has still opened the door for the next edit.  A string
    spelling the name is not a use, which is why this reads the tree rather than
    the text - the guard's own constant would otherwise indict it.  A string
    handed to ``getattr`` is the exception, because there the name is not being
    mentioned but resolved.
    """
    imports: set = set()
    references: set = set()
    tree = ast.parse(path.read_text(), filename=str(path))
    for node, stack in _walk(tree):
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == RAW_CONSTRUCTOR for alias in node.names):
                imports.add(f"{path.name}:{_qualified(stack)}")
        elif isinstance(node, ast.Name) and node.id == RAW_CONSTRUCTOR:
            references.add(f"{path.name}:{_qualified(stack)}")
        elif isinstance(node, ast.Attribute) and node.attr == RAW_CONSTRUCTOR:
            references.add(f"{path.name}:{_qualified(stack)}")
        elif _reflective_lookup(node):
            references.add(f"{path.name}:{_qualified(stack)}")
    return imports, references


def test_every_phase_2_module_is_inside_the_construction_scan():
    """The scan finds its modules, so no rename can quietly empty it."""
    found = {path.name for path in _phase_2_files()}

    assert PHASE_2_MODULES <= found, f"Phase 2 modules missing from the scan: {sorted(PHASE_2_MODULES - found)}"


def test_only_the_causal_builder_constructs_a_phase_2_owner_episode():
    """One construction path, enforced rather than agreed.

    A failure here names a Phase 2 scope that builds an episode from the raw
    fixture constructor.  That episode keeps ``make_episode``'s own literal
    phase times and fact stamps, so any fact the scenario then writes on the
    scenario clock makes the history impossible - the defect that stopped the
    Fell twice.  The fix is to call ``causal_episode``, never to normalise one
    more timestamp at the call site.
    """
    imports: set = set()
    references: set = set()
    for path in _phase_2_files():
        module_imports, module_references = _raw_constructor_uses(path)
        imports |= module_imports
        references |= module_references

    assert references == {CAUSAL_BUILDER}, (
        f"{RAW_CONSTRUCTOR} must be named only by {CAUSAL_BUILDER}; found {sorted(references)}"
    )
    assert imports == {f"{CAUSAL_BUILDER_MODULE}:<module>"}, (
        f"{RAW_CONSTRUCTOR} must be imported only by {CAUSAL_BUILDER_MODULE}; found {sorted(imports)}"
    )


def test_the_construction_scan_sees_a_module_that_builds_its_own_episode(tmp_path):
    """The scan's own teeth: each way of reaching the raw constructor is seen."""
    path = tmp_path / "test_owner_convergence_sample.py"
    path.write_text(
        "from tests.owner_episode_fixtures import make_episode\n"
        "import tests.owner_episode_fixtures as fixtures\n"
        "\n"
        "def _direct():\n"
        "    return make_episode('released')\n"
        "\n"
        "def _through_the_module():\n"
        "    return fixtures.make_episode('released')\n"
        "\n"
        "def _mentions_it_in_a_string():\n"
        "    return 'make_episode'\n"
    )

    imports, references = _raw_constructor_uses(path)

    assert imports == {"test_owner_convergence_sample.py:<module>"}
    assert references == {
        "test_owner_convergence_sample.py:_direct",
        "test_owner_convergence_sample.py:_through_the_module",
    }


def test_the_construction_scan_sees_a_reflective_lookup_of_the_raw_constructor(tmp_path):
    """A ``getattr`` alias uses the constructor; it does not merely mention it.

    This is the realistic way around the boundary, and the one the scan used to
    walk past: nothing imports the raw constructor and nothing spells it as an
    attribute, so a module could bind ``getattr(fixtures, "make_episode")`` to a
    local name, build its episodes through that name on the pure fixture's own
    literal clock, and still be reported clean.  A lookup of some other
    attribute, and one whose name is only known at run time, stay out of it -
    the first is not this constructor and the second is a resolver, not a guard.
    """
    path = tmp_path / "test_owner_convergence_reflective.py"
    path.write_text(
        "import tests.owner_episode_fixtures as fixtures\n"
        "\n"
        "def _through_a_local_alias():\n"
        '    builder = getattr(fixtures, "make_episode")\n'
        '    return builder("released")\n'
        "\n"
        "def _called_where_it_is_looked_up():\n"
        '    return getattr(fixtures, "make_episode")("released")\n'
        "\n"
        "def _looks_up_another_attribute():\n"
        '    return getattr(fixtures, "facts_for")("cleanup")\n'
        "\n"
        "def _looks_up_a_name_it_computes(name):\n"
        '    return getattr(fixtures, name)("released")\n'
    )

    imports, references = _raw_constructor_uses(path)

    assert imports == set()
    assert references == {
        "test_owner_convergence_reflective.py:_through_a_local_alias",
        "test_owner_convergence_reflective.py:_called_where_it_is_looked_up",
    }, "the construction scan does not recognise a reflective lookup of the raw constructor"
