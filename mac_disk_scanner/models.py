from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional
import time


@dataclass
class ScanNode:
    path: str
    name: str
    category: str
    size: int = 0
    parent_path: Optional[str] = None
    children: list["ScanNode"] = field(default_factory=list)
    scanned_at: float = field(default_factory=time.time)
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ScanNode":
        value = dict(data)
        value["children"] = [cls.from_dict(x) for x in value.get("children", [])]
        return cls(**value)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def find(self, path: str) -> Optional["ScanNode"]:
        if self.path == path:
            return self
        for child in self.children:
            found = child.find(path)
            if found:
                return found
        return None
