"""report_producer.py - the one configured report-producer service principal.

The principal's name and binding are runtime configuration, not source. The
environment variable PENTACLE_REPORT_PRODUCER_CONFIG carries only the PATH of a
user-owned mode-0600 JSON file (never a token):

    {"stream_id": "examplehost:daily-report",      # host:session, the fixed anchor
     "token_file": "/private/path/report-token",   # its own user-owned 0600 token file
     "spec_id": "spec_example__daily_reports",
     "asset_id_pattern": "^daily-report-([0-9]{8})(?:-r[0-9]+)?$",   # exactly one group = cutoff
     "cutoff_format": "%Y%m%d",                    # optional strptime check of the cutoff
     "title_template": "Daily report {cutoff}",
     "tag": "daily-report",
     "body_max_bytes": 65536}

Unset, unreadable, foreign-owned, group/world-accessible or invalid -> disabled
(load() returns None). The file is re-read on every call, so removing or
changing it takes effect on the next RPC. server.py authenticates the principal
and validates its one publish shape; assets.py enforces immutability and keeps
its namespace and producer claim reserved for it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

CONFIG_ENV = "PENTACLE_REPORT_PRODUCER_CONFIG"
CONFIG_MAX_BYTES = 16 * 1024
BODY_MAX_BYTES_CEILING = 1024 * 1024
REQUIRED_KEYS = frozenset({
    "stream_id", "token_file", "spec_id", "asset_id_pattern", "title_template", "tag", "body_max_bytes",
})
OPTIONAL_KEYS = frozenset({"cutoff_format"})
# The other fixed principals; the report producer may never take their identity.
OTHER_FIXED_PRINCIPALS = frozenset({"altum-bot-cd", "amaterasu:wmi-pg-dailybackup"})
_STREAM_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}:[A-Za-z0-9._-]{1,64}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class ReportProducer:
    stream_id: str
    token_file: str
    spec_id: str
    asset_id_re: re.Pattern
    cutoff_format: str | None
    title_template: str
    tag: str
    body_max_bytes: int

    @property
    def anchor(self) -> tuple[str, str]:
        host, _, session = self.stream_id.partition(":")
        return host, session

    def owns_asset_id(self, asset_id: object) -> bool:
        return isinstance(asset_id, str) and self.asset_id_re.fullmatch(asset_id) is not None

    def cutoff(self, asset_id: object) -> str | None:
        """The asset id's cutoff group, or None if the id is not this producer's (or a bad date)."""
        match = self.asset_id_re.fullmatch(asset_id) if isinstance(asset_id, str) else None
        if match is None or not match.group(1):
            return None
        if self.cutoff_format:
            try:
                datetime.strptime(match.group(1), self.cutoff_format)
            except ValueError:
                return None
        return match.group(1)

    def title_for(self, cutoff: str) -> str:
        return self.title_template.replace("{cutoff}", cutoff)


def private_file(path: Path) -> bool:
    try:
        metadata = path.stat()
    except OSError:
        return False
    return path.is_file() and metadata.st_uid == os.getuid() and not metadata.st_mode & 0o077


def load() -> ReportProducer | None:
    raw_path = os.environ.get(CONFIG_ENV)
    if not raw_path:
        return None
    path = Path(raw_path).expanduser()
    if not private_file(path):
        return None
    try:
        if path.stat().st_size > CONFIG_MAX_BYTES:
            return None
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    return _parse(config)


def _round_trips(cutoff_format: str) -> bool:
    """A usable cutoff format: a sample date formats and parses back to the same
    calendar date (literal suffixes such as T1300Z are allowed; lossy formats
    without a full year, month and day are not)."""
    sample = datetime(2001, 2, 3, 4, 5, 6)
    try:
        return datetime.strptime(sample.strftime(cutoff_format), cutoff_format).date() == sample.date()
    except (ValueError, TypeError):
        return False


def _parse(config: object) -> ReportProducer | None:
    if not isinstance(config, dict) or not REQUIRED_KEYS <= set(config) <= REQUIRED_KEYS | OPTIONAL_KEYS:
        return None
    stream_id, token_file, spec_id = config["stream_id"], config["token_file"], config["spec_id"]
    pattern, title_template, tag = config["asset_id_pattern"], config["title_template"], config["tag"]
    body_max, cutoff_format = config["body_max_bytes"], config.get("cutoff_format")
    if (
        not isinstance(stream_id, str) or not _STREAM_ID_RE.fullmatch(stream_id)
        or stream_id in OTHER_FIXED_PRINCIPALS
        or not isinstance(token_file, str) or not Path(token_file).expanduser().is_absolute()
        or not isinstance(spec_id, str) or not spec_id.strip() or spec_id != spec_id.strip() or len(spec_id) > 200
        or not isinstance(pattern, str) or len(pattern) > 200
        or not isinstance(title_template, str) or title_template.count("{cutoff}") != 1 or len(title_template) > 120
        or not isinstance(tag, str) or not _TAG_RE.fullmatch(tag)
        or not isinstance(body_max, int) or isinstance(body_max, bool) or not 0 < body_max <= BODY_MAX_BYTES_CEILING
        or (cutoff_format is not None and (not isinstance(cutoff_format, str) or not cutoff_format))
    ):
        return None
    try:
        asset_id_re = re.compile(pattern)
    except re.error:
        return None
    if cutoff_format is not None and not _round_trips(cutoff_format):
        return None
    if asset_id_re.groups != 1:
        return None
    return ReportProducer(stream_id=stream_id, token_file=str(Path(token_file).expanduser()), spec_id=spec_id,
                          asset_id_re=asset_id_re, cutoff_format=cutoff_format, title_template=title_template,
                          tag=tag, body_max_bytes=body_max)
