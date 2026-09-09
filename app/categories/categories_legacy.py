from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Category:
    name: str
    parent: str | None = None
    children: list[str] = field(default_factory=list)

    @property
    def display(self) -> str:
        return f"{self.parent} > {self.name}" if self.parent else self.name
