import asyncio
from datetime import datetime

from aiohttp import web
import aiohttp_session

from aiohttp_session_ws import (
    SessionWSRegistry,
    get_session_ws_id,
    new_session_ws_id,
    schedule_close_all_session_ws,
    session_ws,
    session_ws_middleware,
    setup as setup_session_websockets,
)


async def handle_root(request):
    # pylint: disable=W0613, unused-argument
    return web.Response(text=template, content_type="text/html")


async def handle_reset(request):
    session_ws_id = await get_session_ws_id(request)
    response = web.Response(
        text=f"Reset called on session {session_ws_id}!",
        content_type="text/plain",
    )
    await schedule_close_all_session_ws(request, response)
    await new_session_ws_id(request)
    return response


async def handle_websocket(request):
    # pylint: disable=W0613, unused-argument
    async with session_ws(request) as wsr:
        connected_at = datetime.now()
        session_ws_id = await get_session_ws_id(request)
        while True:
            await wsr.send_str(
                f"Websocket associated with session [{session_ws_id}] "
                f"connected for {(datetime.now() - connected_at).seconds}"
            )
            await asyncio.sleep(1)
        return wsr


def make_app():
    app = web.Application(
        middlewares=[
            aiohttp_session.session_middleware(
                aiohttp_session.SimpleCookieStorage()
            ),
            session_ws_middleware,
        ]
    )
    app.router.add_get("/", handle_root)
    app.router.add_get("/reset", handle_reset)
    app.router.add_get("/ws", handle_websocket)

    setup_session_websockets(app, SessionWSRegistry())
    return app


template = """
<html>
    <head>
        <script type="text/javascript">
        const host = window.location.host;

        function addLog(message) {
            const logs = document.getElementById('logs');
            const log = document.createElement('li');
            log.innerText = message;
            logs.appendChild(log);
        }

        function connect() {
            const socket = new WebSocket("ws://" + host + "/ws");
            socket.onmessage = function(event) {
                document.getElementById("message").innerText = event.data;
            };
            socket.onopen = function(event) { addLog('websocket opened'); }
            socket.onclose = function(event) {
                addLog('websocket closed');
                setTimeout(function() { connect(); }, 1000);
            }
        }
        connect();

        function resetSession() {
            fetch("http://" + host + "/reset", { credentials: "same-origin" })
                .then(function(response) {
                    return response.text().then(function(text) {
                        const logs = document.getElementById('logs');
                        const log = document.createElement('li');
                        log.innerText = text;
                        logs.appendChild(log);
                        console.log(text);
                    });
                });
        }
        </script>
    </head>
    <body>
        <h2>aiohttp_session_ws demo</h2>
        <div id="message">Waiting on connection...</div>
        <button onclick="resetSession()">reset session</button>
        <ul id="logs">
        </ul>
    </body>
</html>
"""

if __name__ == "__main__":
    web.run_app(make_app())
