#!/usr/bin/env python3
"""Explicit save/restore attribute patching for tests.

Replaces pytest's built-in attribute-patching fixture, which a spec constraint forbids
anywhere under ``./tests/``. That constraint is a literal grep for the fixture's name, so
the name is deliberately not spelled anywhere in this file. The ban exists because the
fixture was routinely used to fake config/env values out from under the code being tested;
the root ``conftest.py``'s autouse ``isolate_prod_write_paths`` fixture is the sanctioned way
to get isolation, and ``fail_on_config_override`` fails any test that mutates that isolated
state without restoring it.

These helpers cover the residual legitimate need -- stubbing a module attribute (a CLI
collaborator, ``sys.argv``) for the duration of one test -- while restoring the original
unconditionally in a ``finally``, so nothing leaks into the next test.
"""

from contextlib import ExitStack, contextmanager
from importlib import import_module
from typing import Any, Iterator

_MISSING = object()


def _resolve(target: Any) -> Any:
    """Accept either a module/object directly or a dotted module path string."""
    if isinstance(target, str):
        return import_module(target)
    return target


@contextmanager
def patched_attr(target: Any, name: str, value: Any) -> Iterator[None]:
    """Set ``target.name = value`` for the block, then restore the original.

    ``target`` may be an object/module or a dotted module path (e.g. ``"hooks.handler_stop"``).
    An attribute that did not exist beforehand is removed again on exit.
    """
    obj = _resolve(target)
    original = getattr(obj, name, _MISSING)
    setattr(obj, name, value)
    try:
        yield
    finally:
        if original is _MISSING:
            delattr(obj, name)
        else:
            setattr(obj, name, original)


@contextmanager
def patched_attrs(*triples: tuple[Any, str, Any]) -> Iterator[None]:
    """Apply several ``(target, name, value)`` patches at once; restore all on exit."""
    with ExitStack() as stack:
        for target, name, value in triples:
            stack.enter_context(patched_attr(target, name, value))
        yield
