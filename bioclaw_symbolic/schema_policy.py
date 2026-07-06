from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .yaml_compat import load_yaml


def normalize_token(value: Any) -> str:
    return " ".join(str(value).lower().replace("_", " ").split())


def normalize_name(value: Any) -> str:
    return str(value).lower()


@dataclass(frozen=True)
class RoleRule:
    names: tuple[str, ...] = ()
    biolink: tuple[str, ...] = ()
    suffixes: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, data: dict[str, Any]) -> "RoleRule":
        return cls(
            names=tuple(normalize_name(item) for item in data.get("names", []) or []),
            biolink=tuple(normalize_token(item) for item in data.get("biolink", []) or []),
            suffixes=tuple(normalize_name(item) for item in data.get("suffixes", []) or []),
        )

    def matches(self, prop_name: str, biolink: Any = None) -> bool:
        normalized_name = normalize_name(prop_name)
        if normalized_name in self.names:
            return True
        if any(normalized_name.endswith(suffix) for suffix in self.suffixes):
            return True
        if biolink is not None and normalize_token(biolink) in self.biolink:
            return True
        return False


@dataclass(frozen=True)
class SchemaPolicy:
    edge_roles: dict[str, RoleRule]
    node_roles: dict[str, RoleRule]

    @classmethod
    def empty(cls) -> "SchemaPolicy":
        return cls(edge_roles={}, node_roles={})

    @classmethod
    def from_file(cls, path: str | Path | None) -> "SchemaPolicy":
        if not path:
            return cls.empty()
        data = load_yaml(Path(path)) or {}
        return cls(
            edge_roles={
                role: RoleRule.from_config(rule or {})
                for role, rule in (data.get("edge_property_roles") or {}).items()
            },
            node_roles={
                role: RoleRule.from_config(rule or {})
                for role, rule in (data.get("node_property_roles") or {}).items()
            },
        )

    def edge_role(self, prop_name: str, prop_spec: Any = None) -> str:
        biolink = prop_spec.get("biolink") if isinstance(prop_spec, dict) else None
        for role, rule in self.edge_roles.items():
            if rule.matches(prop_name, biolink):
                return role
        return "other"

    def node_role(self, prop_name: str, prop_spec: Any = None) -> str:
        biolink = prop_spec.get("biolink") if isinstance(prop_spec, dict) else None
        for role, rule in self.node_roles.items():
            if rule.matches(prop_name, biolink):
                return role
        return "other"
