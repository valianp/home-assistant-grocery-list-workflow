"""Conservative two-way synchronization for Home Assistant to-do entities."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import DEFAULT_SYNC_MINUTES
from .sorter import is_route_header

_LOGGER = logging.getLogger(__name__)


class TodoSynchronizer:
    """Synchronize two to-do lists; the source wins simultaneous conflicts."""

    def __init__(
        self,
        hass: HomeAssistant,
        source_entity: str,
        target_entity: str,
        storage_key: str,
        on_content_change: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._hass = hass
        self._source_entity = source_entity
        self._target_entity = target_entity
        self._on_content_change = on_content_change
        self._lock = asyncio.Lock()
        self._unsub = None
        self._store: Store[dict[str, Any]] = Store(hass, 1, storage_key)
        self._snapshot: dict[str, dict[str, Any]] = {}

    async def async_start(self) -> None:
        """Start periodic synchronization without delaying integration setup."""
        stored = await self._store.async_load()
        self._snapshot = (stored or {}).get("items", {})
        self._unsub = async_track_time_interval(
            self._hass,
            lambda _: self._hass.add_job(self.async_periodic_sync),
            timedelta(minutes=DEFAULT_SYNC_MINUTES),
        )

    async def async_stop(self) -> None:
        """Stop periodic synchronization."""
        if self._unsub:
            self._unsub()
            self._unsub = None

    async def async_periodic_sync(self) -> bool:
        """Synchronize and sort only after additions or removals."""
        content_changed = await self.async_sync()
        if content_changed and self._on_content_change:
            try:
                await self._on_content_change()
            except Exception:
                _LOGGER.exception("Unable to sort the synchronized grocery list")
        return content_changed

    async def _items(self, entity_id: str) -> list[dict[str, Any]]:
        response = await self._hass.services.async_call(
            "todo", "get_items", target={"entity_id": entity_id}, blocking=True, return_response=True
        )
        return response.get(entity_id, {}).get("items", [])

    async def _add(self, entity_id: str, summary: str) -> None:
        await self._hass.services.async_call(
            "todo", "add_item", {"item": summary}, target={"entity_id": entity_id}, blocking=True
        )

    async def _status(self, entity_id: str, uid: str, status: str) -> None:
        await self._hass.services.async_call(
            "todo", "update_item", {"item": uid, "status": status}, target={"entity_id": entity_id}, blocking=True
        )

    async def _delete(self, entity_id: str, uid: str) -> None:
        await self._hass.services.async_call(
            "todo", "remove_item", {"item": uid}, target={"entity_id": entity_id}, blocking=True
        )

    @staticmethod
    def _index(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {
            item["summary"].strip().casefold(): item
            for item in items
            if item.get("summary") and not is_route_header(item["summary"])
        }

    async def _save_snapshot(self, source: list[dict[str, Any]], target: list[dict[str, Any]]) -> None:
        source_by_name = self._index(source)
        target_by_name = self._index(target)
        self._snapshot = {
            key: {"source_uid": source_by_name[key]["uid"], "target_uid": target_by_name[key]["uid"], "status": source_by_name[key].get("status")}
            for key in source_by_name.keys() & target_by_name.keys()
        }
        await self._store.async_save({"items": self._snapshot})

    async def async_sync(self) -> bool:
        """Reconcile both lists and return whether contents changed."""
        if self._lock.locked():
            return False
        async with self._lock:
            try:
                content_changed = False
                source, target = await asyncio.gather(self._items(self._source_entity), self._items(self._target_entity))
                source_by_name, target_by_name = self._index(source), self._index(target)

                if not self._snapshot:
                    for key, item in source_by_name.items():
                        if key not in target_by_name:
                            await self._add(self._target_entity, item["summary"])
                            content_changed = True
                    for key, item in target_by_name.items():
                        if key not in source_by_name:
                            await self._add(self._source_entity, item["summary"])
                            content_changed = True
                    source, target = await asyncio.gather(self._items(self._source_entity), self._items(self._target_entity))
                    await self._save_snapshot(source, target)
                    return content_changed

                for key, item in source_by_name.items():
                    if key not in target_by_name and key not in self._snapshot:
                        await self._add(self._target_entity, item["summary"])
                        content_changed = True
                for key, item in target_by_name.items():
                    if key not in source_by_name and key not in self._snapshot:
                        await self._add(self._source_entity, item["summary"])
                        content_changed = True

                for key, previous in self._snapshot.items():
                    source_item, target_item = source_by_name.get(key), target_by_name.get(key)
                    if source_item and target_item:
                        source_changed = source_item.get("status") != previous.get("status")
                        target_changed = target_item.get("status") != previous.get("status")
                        if source_changed and not target_changed:
                            await self._status(self._target_entity, target_item["uid"], source_item["status"])
                        elif target_changed and not source_changed:
                            await self._status(self._source_entity, source_item["uid"], target_item["status"])
                        elif source_changed and target_changed and source_item.get("status") != target_item.get("status"):
                            await self._status(self._target_entity, target_item["uid"], source_item["status"])
                    elif source_item and not target_item:
                        if source_item.get("status") == previous.get("status"):
                            await self._delete(self._source_entity, source_item["uid"])
                        else:
                            await self._add(self._target_entity, source_item["summary"])
                        content_changed = True
                    elif target_item and not source_item:
                        if target_item.get("status") == previous.get("status"):
                            await self._delete(self._target_entity, target_item["uid"])
                        else:
                            await self._add(self._source_entity, target_item["summary"])
                        content_changed = True

                source, target = await asyncio.gather(self._items(self._source_entity), self._items(self._target_entity))
                await self._save_snapshot(source, target)
                return content_changed
            except Exception:
                _LOGGER.exception("Unable to synchronize %s and %s", self._source_entity, self._target_entity)
                return False
