"""TwitchChatClient against a local fake chat server."""

import asyncio

from websockets.asyncio.server import serve

from clipper_twitch.chat import JoinRateLimiter, TwitchChatClient

NICK = "justinfan1"


class FakeChatServer:
    def __init__(self, reconnect_once: bool = False) -> None:
        self.lines: list[str] = []
        self.connections = 0
        self.reconnect_once = reconnect_once

    async def handler(self, ws) -> None:
        self.connections += 1
        async for frame in ws:
            for line in frame.split("\r\n"):
                self.lines.append(line)
                if line.startswith("NICK"):
                    await ws.send("PING :tmi.twitch.tv")
                elif line.startswith("JOIN #"):
                    chan = line.split("#", 1)[1]
                    await ws.send(
                        f":{NICK}!{NICK}@{NICK}.tmi.twitch.tv JOIN #{chan}\r\n"
                        f"@id=m1;tmi-sent-ts=1700000000000;user-id=7 "
                        f":bob!bob@bob.tmi.twitch.tv PRIVMSG #{chan} :hi from {chan}"
                    )
                    if self.reconnect_once and self.connections == 1:
                        await ws.send(":tmi.twitch.tv RECONNECT")


async def wait_for(condition, limit_s: float = 5.0) -> None:
    async def poll():
        while not condition():  # noqa: ASYNC110 - polling a plain predicate in a test
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), limit_s)


async def test_handshake_join_pong_dispatch_and_part():
    server = FakeChatServer()
    received = []
    async with serve(server.handler, "127.0.0.1", 0) as ws_server:
        port = ws_server.sockets[0].getsockname()[1]
        client = TwitchChatClient(
            lambda m, t: received.append((m, t)), url=f"ws://127.0.0.1:{port}", nick=NICK
        )
        client.set_channels(["Alpha"])
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        await wait_for(lambda: any(m.command == "PRIVMSG" for m, _ in received))

        assert server.lines[:3] == [
            "CAP REQ :twitch.tv/tags twitch.tv/commands",
            "PASS SCHMOOPIIE",
            f"NICK {NICK}",
        ]
        assert "PONG :tmi.twitch.tv" in server.lines
        assert "JOIN #alpha" in server.lines
        assert client.joined == {"alpha"} and client.connected
        msg, received_at = next((m, t) for m, t in received if m.command == "PRIVMSG")
        assert msg.channel == "alpha" and msg.text == "hi from alpha"
        assert received_at.tzinfo is not None

        client.set_channels([])
        await wait_for(lambda: "PART #alpha" in server.lines)
        stop.set()
        await asyncio.wait_for(task, 5)


async def test_reconnects_and_rejoins_after_reconnect_command():
    server = FakeChatServer(reconnect_once=True)
    async with serve(server.handler, "127.0.0.1", 0) as ws_server:
        port = ws_server.sockets[0].getsockname()[1]
        client = TwitchChatClient(lambda m, t: None, url=f"ws://127.0.0.1:{port}", nick=NICK)
        client.set_channels(["alpha"])
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        await wait_for(lambda: server.lines.count("JOIN #alpha") >= 2)
        assert server.connections == 2
        stop.set()
        await asyncio.wait_for(task, 5)


async def test_join_rate_limiter_waits_when_full():
    limiter = JoinRateLimiter(limit=2, period_s=0.3)
    loop = asyncio.get_running_loop()
    start = loop.time()
    for _ in range(3):
        await limiter.acquire()
    assert loop.time() - start >= 0.25
