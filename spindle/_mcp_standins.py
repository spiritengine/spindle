"""Stand-ins for the MCP server names, bound by spindle's helper processes.

The per-spool owner, its watchdog and the store supervisor import the spindle
package for its spool helpers and never serve MCP. They set the store
supervisor guard before importing, and ``spindle/__init__.py`` then binds these
names instead of importing fastmcp, which was 6.1 s of the owner's 7.1 s import
under load (friction-20261103-mbj2).

The tool definitions still execute, since decorators and annotations are
evaluated at import, so each name only has to survive that: the decorators hand
back the plain function, the annotation types refuse to be constructed, and the
stream errors are never raised because nothing in a helper owns an MCP stream.
"""

GUARD_MESSAGE = (
    "spindle was imported as a helper process (_SPINDLE_STORE_SUPERVISOR=1), "
    "which does not load the MCP server; unset it to serve"
)


class _ServerOnly:
    """Annotation-only type. Constructing one means server code ran in a helper."""

    def __init__(self, *args, **kwargs):
        raise RuntimeError(GUARD_MESSAGE)


class Context(_ServerOnly):
    pass


class ToolResult(_ServerOnly):
    pass


class Request(_ServerOnly):
    pass


class JSONResponse(_ServerOnly):
    pass


class ClosedResourceError(Exception):
    """Never raised in a helper: nothing there owns an MCP stream."""


class BrokenResourceError(Exception):
    """Never raised in a helper: nothing there owns an MCP stream."""


def _unchanged(fn):
    return fn


class FastMCP:
    """Registers nothing, so every tool stays an ordinary module function."""

    def __init__(self, name, **settings):
        self.name = name

    def tool(self, name_or_fn=None, **kwargs):
        # Both decorator spellings: @mcp.tool and @mcp.tool(...).
        return name_or_fn if callable(name_or_fn) else _unchanged

    def custom_route(self, *args, **kwargs):
        return _unchanged

    def run(self, *args, **kwargs):
        raise RuntimeError(GUARD_MESSAGE)

    def __getattr__(self, name):
        raise AttributeError(f"FastMCP.{name}: {GUARD_MESSAGE}")
