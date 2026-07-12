"""Private-profile grocery route formatting for a target to-do list."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Mapping

from homeassistant.components.todo import TodoItem
from homeassistant.components.todo.const import DATA_COMPONENT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

ROUTE_HEADER_PREFIX = "[Route] "
LEGACY_ROUTE_HEADER_PREFIX = "\U0001F4CD "
# Grocery items are usually category-level guesses; prefer a plausible route stop
# over the catch-all fallback while still rejecting a model's zero-confidence output.
AI_CONFIDENCE_THRESHOLD = 0.20


@dataclass(frozen=True, slots=True)
class StoreLocation:
    """One ordered stop from a private route profile."""

    id: str
    order: int
    label: str


@dataclass(frozen=True, slots=True)
class RouteProfile:
    """Validated route data stored in a Home Assistant config entry."""

    locations: tuple[StoreLocation, ...]
    item_locations: Mapping[str, str]
    fallback_id: str

    @property
    def locations_by_id(self) -> dict[str, StoreLocation]:
        """Return locations indexed by their stable private IDs."""
        return {location.id: location for location in self.locations}


DEFAULT_ROUTE_PROFILE: dict[str, Any] = {
    "stops": [{"id": "unmapped", "order": 9999, "label": "Unmapped items"}],
    "items": {},
    "fallback": "unmapped",
}


def _key(summary: str) -> str:
    return summary.strip().casefold()


def parse_route_profile(value: Any) -> RouteProfile:
    """Validate a private route profile and return its runtime form."""
    if value is None:
        value = DEFAULT_ROUTE_PROFILE
    if not isinstance(value, dict):
        raise ValueError("Route profile must be an object")

    raw_stops = value.get("stops")
    raw_items = value.get("items", {})
    fallback_id = str(value.get("fallback", "")).strip()
    if not isinstance(raw_stops, list) or not raw_stops:
        raise ValueError("Route profile must contain at least one stop")
    if not isinstance(raw_items, dict):
        raise ValueError("Route profile items must be an object")

    locations: list[StoreLocation] = []
    location_ids: set[str] = set()
    for raw_stop in raw_stops:
        if not isinstance(raw_stop, dict):
            raise ValueError("Every route stop must be an object")
        stop_id = str(raw_stop.get("id", "")).strip()
        label = str(raw_stop.get("label", "")).strip()
        order = raw_stop.get("order")
        if not stop_id or not label or not isinstance(order, int):
            raise ValueError("Every route stop needs a string id, integer order, and label")
        if stop_id in location_ids:
            raise ValueError(f"Duplicate route stop id: {stop_id}")
        location_ids.add(stop_id)
        locations.append(StoreLocation(stop_id, order, label))

    if fallback_id not in location_ids:
        raise ValueError("Route fallback must reference a configured stop id")

    item_locations: dict[str, str] = {}
    for summary, stop_id_value in raw_items.items():
        normalized_summary = _key(str(summary))
        stop_id = str(stop_id_value).strip()
        if not normalized_summary or stop_id not in location_ids:
            raise ValueError(f"Invalid item route assignment for: {summary}")
        item_locations[normalized_summary] = stop_id

    return RouteProfile(
        tuple(sorted(locations, key=lambda location: (location.order, location.label.casefold()))),
        item_locations,
        fallback_id,
    )


def is_route_header(summary: str | None) -> bool:
    """Whether an item is workflow-owned presentation metadata."""
    return bool(
        summary
        and summary.startswith((ROUTE_HEADER_PREFIX, LEGACY_ROUTE_HEADER_PREFIX))
    )


class GroceryRouteSorter:
    """Sort a target to-do list using a private route profile."""

    def __init__(
        self,
        hass: HomeAssistant,
        source_entity: str,
        target_entity: str,
        route_profile: Any = None,
        *,
        ai_entity_id: str | None = None,
        cache_key: str | None = None,
    ) -> None:
        self._hass = hass
        self._source_entity = source_entity
        self._target_entity = target_entity
        self._profile = parse_route_profile(route_profile)
        self._ai_entity_id = ai_entity_id
        self._learned_routes = Store(
            hass, 1, f"grocery_list_workflow.{cache_key or target_entity}.learned_routes_v5"
        )
        self._learned_item_locations: dict[str, str] | None = None
        self._unclassified_items: set[str] | None = None
        self._lock = asyncio.Lock()

    async def _items(self, entity_id: str) -> list[dict[str, Any]]:
        response = await self._hass.services.async_call(
            "todo", "get_items", target={"entity_id": entity_id}, blocking=True, return_response=True
        )
        return response.get(entity_id, {}).get("items", [])

    def _location(self, summary: str) -> StoreLocation:
        locations = self._profile.locations_by_id
        item_locations = {**self._profile.item_locations, **(self._learned_item_locations or {})}
        location_id = item_locations.get(_key(summary), self._profile.fallback_id)
        return locations[location_id]

    async def _async_load_learned_locations(self) -> None:
        """Load private AI suggestions saved for this workflow entry."""
        if self._learned_item_locations is not None:
            return
        cached = await self._learned_routes.async_load()
        raw_items = cached.get("items", {}) if isinstance(cached, dict) else {}
        raw_unclassified = (
            cached.get("unclassified", []) if isinstance(cached, dict) else []
        )
        valid_ids = set(self._profile.locations_by_id)
        self._learned_item_locations = {
            _key(str(summary)): str(location_id)
            for summary, location_id in raw_items.items()
            if str(location_id) in valid_ids
        }
        self._unclassified_items = {
            _key(str(summary)) for summary in raw_unclassified if str(summary).strip()
        }

    async def _async_classify_unmapped(self, summaries: list[str]) -> None:
        """Ask an opt-in HA AI task to classify only previously unseen items."""
        await self._async_load_learned_locations()
        if not self._ai_entity_id:
            return

        known = {**self._profile.item_locations, **self._learned_item_locations}
        unresolved = sorted(
            {
                _key(summary): summary
                for summary in summaries
                if _key(summary) not in known
                and _key(summary) not in self._unclassified_items
            }.values()
        )
        if not unresolved:
            return

        valid_ids = set(self._profile.locations_by_id)
        valid_ids.discard(self._profile.fallback_id)
        accepted: dict[str, str] = {}
        processed: set[str] = set()
        allowed_stops = [
            {"id": location.id, "label": location.label}
            for location in self._profile.locations
            if location.id in valid_ids
        ]
        for summary in unresolved:
            classification = await self._async_classify_item(summary, allowed_stops)
            if classification is None:
                continue
            processed.add(_key(summary))
            stop_id = str(classification.get("stop_id", "")).strip()
            try:
                confidence = float(classification.get("confidence", 0))
            except (TypeError, ValueError):
                continue
            if stop_id in valid_ids and confidence >= AI_CONFIDENCE_THRESHOLD:
                accepted[_key(summary)] = stop_id

        self._learned_item_locations.update(accepted)
        self._unclassified_items.update(
            summary_key for summary_key in processed if summary_key not in accepted
        )
        await self._learned_routes.async_save(
            {
                "items": self._learned_item_locations,
                "unclassified": sorted(self._unclassified_items),
            }
        )

    async def _async_classify_item(
        self, summary: str, allowed_stops: list[dict[str, str]]
    ) -> Mapping[str, Any] | None:
        """Classify one item with a schema that the AI provider can enforce."""
        try:
            response = await self._hass.services.async_call(
                "ai_task",
                "generate_data",
                {
                    "task_name": "Classify a grocery-list item",
                    "instructions": (
                        f"Classify this grocery item: {summary!r}. Choose the most likely "
                        "store-route stop from the allowed options. Make a sensible "
                        "grocery-category guess and report confidence from 0 to 1.\n\n"
                        f"Allowed route stops: {json.dumps(allowed_stops)}"
                    ),
                    "entity_id": self._ai_entity_id,
                    "structure": {
                        "stop_id": {
                            "selector": {
                                "select": {
                                    "options": [stop["id"] for stop in allowed_stops]
                                }
                            },
                            "description": "The most likely allowed route stop ID.",
                            "required": True,
                        },
                        "confidence": {
                            "selector": {
                                "number": {"min": 0, "max": 1, "step": 0.01}
                            },
                            "description": "Confidence from 0 to 1.",
                            "required": True,
                        },
                    },
                },
                blocking=True,
                return_response=True,
            )
        except Exception:  # AI is optional; sorting must still work without it.
            return None
        return self._extract_classification(response)

    @staticmethod
    def _extract_classification(response: Any) -> Mapping[str, Any] | None:
        """Extract one structured AI classification from an HA service response."""
        if not isinstance(response, Mapping):
            return None
        candidates = [response]
        seen: set[int] = set()
        while candidates:
            candidate = candidates.pop()
            if id(candidate) in seen:
                continue
            seen.add(id(candidate))
            if "stop_id" in candidate and "confidence" in candidate:
                return candidate
            candidates.extend(
                value for value in candidate.values() if isinstance(value, Mapping)
            )
        return None

    async def async_sort(self) -> None:
        """Create target-only headers and place source items in route order."""
        if self._lock.locked():
            return
        async with self._lock:
            source_items, target_items = await asyncio.gather(
                self._items(self._source_entity), self._items(self._target_entity)
            )
            source_summaries = [
                item["summary"]
                for item in source_items
                if item.get("summary") and not is_route_header(item["summary"])
            ]
            await self._async_classify_unmapped(source_summaries)
            planned = sorted(
                (
                    (self._location(item["summary"]), item["summary"])
                    for item in source_items
                    if item.get("summary") and not is_route_header(item["summary"])
                ),
                key=lambda item: (item[0].order, item[1].casefold()),
            )
            target_entity = self._hass.data[DATA_COMPONENT].get_entity(self._target_entity)
            if target_entity is None:
                raise ValueError(f"To-do entity not found: {self._target_entity}")

            headers = {
                item["summary"]: item
                for item in target_items
                if is_route_header(item.get("summary"))
            }
            needed = {f"{ROUTE_HEADER_PREFIX}{location.label}" for location, _ in planned}
            stale_uids = [item["uid"] for summary, item in headers.items() if summary not in needed]
            if stale_uids:
                await target_entity.async_delete_todo_items(stale_uids)
            for location in self._profile.locations:
                header = f"{ROUTE_HEADER_PREFIX}{location.label}"
                if header in needed and header not in headers:
                    await target_entity.async_create_todo_item(TodoItem(summary=header))

            await target_entity.async_update()
            target_items = await self._items(self._target_entity)
            items_by_name = {
                _key(item["summary"]): item
                for item in target_items
                if item.get("summary") and not is_route_header(item["summary"])
            }
            headers = {
                item["summary"]: item
                for item in target_items
                if is_route_header(item.get("summary"))
            }

            previous_uid: str | None = None
            active_location: StoreLocation | None = None
            for location, summary in planned:
                if location != active_location:
                    header = headers[f"{ROUTE_HEADER_PREFIX}{location.label}"]
                    await target_entity.async_move_todo_item(header["uid"], previous_uid)
                    previous_uid = header["uid"]
                    active_location = location
                if item := items_by_name.get(_key(summary)):
                    await target_entity.async_move_todo_item(item["uid"], previous_uid)
                    previous_uid = item["uid"]
