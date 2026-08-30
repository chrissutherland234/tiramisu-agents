"""Explicit process-definition registration performed outside workflow code."""

from collections.abc import Iterable
from pathlib import Path

import yaml

from tiramisu_agents.processes.definitions import DefinitionStatus, ProcessDefinition


class DuplicateProcessDefinition(ValueError):
    """Raised when an immutable definition identity is registered twice."""


class AmbiguousTrigger(ValueError):
    """Raised when more than one enabled definition claims a trigger event."""


class ProcessDefinitionRegistry:
    def __init__(self, definitions: Iterable[ProcessDefinition]) -> None:
        registered: dict[tuple[str, str], ProcessDefinition] = {}
        for definition in definitions:
            key = (definition.id, definition.version)
            if key in registered:
                raise DuplicateProcessDefinition(f"duplicate process definition: {key}")
            registered[key] = definition
        self._definitions = registered

    @classmethod
    def from_yaml_files(cls, paths: Iterable[Path]) -> "ProcessDefinitionRegistry":
        definitions: list[ProcessDefinition] = []
        for path in paths:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
            definitions.append(ProcessDefinition.model_validate(document))
        return cls(definitions)

    def get(self, definition_id: str, version: str) -> ProcessDefinition:
        try:
            return self._definitions[(definition_id, version)]
        except KeyError as error:
            raise LookupError(f"unknown process definition: {(definition_id, version)}") from error

    def resolve_trigger(
        self, event_type: str, *, include_drafts: bool = False
    ) -> ProcessDefinition | None:
        enabled_statuses = (
            {DefinitionStatus.PUBLISHED, DefinitionStatus.DRAFT}
            if include_drafts
            else {DefinitionStatus.PUBLISHED}
        )
        candidates = [
            definition
            for definition in self._definitions.values()
            if definition.status in enabled_statuses and event_type in definition.trigger_events
        ]
        if len(candidates) > 1:
            identities = sorted((item.id, item.version) for item in candidates)
            raise AmbiguousTrigger(f"ambiguous trigger {event_type}: {identities}")
        return candidates[0] if candidates else None
