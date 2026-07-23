"""Entry point for ``python -m spindle``.

Lets the CLI run without the console script on PATH, which is what
``spindle start`` uses for its no-systemd fallback: an installed wheel's
``__init__.py`` executed as a plain file has no package context, so launching
it by module name is the portable form.
"""

from . import main

if __name__ == "__main__":
    main()
