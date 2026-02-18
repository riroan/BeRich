from typing import Callable, Any
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
import asyncio
import logging

logger = logging.getLogger(__name__)


class EventType(Enum):
    # Market data events
    QUOTE_UPDATE = auto()
    BAR_UPDATE = auto()

    # Order events
    ORDER_SUBMITTED = auto()
    ORDER_FILLED = auto()
    ORDER_PARTIAL_FILLED = auto()
    ORDER_CANCELLED = auto()
    ORDER_REJECTED = auto()

    # Position events
    POSITION_OPENED = auto()
    POSITION_CLOSED = auto()
    POSITION_UPDATED = auto()

    # Signal events
    SIGNAL_GENERATED = auto()

    # System events
    BROKER_CONNECTED = auto()
    BROKER_DISCONNECTED = auto()
    RISK_LIMIT_BREACHED = auto()
    ERROR = auto()


@dataclass
class Event:
    event_type: EventType
    data: Any
    timestamp: datetime
    source: str


class EventBus:
    """Async event bus with pub/sub pattern"""

    def __init__(self):
        self._subscribers: dict[EventType, list[Callable]] = {}
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False
        self._task: asyncio.Task | None = None

    def subscribe(self, event_type: EventType, handler: Callable) -> None:
        """Subscribe to an event type"""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(handler)
        logger.debug(f"Subscribed {handler.__name__} to {event_type}")

    def unsubscribe(self, event_type: EventType, handler: Callable) -> None:
        """Unsubscribe from an event type"""
        if event_type in self._subscribers:
            self._subscribers[event_type].remove(handler)

    async def publish(self, event: Event) -> None:
        """Publish an event"""
        await self._queue.put(event)

    async def start(self) -> None:
        """Start the event processing loop"""
        self._running = True
        self._task = asyncio.create_task(self._process_loop())
        logger.info("EventBus started")

    async def stop(self) -> None:
        """Stop the event processing loop"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("EventBus stopped")

    async def _process_loop(self) -> None:
        """Main event processing loop"""
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                await self._dispatch(event)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def _dispatch(self, event: Event) -> None:
        """Dispatch event to all subscribers"""
        handlers = self._subscribers.get(event.event_type, [])
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception as e:
                logger.error(f"Error in event handler {handler.__name__}: {e}")
                if event.event_type != EventType.ERROR:
                    error_event = Event(
                        event_type=EventType.ERROR,
                        data={"error": str(e), "original_event": event},
                        timestamp=datetime.now(),
                        source="EventBus",
                    )
                    await self._dispatch(error_event)
