"""What Fabric says each source can do (GET /api/history/sources, `capabilities`), as one typed object.

Fabric is the one authority for source identity, licence state and implemented capability; the radar keeps
no list of sources of its own. A receipt is read whole or not at all: a receipt of another version, or an
entry that does not carry a boolean `capabilities.readable`, is unusable (CapabilityError) and the caller
treats the capabilities as unknown - it never falls back to a built-in list.

A snapshot of these capabilities is for planning and audit only (what to fetch, which text to expect, what
Fabric declared when the analysis began). It authorises nothing later: every send, retry, cache hit and report
render asks Fabric again (Run.confirm_sources) and reads by version still verify the exact identity.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

VERSION = "datafabric.source_capabilities.v1"


class CapabilityError(Exception):
    """The receipt is not a source-capabilities receipt this client understands."""


def _entries(receipt: Mapping) -> list[Mapping]:
    """The `sources` and `retired_sources` entries. unavailable_sources only say `readable: false` and never
    count as readable."""
    out = []
    for name in ("sources", "retired_sources"):
        listed = receipt.get(name)
        if not isinstance(listed, list):
            raise CapabilityError(f"{name} is not a list")
        out.extend(listed)
    return out


@dataclass(frozen=True)
class SourceCapabilities:
    version: str
    definition_sha256: str | None
    receipt_sha256: str | None
    # source_id -> Fabric's capabilities object, as given
    by_source: Mapping[str, Mapping] = field(default_factory=dict)
    # sources whose every entry says capabilities.readable is true (sources and retired_sources alike)
    readable: frozenset[str] = frozenset()

    @classmethod
    def parse(cls, receipt) -> "SourceCapabilities":
        if not isinstance(receipt, dict):
            raise CapabilityError("the receipt is not an object")
        if receipt.get("capabilities_version") != VERSION:
            raise CapabilityError(f"capabilities_version is {receipt.get('capabilities_version')!r}")
        by_source: dict[str, Mapping] = {}
        flags: dict[str, list[bool]] = {}
        for entry in _entries(receipt):
            source = entry.get("source_id") if isinstance(entry, dict) else None
            capabilities = entry.get("capabilities") if isinstance(entry, dict) else None
            if not isinstance(source, str) or not isinstance(capabilities, dict):
                raise CapabilityError("an entry has no source_id or capabilities")
            if not isinstance(capabilities.get("readable"), bool):
                raise CapabilityError(f"{source}: capabilities.readable is not a boolean")
            by_source.setdefault(source, capabilities)
            flags.setdefault(source, []).append(capabilities["readable"])
        readable = frozenset(source for source, seen in flags.items() if all(seen))
        return cls(
            version=VERSION,
            definition_sha256=_text(receipt.get("definition_sha256")),
            receipt_sha256=_text(receipt.get("receipt_sha256")),
            by_source=by_source,
            readable=readable,
        )

    def _flag(self, source: str, name: str) -> bool:
        return source in self.readable and self.by_source[source].get(name) is True

    def selectable(self, source: str) -> bool:
        """May be an option of a new analysis or of monitoring (Fabric: select)."""
        return self._flag(source, "select")

    def query_fetch(self, source: str) -> bool:
        """Fabric can collect fresh records of this source for a search term."""
        fetch = self.by_source.get(source, {}).get("fetch") if source in self.readable else None
        return isinstance(fetch, dict) and "query" in (fetch.get("modes") or ())

    def attachment_text(self, source: str) -> bool:
        """Attachment versions of this source get their text from Fabric's processing layer."""
        attachments = self.by_source.get(source, {}).get("attachments") if source in self.readable else None
        return isinstance(attachments, dict) and attachments.get("versions") is True

    def registry_diff(self, source: str) -> bool:
        """Fabric diffs a version of this source against the previous one, field by field."""
        diff = self.by_source.get(source, {}).get("registry_diff") if source in self.readable else None
        return isinstance(diff, dict) and bool(diff)

    def snapshot(self) -> dict:
        """What the analysis records of the moment it planned: which receipt, never a permission."""
        return {
            "capabilities_version": self.version,
            "definition_sha256": self.definition_sha256,
            "receipt_sha256": self.receipt_sha256,
            "sources": len(self.by_source),
            "readable": len(self.readable),
        }


def _text(value) -> str | None:
    return value if isinstance(value, str) else None
