"""Single source of truth for the spindle version.

Kept in its own import-free module so setuptools can read it statically
(``[tool.setuptools.dynamic] version = {attr = "spindle._version.__version__"}``)
without importing the package - importing ``spindle`` pulls in fastmcp, which
is not available at build time.

Everything else reads it from ``spindle.__version__``. The wheel metadata, the
``/health`` payload, ``spindle --version`` and ``spindle doctor`` all resolve to
this one string, so a CLI and a service built from different trees cannot
silently agree.
"""

__version__ = "1.2.0"
