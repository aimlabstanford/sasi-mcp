"""Vendored AppleScript runner + Outlook SQLite cache helper.

Standalone-repo copy of the parts of ledger_bridge.outlook / outlook_cache
this package actually depends on. Kept narrow on purpose:

* `_run_script` — invoke an AppleScript with positional args, parse stdout
  as JSON. `strict=False` because Outlook bodies carry zero-width-joiner
  preheader gunk that breaks strict JSON parsing.
* `OutlookCacheClient.list_accounts` — opens the local Outlook SQLite
  read-only and lists configured accounts. Used at preflight to confirm
  the shared mailbox is reachable. Body decoding is deliberately not
  ported: in ledger_bridge it only handles attachment blocks (BlockTag
  AT11), not message bodies, so it has no use here.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from sasi_mcp.logger import get_logger

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "applescript"
_log = get_logger("sasi_mcp.outlook_bridge")

_DEFAULT_PROFILE = (
    Path.home()
    / "Library"
    / "Group Containers"
    / "UBF8T346G9.Office"
    / "Outlook"
    / "Outlook 15 Profiles"
    / "Main Profile"
    / "Data"
)


class OutlookError(Exception):
    """Raised when osascript returns a non-zero exit or malformed JSON."""


class OutlookCacheError(RuntimeError):
    pass


def _run_script(name: str, *args: str, timeout: int = 1800) -> object:
    script_path = _SCRIPTS_DIR / name
    if not script_path.exists():
        raise OutlookError(f"AppleScript not found: {script_path}")
    proc = subprocess.run(
        ["osascript", str(script_path), *args],
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise OutlookError(f"osascript {name} exited {proc.returncode}: {err[:500]}")
    stdout = proc.stdout.decode("utf-8", errors="replace").strip()
    if not stdout:
        return None
    try:
        return json.loads(stdout, strict=False)
    except json.JSONDecodeError as exc:
        raise OutlookError(f"osascript {name} returned non-JSON: {stdout[:500]}") from exc


@dataclass(slots=True)
class OutlookCacheClient:
    profile_dir: Path = _DEFAULT_PROFILE
    _conn: sqlite3.Connection | None = field(default=None, init=False, repr=False)

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        db = self.profile_dir / "Outlook.sqlite"
        if not db.exists():
            raise OutlookCacheError(f"Outlook SQLite DB not found: {db}")
        uri = f"file:{db}?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True)
        self._conn.row_factory = sqlite3.Row
        return self._conn

    def list_accounts(self) -> list[tuple[int, str, str]]:
        """Return [(record_id, display_name, email)] per configured account."""
        cur = self._ensure_conn().cursor()
        cur.execute(
            "SELECT Record_RecordID, Account_Name, Account_EmailAddress "
            "FROM AccountsMail "
            "WHERE Account_EmailAddress <> ''"
        )
        return [(int(r[0]), str(r[1] or ""), str(r[2] or "")) for r in cur.fetchall()]
