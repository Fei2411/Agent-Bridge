"""Extract only identified native search/media tool completions from ACP."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

from agent_bridge.models import Task
from agent_bridge.native_results import (
    MAX_SOURCES,
    MEDIA_TOOLS,
    SEARCH_TOOLS,
    add_source,
    bounded_output,
    checkpoint,
    finish_job,
    observe_call,
    safe_url,
    start_job,
)
from agent_bridge.output_files import MAX_OUTPUT_FILES, checked_path, deliver_file, existing_files

_MEDIA_VARIANTS = {
    "image_gen": "ImageGen", "image_edit": "ImageEdit",
    "image_to_video": "ImageToVideo", "reference_to_video": "ReferenceToVideo",
}


def grok_media_session(cwd: Path, native_id: str, home: Path) -> Path:
    if not native_id or len(native_id) > 128 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in native_id):
        raise ValueError("invalid Grok native session identifier")
    sessions = home / "sessions"
    encoded = quote(str(cwd), safe="")
    if len(encoded.encode("utf-8")) <= 255:
        return checked_path(sessions / encoded / native_id, sessions, regular=False)
    # Long cwd groups use a slug/hash plus .cwd. Bind using the small official
    # marker rather than guessing the hash or trusting a tool-supplied folder.
    checked_path(sessions, sessions, regular=False)
    for group in list(sessions.iterdir())[:1024]:
        try:
            checked_path(group, sessions, regular=False)
            marker = checked_path(group / ".cwd", sessions)
            if marker.stat().st_size <= 16384 and Path(marker.read_text(encoding="utf-8").strip()).resolve() == cwd.resolve():
                return checked_path(group / native_id, sessions, regular=False)
        except (OSError, ValueError):
            continue
    raise ValueError("Grok long-cwd native session could not be bound")


class AcpNativeResults:
    def __init__(self, task: Task, home: Path, native_id: str | None, env: dict[str, str]) -> None:
        self.task, self.home = task, home
        self.native_id = native_id
        self.names: dict[str, str] = {}
        self.urls: dict[str, str] = {}
        self.completed: set[str] = set()
        self.media_calls: dict[str, str] = {}
        self.folder: Path | None = None
        self.previous: set[Path] = set()
        self.setup_error = False
        if task.agent == "grok" and native_id:
            try:
                root = Path(env.get("GROK_HOME") or Path.home() / ".grok")
                self.folder = grok_media_session(Path(task.cwd), native_id, root)
                for name in ("images", "videos"):
                    self.previous.update(existing_files(self.folder / name))
            except (OSError, ValueError):
                self.setup_error = True

    def tool_name(self, update: dict[str, Any]) -> str | None:
        call_id = update.get("toolCallId")
        if not isinstance(call_id, str) or not call_id or len(call_id) > 128:
            return None
        field = "toolName" if self.task.agent == "grok" else "name"
        name = update.get(field)
        raw_input = update.get("rawInput")
        if not isinstance(name, str) and self.task.agent == "grok" and isinstance(raw_input, dict):
            variant_names = {
                "WebSearch": "web_search", "WebFetch": "web_fetch",
                "ImageGen": "image_gen", "ImageEdit": "image_edit",
                "ImageToVideo": "image_to_video", "ReferenceToVideo": "reference_to_video",
            }
            variant = raw_input.get("variant")
            name = variant_names.get(variant) if isinstance(variant, str) else None
        if not isinstance(name, str) and self.task.agent == "grok":
            vendor_meta = update.get("_meta")
            if isinstance(vendor_meta, dict):
                xai_tool = vendor_meta.get("x.ai/tool")
                if isinstance(xai_tool, dict):
                    name = xai_tool.get("name")
        if not isinstance(name, str):
            return self.names.get(call_id)
        if isinstance(name, str) and name in SEARCH_TOOLS | MEDIA_TOOLS and len(self.names) < 256:
            if call_id in self.names and self.names[call_id] != name:
                return None
            self.names[call_id] = name
        return self.names.get(call_id)

    def consume(self, update: dict[str, Any]) -> None:
        if self.task.agent not in {"grok", "minimax"}:
            return
        name = self.tool_name(update)
        if name is None:
            return
        call_id = update["toolCallId"]
        observe_call(self.task, self.home, call_id, name)
        status = update.get("status")
        raw_input = update.get("rawInput")
        if isinstance(raw_input, dict) and name == "web_fetch" and (url := safe_url(raw_input.get("url"))):
            self.urls[call_id] = url
        if name in MEDIA_TOOLS:
            if self.task.agent != "grok":
                return
            if call_id not in self.media_calls:
                self.media_calls[call_id] = "running"
                start_job(self.task, self.home, provider="grok", native_id=self.native_id)
            if status == "failed":
                self.media_calls[call_id] = "failed"
                finish_job(self.task, self.home, status="failed", reason="Grok native media tool failed.")
                return
        if status != "completed" or call_id in self.completed:
            return
        output = bounded_output(update.get("rawOutput"))
        if output is None:
            return
        self.completed.add(call_id)
        if name in SEARCH_TOOLS:
            self._search(name, call_id, output)
            checkpoint(self.home, self.task)
            return
        try:
            self._media(name, call_id, output)
            self.media_calls[call_id] = "completed"
            self._aggregate()
        except (OSError, ValueError):
            self.media_calls[call_id] = "failed"
            self.task.warnings.append("Native media output could not be safely delivered.")
            if not self.task.checkpoint_error:
                finish_job(self.task, self.home, status="failed", reason="Native file validation or copy failed; do not resubmit generation.")

    def _aggregate(self, *, ended: bool = False) -> None:
        states = set(self.media_calls.values())
        if "running" in states and ended:
            status = "uncertain"
        elif "failed" in states:
            status = "failed"
        elif "running" in states:
            status = "running"
        else:
            status = "completed"
        finish_job(
            self.task, self.home, status=status,
            reason="One or more native media calls failed or remain unconfirmed; do not resubmit." if status in {"failed", "uncertain"} else None,
        )

    def _search(self, name: str, call_id: str, output: dict[str, Any]) -> None:
        if self.task.agent == "grok":
            variant = output.get("type")
            if name == "web_search" and variant == "WebSearch":
                citations = output.get("citations")
                if isinstance(citations, list):
                    for citation in citations[:MAX_SOURCES]:
                        add_source(self.task.sources, {"url": citation} if isinstance(citation, str) else citation, call_id)
            if name == "web_fetch" and variant == "WebFetch":
                # Historical native fixtures use a Content variant. Unknown
                # current variants do not silently become successful fetches.
                content = output.get("Content")
                if isinstance(content, dict) and isinstance(content.get("status_code"), int) and 200 <= content["status_code"] < 400:
                    url = content.get("url") or self.urls.get(call_id)
                    text = content.get("content")
                    snippet = text[:2000] if isinstance(text, str) and "base64" not in text[:2000] else None
                    add_source(self.task.sources, {"url": url, "snippet": snippet}, call_id)
        elif name == "web_search":
            # Accept the official search service body only inside an identified
            # successful ACP tool. Do not infer citations from arbitrary text.
            body = output
            content = output.get("content")
            if isinstance(content, list) and content and isinstance(content[0], dict):
                decoded = bounded_output(content[0].get("text"))
                if decoded is not None:
                    body = decoded
            if "code" in body and body["code"] != 0:
                return
            results = body.get("results")
            if isinstance(results, list):
                for result in results[:MAX_SOURCES]:
                    if isinstance(result, dict):
                        add_source(self.task.sources, {
                            "url": result.get("url") or result.get("href") or result.get("link"),
                            "title": result.get("title"), "snippet": result.get("snippet"),
                        }, call_id)

    def _media(self, name: str, call_id: str, output: dict[str, Any]) -> None:
        if self.setup_error or self.folder is None or output.get("type") != _MEDIA_VARIANTS.get(name):
            raise ValueError("Grok media lacks native session or output evidence")
        media_dir = "videos" if name in {"image_to_video", "reference_to_video"} else "images"
        raw_path, filename, session_folder = (output.get(field) for field in ("path", "filename", "session_folder"))
        if not all(isinstance(value, str) and 0 < len(value) <= 4096 for value in (raw_path, filename, session_folder)):
            raise ValueError("Grok media fields are missing")
        source = Path(str(raw_path))
        if session_folder != media_dir or source.name != filename or source.parent != self.folder / media_dir:
            raise ValueError("Grok media path does not match this session")
        if len(self.task.output_files) >= MAX_OUTPUT_FILES:
            raise ValueError("Grok media output limit exceeded")
        self.task.output_files.append(deliver_file(
            source, allowed_root=self.folder / media_dir, cwd=Path(self.task.cwd),
            task_id=self.task.task_id, tool_call_id=call_id, previous=self.previous,
            expected="video" if media_dir == "videos" else "image",
        ))

    def end(self) -> None:
        if self.task.job and not self.task.checkpoint_error:
            self._aggregate(ended=True)
