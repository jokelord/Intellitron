"""Validated, immutable configuration for selectable Agent business tools.

This module deliberately knows nothing about MCP or special protocol tools.  It
is the single source of truth used to project a selection into runtime tool
names, prompt guidance, and public configuration metadata.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator


TOOL_CONFIGURATION_SCHEMA_VERSION = 1

BUSINESS_TOOL_NAMES: tuple[str, ...] = (
    "read_file", "write_file", "edit_file", "glob", "grep",
    "exec_command", "check_process", "list_skills",
    "read_skill", "web_search", "web_fetch", "search_and_fetch",
    "wikipedia_search_and_fetch",
)
SPECIAL_TOOL_NAMES = frozenset({
    "final_answer", "switch_agent", "dispatching_parallel_agent",
    "wikipedia_search", "wikipedia_fetch", "wikipedia_summary",
})
PROFILE_NAMES = frozenset({"manager_agent", "basic_agent"})

DEFAULT_PROFILE_TOOLS: dict[str, tuple[str, ...]] = {
    "manager_agent": (
        "read_file", "write_file", "edit_file", "glob", "grep",
        "exec_command", "list_skills", "read_skill", "web_fetch",
        "search_and_fetch",
    ),
    "basic_agent": (
        "read_file", "write_file", "edit_file", "glob", "grep",
        "exec_command", "check_process", "list_skills",
        "read_skill", "web_search", "web_fetch", "search_and_fetch",
    ),
}


class AgentToolConfigurationError(ValueError):
    """Stable domain error mapped to HTTP by the Agent API."""

    def __init__(self, code: Literal[
        "invalid_tool_configuration", "tool_catalog_changed",
        "tool_configuration_locked", "tool_prompt_render_failed",
    ], message: str, *, status_code: int | None = None) -> None:
        self.code = code
        self.status_code = status_code or {
            "invalid_tool_configuration": 422,
            "tool_catalog_changed": 409,
            "tool_configuration_locked": 409,
            "tool_prompt_render_failed": 500,
        }[code]
        self.message = message
        super().__init__(message)


class AgentToolConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: int
    catalog_revision: str
    manager_agent_tools: list[str]
    basic_agent_tools: list[str]

    @field_validator("catalog_revision")
    @classmethod
    def _revision(cls, value: str) -> str:
        if not value or len(value) > 128:
            raise ValueError("catalog_revision must be a non-empty bounded string")
        return value

    @field_validator("manager_agent_tools", "basic_agent_tools")
    @classmethod
    def _tools(cls, value: list[str]) -> list[str]:
        if any(not isinstance(name, str) or not name for name in value):
            raise ValueError("tool names must be non-empty strings")
        if len(value) != len(set(value)):
            raise ValueError("duplicate tool names are not allowed")
        return value


@dataclass(frozen=True, slots=True)
class ProfileToolPromptPlan:
    tool_names: tuple[str, ...]
    business_tool_protocol: str
    tool_modules: tuple[str, ...]
    combination_modules: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AgentToolConfigurationSnapshot:
    schema_version: int
    catalog_revision: str
    manager_agent: ProfileToolPromptPlan
    basic_agent: ProfileToolPromptPlan
    fingerprint: str

    def public_metadata(self, *, locked: bool = True) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "catalog_revision": self.catalog_revision,
            "manager_agent_tools": list(self.manager_agent.tool_names),
            "basic_agent_tools": list(self.basic_agent.tool_names),
            "locked": bool(locked),
            "fingerprint": self.fingerprint,
        }


def _clean_prompt_value(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgentToolConfigurationError(
            "invalid_tool_configuration", f"{field_name} must be a non-empty string"
        )
    return value.strip()


def _business_tool_names_in_text(text: str) -> tuple[str, ...]:
    """Find business tool identifiers using the same boundary as rendering."""

    return tuple(
        name
        for name in BUSINESS_TOOL_NAMES
        if re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
            text,
        )
    )


class BusinessToolCatalog:
    """Canonical ordered catalog and deterministic prompt compiler."""

    def __init__(
        self,
        *,
        prompt_config: Mapping[str, object] | None = None,
        available_tool_names: Sequence[str] | None = None,
        default_profile_tools: Mapping[str, Sequence[str]] | None = None,
        tool_signatures: Mapping[str, object] | None = None,
    ) -> None:
        if prompt_config is not None and not isinstance(prompt_config, Mapping):
            raise AgentToolConfigurationError(
                "invalid_tool_configuration", "prompt_config must be a mapping"
            )
        raw = {} if prompt_config is None else prompt_config
        available = set(
            BUSINESS_TOOL_NAMES if available_tool_names is None else available_tool_names
        )
        unknown_available = available - set(BUSINESS_TOOL_NAMES)
        if unknown_available:
            raise AgentToolConfigurationError(
                "invalid_tool_configuration",
                f"unavailable business tools: {sorted(unknown_available)}",
            )
        self.available_tool_names = frozenset(available)
        self.tool_signatures = dict(tool_signatures or {})
        raw_defaults = (
            DEFAULT_PROFILE_TOOLS
            if default_profile_tools is None
            else default_profile_tools
        )
        if not isinstance(raw_defaults, Mapping):
            raise AgentToolConfigurationError(
                "invalid_tool_configuration",
                "defaults must be a mapping for manager_agent and basic_agent",
            )
        if set(raw_defaults) != PROFILE_NAMES:
            raise AgentToolConfigurationError("invalid_tool_configuration", "defaults must contain manager_agent and basic_agent")
        normalized_defaults: dict[str, tuple[str, ...]] = {}
        for profile, raw_values in raw_defaults.items():
            if (
                isinstance(raw_values, (str, bytes))
                or not isinstance(raw_values, Sequence)
            ):
                raise AgentToolConfigurationError(
                    "invalid_tool_configuration",
                    f"{profile} defaults must be a list of business tools",
                )
            values = tuple(raw_values)
            normalized_defaults[profile] = values
        self.default_profile_tools = normalized_defaults
        for profile, values in self.default_profile_tools.items():
            if any(not isinstance(name, str) for name in values):
                raise AgentToolConfigurationError(
                    "invalid_tool_configuration",
                    f"{profile} defaults must contain only business tool names",
                )
            if len(values) != len(set(values)):
                raise AgentToolConfigurationError(
                    "invalid_tool_configuration",
                    f"duplicate default tools for {profile}",
                )
            invalid_defaults = [
                name for name in values
                if name not in BUSINESS_TOOL_NAMES
                or name not in self.available_tool_names
            ]
            if invalid_defaults:
                raise AgentToolConfigurationError(
                    "invalid_tool_configuration",
                    f"unknown, unavailable, or special default tools for {profile}: {invalid_defaults}",
                )
        self.business_tool_protocol = _clean_prompt_value(
            raw.get("business_tool_protocol"),
            "business_tool_protocol",
        )
        protocol_leaks = _business_tool_names_in_text(self.business_tool_protocol)
        if protocol_leaks:
            raise AgentToolConfigurationError(
                "invalid_tool_configuration",
                f"business_tool_protocol must not name business tools: {list(protocol_leaks)}",
            )
        modules_raw = raw.get("business_tool_modules", {})
        self.tool_modules: dict[str, dict[str, object]] = {}
        if modules_raw is not None and not isinstance(modules_raw, Mapping):
            raise AgentToolConfigurationError("invalid_tool_configuration", "business_tool_modules must be a mapping")
        for name, item in (modules_raw or {}).items():
            if name not in BUSINESS_TOOL_NAMES or not isinstance(item, Mapping):
                raise AgentToolConfigurationError("invalid_tool_configuration", f"invalid business tool module: {name}")
            order = item.get("order")
            if isinstance(order, bool) or not isinstance(order, int):
                raise AgentToolConfigurationError("invalid_tool_configuration", f"invalid order for {name}")
            module_prompt = _clean_prompt_value(item.get("prompt"), f"{name}.prompt")
            module_description = _clean_prompt_value(
                item.get("description"), f"{name}.description"
            )
            module_text = f"{module_description}\n{module_prompt}"
            module_leaks = tuple(
                leak for leak in _business_tool_names_in_text(module_text)
                if leak != name
            )
            if module_leaks:
                raise AgentToolConfigurationError(
                    "invalid_tool_configuration",
                    f"{name} prompt module names other business tools: {list(module_leaks)}",
                )
            self.tool_modules[name] = {
                "order": order,
                "label": _clean_prompt_value(item.get("label"), f"{name}.label"),
                "description": module_description,
                "prompt": module_prompt,
            }
        active_module_names = set(self.tool_modules)
        if active_module_names != self.available_tool_names:
            missing = sorted(self.available_tool_names - active_module_names)
            extra = sorted(active_module_names - self.available_tool_names)
            raise AgentToolConfigurationError(
                "invalid_tool_configuration",
                f"prompt modules must exactly match active selectable catalog; missing={missing}, extra={extra}",
            )
        combinations_raw = raw.get("tool_prompt_combinations", [])
        if not isinstance(combinations_raw, Sequence) or isinstance(combinations_raw, (str, bytes)):
            raise AgentToolConfigurationError("invalid_tool_configuration", "tool_prompt_combinations must be a list")
        self.combinations: tuple[dict[str, object], ...] = tuple()
        seen_ids: set[str] = set()
        normalized_combinations: list[dict[str, object]] = []
        for item in combinations_raw:
            if not isinstance(item, Mapping):
                raise AgentToolConfigurationError("invalid_tool_configuration", "combination must be a mapping")
            ident = _clean_prompt_value(item.get("id"), "combination.id")
            requires = item.get("requires_all")
            if ident in seen_ids or not isinstance(requires, Sequence) or isinstance(requires, (str, bytes)):
                raise AgentToolConfigurationError("invalid_tool_configuration", f"invalid combination: {ident}")
            req = tuple(requires)
            if (
                not req
                or any(not isinstance(name, str) for name in req)
                or len(req) != len(set(req))
                or any(name not in self.available_tool_names for name in req)
            ):
                raise AgentToolConfigurationError("invalid_tool_configuration", f"invalid requires_all for {ident}")
            combination_prompt = _clean_prompt_value(
                item.get("prompt"), f"{ident}.prompt"
            )
            combination_leaks = tuple(
                name for name in _business_tool_names_in_text(combination_prompt)
                if name not in req
            )
            if combination_leaks:
                raise AgentToolConfigurationError(
                    "invalid_tool_configuration",
                    f"{ident} combination prompt names tools outside requires_all: {list(combination_leaks)}",
                )
            normalized_combinations.append({
                "id": ident,
                "requires_all": req,
                "prompt": combination_prompt,
            })
            seen_ids.add(ident)
        self.combinations = tuple(normalized_combinations)
        self._validate_modules()
        self.catalog_revision = self._compute_revision()

    def _validate_modules(self) -> None:
        orders: set[int] = set()
        for name, item in self.tool_modules.items():
            order = item["order"]
            if order in orders:
                raise AgentToolConfigurationError("invalid_tool_configuration", "tool module orders must be unique")
            orders.add(order)

    def _public_content(self) -> dict[str, object]:
        return {
            "schema_version": TOOL_CONFIGURATION_SCHEMA_VERSION,
            "tools": [
                {"name": name, **self.tool_modules[name]}
                for name in BUSINESS_TOOL_NAMES if name in self.tool_modules
            ],
            "business_tool_protocol": self.business_tool_protocol,
            "combinations": list(self.combinations),
            "available": [name for name in BUSINESS_TOOL_NAMES if name in self.available_tool_names],
            "tool_signatures": {
                name: self.tool_signatures[name]
                for name in BUSINESS_TOOL_NAMES if name in self.tool_signatures
            },
        }

    def _compute_revision(self) -> str:
        raw = json.dumps(self._public_content(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    def public_metadata(self) -> dict[str, object]:
        tools = [
            {
                "name": name,
                "order": self.tool_modules[name]["order"],
                "label": self.tool_modules[name]["label"],
                "description": self.tool_modules[name]["description"],
            }
            for name in BUSINESS_TOOL_NAMES
            if name in self.available_tool_names
        ]
        return {
            "schema_version": TOOL_CONFIGURATION_SCHEMA_VERSION,
            "catalog_revision": self.catalog_revision,
            # ``tools`` is retained for older clients; the canonical field is
            # ``business_tool_catalog`` and is deliberately identical.
            "tools": tools,
            "business_tool_catalog": tools,
            "default_profile_tools": {
                profile: list(names)
                for profile, names in self.default_profile_tools.items()
            },
            "tool_configuration_schema_version": TOOL_CONFIGURATION_SCHEMA_VERSION,
            "tool_catalog_revision": self.catalog_revision,
        }

    def _normalize(self, names: Sequence[str]) -> tuple[str, ...]:
        if not isinstance(names, Sequence) or isinstance(names, (str, bytes)):
            raise AgentToolConfigurationError("invalid_tool_configuration", "tool selection must be a list")
        values = list(names)
        if len(values) != len(set(values)):
            raise AgentToolConfigurationError("invalid_tool_configuration", "duplicate tool names are not allowed")
        unknown = [name for name in values if name not in self.available_tool_names]
        if unknown:
            raise AgentToolConfigurationError("invalid_tool_configuration", f"unknown or unavailable business tools: {unknown}")
        return tuple(name for name in BUSINESS_TOOL_NAMES if name in values)

    def _plan(self, names: Sequence[str]) -> ProfileToolPromptPlan:
        normalized = self._normalize(names)
        chosen = set(normalized)
        modules = tuple(
            str(self.tool_modules[name]["prompt"])
            for name in BUSINESS_TOOL_NAMES
            if name in chosen and name in self.tool_modules
        )
        combinations = tuple(
            str(item["prompt"])
            for item in self.combinations
            if set(item["requires_all"]).issubset(chosen)
        )
        return ProfileToolPromptPlan(
            tool_names=normalized,
            business_tool_protocol=self.business_tool_protocol if normalized else "",
            tool_modules=modules,
            combination_modules=combinations,
        )

    def resolve_candidate(self, candidate: AgentToolConfigurationRequest | None) -> AgentToolConfigurationSnapshot:
        if candidate is None:
            selections = self.default_profile_tools
        else:
            if (
                candidate.schema_version != TOOL_CONFIGURATION_SCHEMA_VERSION
                or candidate.catalog_revision != self.catalog_revision
            ):
                raise AgentToolConfigurationError("tool_catalog_changed", "Tool catalog has changed; reload configuration.")
            selections = {
                "manager_agent": candidate.manager_agent_tools,
                "basic_agent": candidate.basic_agent_tools,
            }
        manager = self._plan(selections["manager_agent"])
        basic = self._plan(selections["basic_agent"])
        body = {
            "schema_version": TOOL_CONFIGURATION_SCHEMA_VERSION,
            "catalog_revision": self.catalog_revision,
            "manager": manager,
            "basic": basic,
        }
        raw = json.dumps(
            {**body, "manager": asdict(manager), "basic": asdict(basic)},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return AgentToolConfigurationSnapshot(
            schema_version=TOOL_CONFIGURATION_SCHEMA_VERSION,
            catalog_revision=self.catalog_revision,
            manager_agent=manager,
            basic_agent=basic,
            fingerprint=hashlib.sha256(raw.encode()).hexdigest(),
        )

    def assert_compatible(self, snapshot: AgentToolConfigurationSnapshot, candidate: AgentToolConfigurationRequest | None) -> None:
        if candidate is None:
            return
        candidate_snapshot = self.resolve_candidate(candidate)
        if candidate_snapshot.fingerprint != snapshot.fingerprint:
            raise AgentToolConfigurationError("tool_configuration_locked", "Tool configuration is already frozen for this Agent session.")


def default_candidate(catalog: BusinessToolCatalog) -> AgentToolConfigurationRequest:
    return AgentToolConfigurationRequest(
        schema_version=TOOL_CONFIGURATION_SCHEMA_VERSION,
        catalog_revision=catalog.catalog_revision,
        manager_agent_tools=list(catalog.default_profile_tools["manager_agent"]),
        basic_agent_tools=list(catalog.default_profile_tools["basic_agent"]),
    )


def build_business_tool_catalog(config) -> BusinessToolCatalog:
    """Build the active catalog from the parsed application config."""

    prompts = getattr(config, "agent_prompts", None)
    return BusinessToolCatalog(
        prompt_config={
            "business_tool_protocol": getattr(prompts, "business_tool_protocol", ""),
            "business_tool_modules": getattr(prompts, "business_tool_modules", {}),
            "tool_prompt_combinations": getattr(prompts, "tool_prompt_combinations", []),
        },
        # The composite Wikipedia tool is selectable even when the legacy
        # web_tools switch is false.  That switch still controls the legacy
        # unfiltered tool builder; session-scoped catalog choice is the source
        # of truth for Agent runtime construction.
        available_tool_names=BUSINESS_TOOL_NAMES,
        default_profile_tools=getattr(getattr(config, "agent", None), "default_profile_tools", None),
    )
