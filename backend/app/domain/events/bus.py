"""In-process async event bus. Subscribers must be fast; slow work belongs in background tasks."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog

from app.domain.events.events import DomainEvent

Handler = Callable[[DomainEvent], Awaitable[None]]
log = structlog.get_logger("events")


class EventBus:
    def __init__(self) -> None:
        self._handlers: list[Handler] = []

    def subscribe(self, handler: Handler) -> None:
        self._handlers.append(handler)

    async def publish(self, event: DomainEvent) -> None:
        for handler in self._handlers:
            try:
                await handler(event)
            except Exception:  # one subscriber failing must not break the others
                log.exception("event_handler_failed", event_name=event.name)

    async def publish_all(self, events: list[DomainEvent]) -> None:
        for event in events:
            await self.publish(event)
