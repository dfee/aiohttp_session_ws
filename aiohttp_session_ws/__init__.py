import asyncio
import collections.abc
import functools
import inspect
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    Hashable,
    Iterator,
    Optional,
    Set,
    Tuple,
    Union,
)
import uuid

from aiohttp import web
import aiohttp_session

__version__ = "1.1.1"

DEFAULT_ID_FACTORY = lambda request: uuid.uuid4().hex
DEFAULT_SESSION_KEY = "aiohttp_session_ws_id"
REGISTRY_KEY = "aiohttp_session_ws_registry"


async def get_session_ws_id(request: web.Request) -> Hashable:
    """
    Get the "session ws id" from a session
    """
    return await request.app[REGISTRY_KEY].get_id(request)


async def new_session_ws_id(request: web.Request) -> None:
    """
    Generate and set a new "session ws id" on a session
    """
    return await request.app[REGISTRY_KEY].new_id(request)


async def delete_session_ws_id(request: web.Request) -> None:
    """
    Remove a "session ws id" from a session
    """
    return await request.app[REGISTRY_KEY].delete_id(request)


async def ensure_session_ws_id(request: web.Request) -> None:
    """
    Add a "session ws id" to a session (if not present)
    """
    return await request.app[REGISTRY_KEY].ensure_id(request)


async def schedule_close_all_session_ws(
    request: web.Request, response: Union[web.Response, web.HTTPFound]
) -> None:
    """
    Removes the wesocket session_ws_id from the session, disables the response's
    keep alive (for timely shutdown), and schedules the removal of websockets
    after the response has been sent.
    """
    return await request.app[REGISTRY_KEY].schedule_close_all_session(
        request, response
    )


@web.middleware
async def session_ws_middleware(
    request: web.Request, handler: Callable[[web.Request], web.StreamResponse]
) -> web.StreamResponse:
    """
    Sets the "session_ws id" on outgoing requests.
    """
    await ensure_session_ws_id(request)
    return await handler(request)


class SessionWSRegistry(collections.abc.Mapping):
    """
    Stores and manages a set of WebSocketResponses by session_ws id
    """

    def __init__(
        self,
        *,
        id_factory: Union[
            Callable[[web.Request], Hashable],
            Callable[[web.Request], Awaitable[Hashable]],
        ] = DEFAULT_ID_FACTORY,
        session_key: Hashable = DEFAULT_SESSION_KEY
    ):
        self._registry = {}  # type: Dict[str, Set[web.WebSocketResponse]]
        self.id_factory = id_factory
        self.session_key = session_key

    def __getitem__(self, key: str) -> Set[web.WebSocketResponse]:
        return self._registry[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._registry)

    def __len__(self) -> int:
        return len(self._registry)

    async def generate_id(self, request: web.Request) -> Hashable:
        result = self.id_factory(request)
        return await result if inspect.isawaitable(result) else result

    async def get_id(self, request: web.Request) -> Hashable:
        """
        Get the session_ws id from a request
        """
        session = await aiohttp_session.get_session(request)
        return session.get(self.session_key)

    async def new_id(self, request: web.Request) -> None:
        """
        Generate and set the session_ws id on a request
        """
        session = await aiohttp_session.get_session(request)
        session[self.session_key] = await self.generate_id(request)

    async def delete_id(self, request: web.Request) -> None:
        """
        Remove the session_ws id from a request
        """
        session = await aiohttp_session.get_session(request)
        session.pop(self.session_key, None)

    async def ensure_id(self, request: web.Request) -> None:
        """
        Ensure the request has a session_ws id
        """
        if await self.get_id(request) is None:
            await self.new_id(request)

    async def close_all(self) -> None:
        """
        Close all known websockets.
        """
        wsrs = set().union(*self.values())
        await asyncio.gather(*[wsr.close() for wsr in wsrs])

    async def close_all_session(self, session_ws_id: Hashable) -> None:
        """
        Close all websockets that share this session.
        Unlike `schedule_close_all_session`, `close_all_session` takes an id,
        because the request might have a new session_ws id by the time it
        arrives here.
        """
        wsrs = self.get(session_ws_id, set())
        await asyncio.gather(*[wsr.close() for wsr in wsrs])

    async def schedule_close_all_session(
        self, request: web.Request, response: Union[web.Response, web.HTTPFound]
    ) -> None:
        """
        Removes the wesocket session_ws_id from the session, disables the
        response's keep alive (for timely shutdown), and schedules the removal
        of websockets after the response has been sent.
        """
        id_ = await self.get_id(request)

        async def onclose() -> None:
            await request.task
            await self.close_all_session(id_)

        asyncio.ensure_future(onclose())
        response.force_close()

    def register(
        self, session_ws_id: Hashable, wsr: web.WebSocketResponse
    ) -> None:
        """
        Adds the session_ws_id, wsr pair to the registry.
        """
        wsrs = self._registry.setdefault(session_ws_id, set())
        wsrs.add(wsr)

    def unregister(
        self, session_ws_id: Hashable, wsr: web.WebSocketResponse
    ) -> None:
        """
        Removes the session_ws_id, wsr pair from the registry, and removes
        the session_ws_id from the registry's keys if there are no more
        associated wsrs.
        """
        if session_ws_id not in self._registry:
            return
        if self._registry[session_ws_id] == set([wsr]):
            del self._registry[session_ws_id]
        else:
            self._registry[session_ws_id].remove(wsr)


def setup(app: web.Application, registry: SessionWSRegistry) -> None:
    """
    Adds the registry to the applicati, as well as an on_shutdown hook that
    tears down all websockets on application shutdown.
    """

    async def on_shutdown(app: web.Application) -> None:
        await app[REGISTRY_KEY].close_all()

    app[REGISTRY_KEY] = registry
    app.on_shutdown.append(on_shutdown)


class session_ws:  # pylint: disable=C0103, invalid-name
    """
    AsyncContextManager that returns a prepared aiothtp.web.WebSocketResponse

    :param request: the aiohttp.web.Request to upgrade to websockets
    :param options: constructor options for to aiohttp.web.WebSocketResponse
    """

    def __init__(self, request: web.Request, **options: Dict[str, Any]) -> None:
        self.request = request
        self.options = options
        self.response = None  # type: Optional[web.WebSocketResponse]
        self.session_ws_id = None  # type: Hashable

    @property
    def registry(self) -> SessionWSRegistry:
        return self.request.app[REGISTRY_KEY]

    async def __aenter__(self) -> web.WebSocketResponse:
        self.response = web.WebSocketResponse(**self.options)

        self.session_ws_id = await get_session_ws_id(self.request)
        if self.session_ws_id is None:
            await new_session_ws_id(self.request)
            self.session_ws_id = await get_session_ws_id(self.request)

        session = await aiohttp_session.get_session(self.request)
        if session._changed:
            storage = self.request[aiohttp_session.STORAGE_KEY]
            await storage.save_session(self.request, self.response, session)

        self.registry.register(self.session_ws_id, self.response)
        # send the session cookie along (if new)
        await self.response.prepare(self.request)

        return self.response

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # pylint: disable=C0103, invalid-name
        self.registry.unregister(self.session_ws_id, self.response)
