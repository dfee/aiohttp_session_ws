import asyncio
import json
import time
from unittest.mock import Mock
import uuid

from aiohttp import CookieJar, WSMessage, WSMsgType, web
from aiohttp.test_utils import make_mocked_request
import aiohttp_session
import pytest

from aiohttp_session_ws import (
    REGISTRY_KEY,
    DEFAULT_SESSION_KEY,
    SessionWSRegistry,
    __version__,
    delete_session_ws_id,
    ensure_session_ws_id,
    get_session_ws_id,
    schedule_close_all_session_ws,
    session_ws_middleware,
    new_session_ws_id,
    setup as setup_session_ws,
    with_session_ws,
)

# pylint: disable=C0103, invalid-name
# pylint: disable=R0201, no-self-use
# pylint: disable=W0621, redefined-outer-name


def make_mock_wsr():
    return Mock(spec=web.WebSocketResponse)


@pytest.fixture
def async_mock_call():
    async def async_mock_call_(*args, **kwargs):
        return (args, kwargs)

    return async_mock_call_


@pytest.fixture
def registry():
    return SessionWSRegistry()


@pytest.fixture
def req(registry):
    return make_mocked_request("GET", "/", app={REGISTRY_KEY: registry})


@pytest.fixture
def wsr():
    return make_mock_wsr()


@pytest.fixture
def cookie_jar(event_loop):
    return CookieJar(unsafe=True, loop=event_loop)


@pytest.fixture
async def client(event_loop, app, cookie_jar):
    from aiohttp.test_utils import TestServer, TestClient

    _server = TestServer(app, loop=event_loop)
    _client = TestClient(_server, loop=event_loop, cookie_jar=cookie_jar)
    await _client.start_server()
    yield _client
    await _client.close()


def test_version():
    assert __version__ == "1.0.0"


@pytest.mark.asyncio
async def test_get_session_ws_id(registry, req, async_mock_call):
    registry.get_id = async_mock_call
    assert await get_session_ws_id(req) == ((req,), {})


@pytest.mark.asyncio
async def test_new_session_ws_id(registry, req, async_mock_call):
    registry.new_id = async_mock_call
    assert await new_session_ws_id(req) == ((req,), {})


@pytest.mark.asyncio
async def test_delete_session_ws_id(registry, req, async_mock_call):
    registry.delete_id = async_mock_call
    assert await delete_session_ws_id(req) == ((req,), {})


@pytest.mark.asyncio
async def test_ensure_session_ws_id(registry, req, async_mock_call):
    registry.ensure_id = async_mock_call
    assert await ensure_session_ws_id(req) == ((req,), {})


@pytest.mark.asyncio
async def test_schedule_close_all_session_ws(registry, req, async_mock_call):
    resp = Mock()
    registry.schedule_close_all_session = async_mock_call
    assert await schedule_close_all_session_ws(req, resp) == ((req, resp), {})


@pytest.mark.asyncio
async def test_session_ws_middleware(req, registry, async_mock_call):
    response = web.Response()
    registry.ensure_id = Mock(side_effect=async_mock_call)

    async def handler(request):  # pylint: disable=W0613, unused-argument
        return response

    returned_response = await session_ws_middleware(req, handler)
    assert response is returned_response
    registry.ensure_id.assert_called_once_with(req)


@pytest.mark.asyncio
async def test_setup(registry, async_mock_call):
    event = asyncio.Event()
    response = Mock(spec=web.WebSocketResponse)
    response.close = event.wait
    registry.close_all = Mock(side_effect=async_mock_call)

    registry._registry[0] = set([response])
    app = web.Application()
    setup_session_ws(app, registry)
    app.freeze()

    assert app[REGISTRY_KEY] == registry

    fut = asyncio.ensure_future(app.shutdown())
    event.set()
    await asyncio.sleep(.01)
    assert fut.done()
    assert not fut.exception()
    registry.close_all.assert_called_once_with()


COOKIE_NAME = "AIOHTTP_SESSION_WEBSOCKET"


def get_session_data(resp):
    return json.loads(resp.cookies[COOKIE_NAME].value).get("session", {})


def make_cookie(data):
    return json.dumps({"session": data, "created": int(time.time())})


class TestWithSessionWS:
    @pytest.fixture
    def app(self):
        @with_session_ws
        async def handle_websocket(request, wsr):
            session_ws_id = await get_session_ws_id(request)
            async for msg in wsr:  # pylint: disable=W0612, unused-variable
                await wsr.send_str(str(session_ws_id))

        app = web.Application(
            middlewares=[
                aiohttp_session.session_middleware(
                    aiohttp_session.SimpleCookieStorage(cookie_name=COOKIE_NAME)
                )
            ]
        )
        app.router.add_get("/ws", handle_websocket)

        setup_session_ws(app, SessionWSRegistry())
        return app

    @pytest.mark.asyncio
    async def test_without_session(self, app, client):
        wsr = await client.ws_connect("/ws")
        wsr_resp = wsr._response
        wsr_session_data = get_session_data(wsr_resp)
        session_ws_id = wsr_session_data[DEFAULT_SESSION_KEY]
        assert len(app[REGISTRY_KEY][session_ws_id]) == 1

        await wsr.close()
        assert session_ws_id not in app[REGISTRY_KEY]

    @pytest.mark.asyncio
    async def test_with_session(self, app, client, cookie_jar):
        data = {DEFAULT_SESSION_KEY: 0}
        cookie_jar.update_cookies({COOKIE_NAME: make_cookie(data)})
        wsr = await client.ws_connect("/ws")
        assert COOKIE_NAME not in wsr._response.cookies
        assert data[DEFAULT_SESSION_KEY] in app[REGISTRY_KEY]

        await wsr.close()
        assert data[DEFAULT_SESSION_KEY] not in app[REGISTRY_KEY]

    @pytest.mark.asyncio
    async def test_inner(self, app, client, cookie_jar):
        data = {DEFAULT_SESSION_KEY: 0}
        cookie_jar.update_cookies({COOKIE_NAME: make_cookie(data)})
        wsr = await client.ws_connect("/ws")
        await wsr.send_str("...")

        resp = await wsr.receive()
        assert isinstance(resp, WSMessage)
        assert resp.data == "0"
        await wsr.close()
        assert data[DEFAULT_SESSION_KEY] not in app[REGISTRY_KEY]


class TestSessionWSRegistry:
    @staticmethod
    def make_request_session_tuple(session_ws_id=None):
        request = make_mocked_request("GET", "/")
        session = aiohttp_session.Session(
            "identity",
            data={
                "created": int(time.time()),
                "session": {DEFAULT_SESSION_KEY: session_ws_id},
            }
            if session_ws_id
            else {"created": int(time.time())},
            new=True,
        )
        request[aiohttp_session.SESSION_KEY] = session
        return request, session

    def test__getitem__(self, registry, wsr):
        registry._registry[0] = set([wsr])
        assert registry[0] == set([wsr])

    def test__iter__(self, registry, wsr):
        registry._registry[0] = set([wsr])
        assert list(registry) == [0]

    def test__len__(self, registry, wsr):
        registry._registry[0] = set([wsr])
        assert len(registry) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("session_ws_id"),
        [pytest.param(None, id="missing"), pytest.param("dummy", id="exists")],
    )
    async def test_get_id(self, registry, session_ws_id):
        # pylint: disable=W0612, unused-variable
        request, session = self.make_request_session_tuple(session_ws_id)
        assert await registry.get_id(request) == session_ws_id

    @pytest.mark.asyncio
    async def test_new_id_with_default_id_factory(self, registry):
        request, session = self.make_request_session_tuple()
        await registry.new_id(request)

        session_id = session[DEFAULT_SESSION_KEY]
        assert uuid.UUID(session_id)

    @pytest.mark.asyncio
    async def test_async_id_factory(self):
        called_with = None

        async def dummy_id_factory(request):
            nonlocal called_with
            called_with = request
            return id(request)

        registry = SessionWSRegistry(id_factory=dummy_id_factory)
        request, session = self.make_request_session_tuple()
        await registry.new_id(request)

        assert called_with == request
        assert session[DEFAULT_SESSION_KEY] == id(request)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("session_ws_id"),
        [pytest.param(None, id="missing"), pytest.param("dummy", id="exists")],
    )
    async def test_delete_id(self, session_ws_id, registry):
        request, session = self.make_request_session_tuple(session_ws_id)
        await registry.delete_id(request)
        assert DEFAULT_SESSION_KEY not in session

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("initial", "factory", "expected"),
        [
            pytest.param(None, lambda req: "found", "found", id="missing"),
            pytest.param("dummy", lambda req: ..., "dummy", id="exists"),
        ],
    )
    async def test_ensure_id(self, initial, factory, expected):
        registry = SessionWSRegistry(id_factory=factory)
        request, session = self.make_request_session_tuple(initial)
        await registry.ensure_id(request)
        assert session[DEFAULT_SESSION_KEY] == expected

    @pytest.mark.asyncio
    async def test_schedule_close_all_session(self, registry):
        event = asyncio.Event()
        registry.close_all_session = Mock(
            side_effect=registry.close_all_session
        )
        response = Mock(spec=web.Response)

        # pylint: disable=W0612, unused-variable
        request, session = self.make_request_session_tuple("dummy")
        request._task = asyncio.ensure_future(event.wait())

        await registry.schedule_close_all_session(request, response)
        registry.close_all_session.assert_not_called()
        response.force_close.assert_called_once_with()

        event.set()
        await asyncio.sleep(.01)
        registry.close_all_session.assert_called_once_with("dummy")

    @pytest.mark.asyncio
    async def test_close_all(self, registry, wsr):
        event = asyncio.Event()
        wsr.close = event.wait
        registry._registry[0] = set([wsr])

        fut = asyncio.ensure_future(registry.close_all())
        event.set()
        await asyncio.sleep(.01)
        assert fut.done()
        assert not fut.exception()

    @pytest.mark.asyncio
    async def test_close_all_session(self, registry, wsr):
        event = asyncio.Event()
        wsr.close = event.wait

        registry._registry[0] = set([wsr])

        fut = asyncio.ensure_future(registry.close_all_session(0))
        event.set()
        await asyncio.sleep(.01)
        assert fut.done()
        assert not fut.exception()

    def test_register(self, registry, wsr):
        registry.register(0, wsr)
        assert dict(registry) == {0: set([wsr])}

    def test_unregister_missing(self, registry, wsr):
        """
        Doesn't raise.
        """
        registry.unregister(0, wsr)

    def test_unregister_last(self, registry, wsr):
        """
        Removes key-value pair from registry
        """
        registry._registry[0] = set([wsr])
        registry.unregister(0, wsr)
        assert 0 not in registry

    def test_unregister_not_last(self, wsr):
        """
        KVP remains
        """
        registry = SessionWSRegistry()
        wsr2 = make_mock_wsr()
        registry._registry[0] = set([wsr, wsr2])
        registry.unregister(0, wsr)
        assert registry.get(0) == set([wsr2])


class TestIntegration:
    @pytest.fixture
    def app(self):
        async def handle_root(request):
            # pylint: disable=W0613, unused-argument
            return web.Response(text="root")

        async def handle_clear(request):
            response = web.Response(text="clear")
            await schedule_close_all_session_ws(request, response)
            return response

        @with_session_ws
        async def handle_websocket(request, wsr):
            # pylint: disable=W0613, unused-argument
            async for msg in wsr:
                await wsr.send_str(msg.data)

        app = web.Application(
            middlewares=[
                aiohttp_session.session_middleware(
                    aiohttp_session.SimpleCookieStorage(cookie_name=COOKIE_NAME)
                ),
                session_ws_middleware,
            ]
        )
        app.router.add_get("/", handle_root)
        app.router.add_get("/clear", handle_clear)
        app.router.add_get("/ws", handle_websocket)

        setup_session_ws(app, SessionWSRegistry())
        return app

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("initial",),
        [pytest.param(0, id="exists"), pytest.param(None, id="dne")],
    )
    async def test_receive_session_ws(self, client, initial, cookie_jar):
        data = {DEFAULT_SESSION_KEY: initial} if initial else {}
        cookie_jar.update_cookies({COOKIE_NAME: make_cookie(data)})
        resp = await client.get("/")

        session_data = get_session_data(resp)
        assert DEFAULT_SESSION_KEY in session_data

    @pytest.mark.asyncio
    async def test_close_session_ws(self, client):
        wsr = await client.ws_connect("/ws")
        wsr_resp = wsr._response
        wsr_session_data = get_session_data(wsr_resp)
        assert wsr_session_data.get(DEFAULT_SESSION_KEY)

        clear_resp = await client.get("/clear")
        assert clear_resp.status == 200

        wsr_msg = await wsr.receive()
        assert wsr_msg.type is WSMsgType.CLOSE
        assert wsr.closed
