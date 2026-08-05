from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Capability:
    name: str
    reason: str
    required: bool = True


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str
    path: str
    line: int = 1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SkillManifest:
    name: str
    version: str
    description: str
    license: str
    entrypoint: str | None = None
    capabilities: list[Capability] = field(default_factory=list)
    compatibility: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["capabilities"] = [asdict(item) for item in self.capabilities]
        return value
