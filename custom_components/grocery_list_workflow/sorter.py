"""Private-profile grocery route formatting for a target to-do list."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Mapping

from homeassistant.components.todo import TodoItem
from homeassistant.components.todo.const import DATA_COMPONENT
from homeassistant.core import HomeAssistant

ROUTE_HEADER_PREFIX = "[Route] "
LEGACY_ROUTE_HEADER_PREFIX = "\U0001F4CD "


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
    ) -> None:
        self._hass = hass
        self._source_entity = source_entity
        self._target_entity = target_entity
        self._profile = parse_route_profile(route_profile)
        self._lock = asyncio.Lock()

    async def _items(self, entity_id: str) -> list[dict[str, Any]]:
        response = await self._hass.services.async_call(
            "todo", "get_items", target={"entity_id": entity_id}, blocking=True, return_response=True
        )
        return response.get(entity_id, {}).get("items", [])

    def _location(self, summary: str) -> StoreLocation:
        locations = self._profile.locations_by_id
        location_id = self._profile.item_locations.get(_key(summary), self._profile.fallback_id)
        return locations[location_id]

    async def async_sort(self) -> None:
        """Create target-only headers and place source items in route order."""
        if self._lock.locked():
            return
        async with self._lock:
            source_items, target_items = await asyncio.gather(
                self._items(self._source_entity), self._items(self._target_entity)
            )
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
