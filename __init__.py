"""Drop-in entry for `hermes plugins install` (clones this repo into
~/.hermes/plugins/tokenomics/ and loads this package).

Hermes loads this package with ``__path__`` set to the plugin dir but does NOT
put that dir on ``sys.path``, so the flat modules (``hermes_plugin``,
``tokenomics_core``) aren't importable as top-level. Bootstrap ``sys.path`` with
the plugin dir, then reuse the same ``register()`` the pip / entry-point path
uses. (The pip wheel does NOT ship this file — it exposes ``hermes_plugin`` via
the ``hermes_agent.plugins`` entry point instead.)
"""
import os as _os
import sys as _sys

_here = _os.path.dirname(_os.path.abspath(__file__))
if _here not in _sys.path:
    _sys.path.insert(0, _here)

from hermes_plugin import register  # noqa: E402,F401
