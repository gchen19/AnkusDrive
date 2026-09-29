"""Provenance that travels with the model: the session record inside a saved .FCStd (#462).

The journal (#407, #433) says how a session reached its result, but it lives in the
server's memory or in a directory on the machine that ran it. A ``.FCStd`` handed to a
colleague carries neither. ``save_document(..., attach_provenance=True)`` embeds the
same record ``journal_export`` gives — the replay script, the environment and solver
identities, and the call ledger with each result's SHA-256 — in the file itself.

**Which calls belong to the model.** A session spans workspaces and documents, so the
record is cut down to the calls that built *this* document:

* only the saving **workspace**, and only its **current worker** (a restart loses every
  document, so nothing before it can have shaped this one);
* from the ``new_document`` / ``open_document`` call that produced the document being
  saved (the last one whose result names it) — or, when nothing did (a ``run_script``
  created it), from the worker's first call;
* only while that document was **active**: calls made after ``new_document`` /
  ``open_document`` / ``set_active_document`` switched to another document are
  excluded, as are ``close_document`` and workspace switches. Tools act on the active document, so this is
  what they could have touched. A cross-document operation (an assembly linking a part
  saved from another document) records only this document's side;
* **successful** calls only. A failed call changed nothing a replay needs.

**Where.** In the document's own ``Meta`` map (``App::PropertyMap``), under
:data:`META_KEY`, as compact JSON. It is plain XML in ``Document.xml``, so it survives
FreeCAD saving and reopening the file — in the GUI too — and :func:`read` recovers it
with the standard library, no FreeCAD. A save *without* the flag removes an earlier
record from the document, so a file never carries a record that no longer describes it.
Edits made later outside AnkusDrive, then saved by FreeCAD, keep the old record: its
``saved`` timestamp and ledger say what it covers.

**Privacy.** Opt-in per save; it puts arguments, paths and the environment into a file
people share (PRIVACY.md). ``ANKUSDRIVE_JOURNAL_REDACT`` is honoured exactly as the
durable journal applies it (``journal_store.redact_entry``): the record is then an
audit trail by digest, not a runnable replay.

Pure stdlib and FreeCAD-free.
"""
from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
import zipfile

FORMAT = "ankusdrive-fcstd-provenance/1"
META_KEY = "AnkusDriveProvenance"
RULE = ("calls in the saving workspace's current worker, from the new_document/"
        "open_document that produced this document (else the worker's first call), "
        "made while it was the active document; failed calls excluded")

_OPENERS = ("new_document", "open_document")
# Never part of a document's record: closing documents, and moving between workspaces
# (the record replays in one workspace; a use_workspace is journaled in the one it left).
_PLUMBING = ("close_document", "use_workspace", "close_workspace")


def _doc_of(entry: dict):
    r = entry.get("result")
    return r.get("doc") if isinstance(r, dict) else None


def select(entries: list, doc: str, worker_pid=None) -> tuple:
    """The entries that belong to document ``doc`` (module docstring). ``entries`` are
    one workspace's journal entries; ``worker_pid`` the current worker's (default: the
    last entry's). Returns ``(selected, scope)``."""
    entries = sorted(entries, key=lambda e: e["seq"])
    if worker_pid is None and entries:
        worker_pid = entries[-1].get("worker_pid")
    mine = [e for e in entries if e.get("worker_pid") == worker_pid]
    anchor = None
    for e in mine:
        if e.get("ok") and e.get("tool") in _OPENERS and _doc_of(e) == doc:
            anchor = e
    start = anchor["seq"] if anchor else (mine[0]["seq"] if mine else 0)
    active, out = doc, []
    failed = other = 0
    for e in mine:
        if e["seq"] < start:
            continue
        tool = e.get("tool")
        if not e.get("ok"):
            failed += 1
            continue
        if tool in _OPENERS:
            active = _doc_of(e) or active
        elif tool == "set_active_document":
            active = (e.get("args") or {}).get("name") or active
        if active == doc and tool not in _PLUMBING:
            out.append(e)
        else:
            other += 1
    scope = {"rule": RULE,
             "anchor": {"seq": anchor["seq"], "tool": anchor["tool"]} if anchor else None,
             "calls": len(out), "excluded_failed": failed,
             "excluded_other_document": other,
             "excluded_earlier": sum(1 for e in entries if e["seq"] < start
                                     or e.get("worker_pid") != worker_pid)}
    return out, scope


def build(entries: list, *, doc: str, workspace: str, worker_pid=None,
          provenance_of=None, redact: bool | None = None) -> dict:
    """The record to embed for ``doc`` of ``workspace``: :func:`select` over that
    workspace's ``entries``, then ``replay.export`` over the
    selection, plus the environment and the ledger. ``provenance_of(entries)`` returns
    the environment record (``session_transcript``'s); ``redact`` defaults to
    ``ANKUSDRIVE_JOURNAL_REDACT``."""
    from ankusdrive import journal_store, replay
    entries = [e for e in entries if e.get("workspace") == workspace]
    selected, scope = select(entries, doc, worker_pid)
    if redact is None:
        redact = journal_store.redacting()
    if redact:
        selected = [journal_store.redact_entry(e) for e in selected]
    prov = None
    if provenance_of is not None:
        try:
            prov = provenance_of(selected)
        except Exception:
            prov = None
    out = replay.export(selected, workspace=workspace, provenance=prov)
    skipped: dict = {}
    for s in out["skipped"]:
        skipped[s["reason"]] = skipped.get(s["reason"], 0) + 1
    warnings = list(out.get("warnings") or [])
    if redact:
        warnings.append("recorded with ANKUSDRIVE_JOURNAL_REDACT: identifying strings are "
                        "sha256 hashes, so the script is an audit record, not a runnable "
                        "replay")
    ledger = []
    for e in selected:
        row = {"seq": e["seq"], "tool": e.get("tool")}
        for k in ("result_digest", "code_sha256"):
            if e.get(k):
                row[k] = e[k]
        ledger.append(row)
    rec = {"format": FORMAT,
           "saved": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "document": journal_store._hash_str(doc) if redact else doc,
           "workspace": workspace, "scope": scope, "script": out["script"],
           "exported": out["exported"], "skipped": skipped, "warnings": warnings,
           "prerequisites": out.get("prerequisites") or [], "redacted": bool(redact),
           "ledger": ledger}
    if prov is not None:
        rec["provenance"] = prov
    status = journal_store.status()
    if status.get("enabled") and status.get("session"):
        rec["journal_session"] = status["session"]
    return rec


def dumps(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str)


def read(path: str) -> dict | None:
    """The record embedded in the ``.FCStd`` at ``path``, or ``None`` when it carries
    none. Reads ``Document.xml`` from the zip directly — no FreeCAD."""
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("Document.xml"))
    props = root.find("Properties")
    if props is None:
        return None
    for prop in props.findall("Property"):
        if prop.get("name") != "Meta":
            continue
        for item in prop.iter("Item"):
            if item.get("key") == META_KEY:
                return json.loads(item.get("value") or "null")
    return None
