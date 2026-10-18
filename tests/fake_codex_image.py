"""Offline app-server fixture; never contacts a provider."""

from __future__ import annotations

import json
import os
import struct
import sys
import zlib
from pathlib import Path


def send(value: dict) -> None:
    print(json.dumps(value), flush=True)


def png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\xff"))
        + chunk(b"IEND", b"")
    )


def image_name(root: Path) -> str:
    if os.environ.get("BRIDGE_IMAGE_UNIQUE") != "1":
        return "call_fixture"
    counter = root.parent / "image_seq"
    current = int(counter.read_text(encoding="utf-8")) + 1 if counter.exists() else 1
    counter.parent.mkdir(parents=True, exist_ok=True)
    counter.write_text(str(current), encoding="utf-8")
    return f"call_fixture_{current}"


def main() -> None:
    root = Path(os.environ["CODEX_HOME"]) / "generated_images" / "thread_fixture"
    case = os.environ.get("BRIDGE_IMAGE_FIXTURE", "success")
    name = image_name(root)
    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        rid = message.get("id")
        if method == "initialize":
            if case == "init_rejected":
                send({"id": rid, "error": {"message": "fixture auth required"}})
            else:
                send({"id": rid, "result": {"codexHome": os.environ["CODEX_HOME"]}})
        elif method == "thread/start":
            if case == "old":
                root.mkdir(parents=True, exist_ok=True)
                (root / "call_fixture.png").write_bytes(png())
            send({"id": rid, "result": {"thread": {"id": "thread_fixture"}}})
        elif method == "turn/start":
            with Path("submissions.log").open("a", encoding="utf-8") as handle:
                handle.write("submit\n")
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            with Path("inputs.log").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(params.get("input"), ensure_ascii=False) + "\n")
            if case == "turn_rejected":
                send({"id": rid, "error": {"message": "fixture rejected"}})
                continue
            if case == "disconnect":
                return
            turn = {"id": rid, "result": {"turn": {"id": "turn_fixture"}}}
            event = {
                "method": "item/completed",
                "params": {
                    "completedAtMs": 0,
                    "threadId": "wrong" if case == "wrong_thread" else "thread_fixture",
                    "turnId": "wrong" if case == "wrong_turn" else "turn_fixture",
                    "item": {
                        "type": "imageGeneration", "id": name,
                        "status": "failed" if case == "native_failed" else "completed",
                        "savedPath": str(root / f"{name}.png"),
                        "result": "fixture-base64-must-not-persist",
                    },
                },
            }
            if case not in {"missing", "hang", "no_image"}:
                root.mkdir(parents=True, exist_ok=True)
                (root / f"{name}.png").write_bytes(png())
            if case == "escape":
                event["params"]["item"]["savedPath"] = str(Path.cwd() / "outside.png")
            if case == "before_response":
                send(event)
                send(turn)
            else:
                send(turn)
                if case not in {"hang", "no_image"}:
                    send(event)
            if case == "duplicate":
                send(event)
            if case == "completed_disconnect":
                return
            if case != "hang":
                send({
                    "method": "thread/tokenUsage/updated",
                    "params": {"threadId": "thread_fixture", "turnId": "turn_fixture", "tokenUsage": {"total": {"inputTokens": 3, "outputTokens": 2}}},
                })
                send({
                    "method": "turn/completed",
                    "params": {"threadId": "thread_fixture", "turn": {"id": "turn_fixture", "status": "completed"}},
                })
        elif method == "turn/interrupt":
            send({"id": rid, "result": {}})
            send({
                "method": "turn/completed",
                "params": {"threadId": "thread_fixture", "turn": {"id": "turn_fixture", "status": "interrupted"}},
            })


if __name__ == "__main__":
    main()
