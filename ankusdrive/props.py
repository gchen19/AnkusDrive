"""Stamped-property names — the AD_ prefix, and its pre-rename DP_ spelling.

Everything AnkusDrive records ON a part — published interfaces, face roles,
design intent, performance contracts and verdicts, drawing dimension truth,
title blocks, balloons — persists as a FreeCAD property on the object, so it
lives in the user's saved ``.FCStd``. The rename (#295) moved that prefix from
``DP_`` to ``AD_``, which means a document authored by <=0.4.x carries DP_ names.

Renaming the prefix without a fallback would NOT raise: ``getattr(obj, "AD_X",
default)`` on an old part quietly returns the default, so the part reads back as
though it were never annotated — no interfaces, no intent, no verdict. That is
the silent-default failure class, on the one surface where the data is the
user's and not ours. So every READ resolves through :func:`prop_name`, and every
WRITE keeps updating whichever spelling the object already carries; only
genuinely new properties are created as ``AD_``.

Lives here rather than in ``worker.py`` for one reason: ``worker.py`` opens with
``import FreeCAD`` and only ever runs inside ``freecadcmd``, so nothing it
contains can be tested in the fast lane. These four functions are pure attribute
lookups over any object, so ``tests/test_compat_rename.py`` exercises them on a
stub. Deprecated: drop the fallback with a document migration in 0.6."""
from __future__ import annotations

_LEGACY_PROP_PREFIX = "DP_"


def prop_name(obj, name):
    """The spelling of ``name`` this object actually carries — the AD_ name when
    present, else the legacy DP_ name when THAT is present, else the AD_ name."""
    if hasattr(obj, name):
        return name
    if name.startswith("AD_"):
        legacy = _LEGACY_PROP_PREFIX + name[3:]
        if hasattr(obj, legacy):
            return legacy
    return name


def prop_has(obj, name):
    """True when the object carries the property under either spelling."""
    return hasattr(obj, prop_name(obj, name))


def prop_get(obj, name, default=None):
    """Read a stamped property under either spelling."""
    return getattr(obj, prop_name(obj, name), default)


def prop_set(obj, name, value):
    """Write a stamped property, updating whichever spelling already exists."""
    setattr(obj, prop_name(obj, name), value)
