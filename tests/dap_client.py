"""Minimal Debug Adapter Protocol client, driving `python -m debugpy.adapter` the way Zed does."""

from __future__ import annotations

import itertools
import json
import queue
import subprocess
import threading
import time
from typing import Any


class DapClient:
    def __init__(self, python: str, env: dict[str, str] | None = None):
        self.process = subprocess.Popen(
            [python, "-m", "debugpy.adapter"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        assert self.process.stdin is not None and self.process.stdout is not None
        self.stdin, self.stdout = self.process.stdin, self.process.stdout
        self.thread_id: int | None = None  # set by debug_until_breakpoint
        self.seq = itertools.count(1)
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.backlog: list[dict[str, Any]] = []
        self.output: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        stream = self.stdout
        while True:
            length = None
            while True:
                line = stream.readline()
                if not line:
                    return
                line = line.strip()
                if not line:
                    break
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":")[1])
            if length is None:
                raise RuntimeError(f"DAP message without Content-Length: {line!r}")
            message = json.loads(stream.read(length))
            if message.get("event") == "output":
                self.output.append(message["body"].get("output", ""))
            self.messages.put(message)

    def send(self, command: str, arguments: dict[str, Any] | None = None) -> int:
        seq = next(self.seq)
        body = json.dumps({"seq": seq, "type": "request", "command": command, "arguments": arguments or {}}).encode()
        self.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
        self.stdin.flush()
        return seq

    def wait(self, predicate, timeout: float = 60) -> dict[str, Any]:
        for i, message in enumerate(self.backlog):
            if predicate(message):
                return self.backlog.pop(i)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no matching DAP message; output so far:\n{''.join(self.output)}")
            message = self.messages.get(timeout=remaining)
            if predicate(message):
                return message
            self.backlog.append(message)

    def request(self, command: str, arguments: dict[str, Any] | None = None, timeout: float = 60) -> dict[str, Any]:
        seq = self.send(command, arguments)
        response = self.wait(lambda m: m.get("type") == "response" and m.get("request_seq") == seq, timeout)
        if not response.get("success"):
            raise RuntimeError(f"{command} failed: {response.get('message')} {response.get('body')}")
        return response

    def event(self, name: str, timeout: float = 60) -> dict[str, Any]:
        return self.wait(lambda m: m.get("type") == "event" and m.get("event") == name, timeout)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()


def debug_until_breakpoint(
    client: DapClient, launch: dict[str, Any], file: str, line: int, timeout: float = 120
) -> int:
    """Launch, stop at `file:line`, and return the top frame id."""
    client.request(
        "initialize",
        {
            "adapterID": "debugpy",
            "clientID": "zed",
            "pathFormat": "path",
            "linesStartAt1": True,
            "columnsStartAt1": True,
        },
    )
    launch_seq = client.send("launch", launch)
    client.event("initialized", timeout)
    breakpoints = client.request("setBreakpoints", {"source": {"path": file}, "breakpoints": [{"line": line}]})
    assert breakpoints["body"]["breakpoints"][0]["verified"], breakpoints
    client.request("configurationDone")
    client.wait(lambda m: m.get("type") == "response" and m.get("request_seq") == launch_seq, timeout)
    stopped = client.event("stopped", timeout)
    frames = client.request("stackTrace", {"threadId": stopped["body"]["threadId"]})["body"]["stackFrames"]
    assert frames[0]["source"]["path"] == file and frames[0]["line"] == line, frames[0]
    client.thread_id = stopped["body"]["threadId"]
    return frames[0]["id"]


def evaluate(client: DapClient, frame_id: int, expression: str, timeout: float = 120) -> str:
    return client.request("evaluate", {"expression": expression, "frameId": frame_id, "context": "repl"}, timeout)[
        "body"
    ]["result"]
