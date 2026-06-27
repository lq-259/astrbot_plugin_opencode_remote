import json
from typing import Optional
import httpx
from astrbot.api import logger


class OpenCodeClient:
    def __init__(
        self,
        server_url: str,
        username: str = "opencode",
        password: str = "",
        timeout: int = 300,
    ):
        self.server_url = server_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    def _get_auth(self) -> Optional[tuple[str, str]]:
        if self.password:
            return (self.username, self.password)
        return None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.server_url,
                auth=self._get_auth(),
                timeout=httpx.Timeout(self.timeout),
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def request(
        self,
        method: str,
        path: str,
        *,
        directory: str = None,
        params: dict = None,
        json_data: dict = None,
        **kwargs,
    ) -> httpx.Response:
        client = await self._get_client()
        query = dict(params or {})
        if directory:
            query["directory"] = directory
        return await client.request(
            method, path, params=query, json=json_data, **kwargs
        )

    async def _get(self, path: str, *, directory: str = None,
                   params: dict = None, **kwargs) -> dict:
        resp = await self.request("GET", path, directory=directory, params=params, **kwargs)
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, *, directory: str = None,
                    params: dict = None, json_data: dict = None) -> dict:
        resp = await self.request(
            "POST", path, directory=directory, params=params, json_data=json_data
        )
        resp.raise_for_status()
        if not resp.content:
            return {}
        return resp.json()

    async def _delete(self, path: str, *, directory: str = None) -> dict:
        resp = await self.request("DELETE", path, directory=directory)
        resp.raise_for_status()
        return resp.json()

    async def _patch(self, path: str, *, directory: str = None,
                     json_data: dict = None) -> dict:
        resp = await self.request(
            "PATCH", path, directory=directory, json_data=json_data
        )
        resp.raise_for_status()
        return resp.json()

    # === global ===

    async def health(self) -> dict:
        return await self._get("/global/health")

    # === project / path / vcs ===

    async def project_current(self, directory: str) -> dict:
        return await self._get("/project/current", directory=directory)

    async def path_info(self, directory: str) -> dict:
        return await self._get("/path", directory=directory)

    async def vcs_info(self, directory: str) -> dict:
        return await self._get("/vcs", directory=directory)

    # === config ===

    async def config_get(self, directory: str = None) -> dict:
        return await self._get("/config", directory=directory)

    async def config_update(self, config_body: dict, directory: str = None) -> dict:
        return await self._patch("/config", directory=directory, json_data=config_body)

    # === provider ===

    async def providers(self) -> dict:
        return await self._get("/provider")

    async def config_providers(self, directory: str) -> dict:
        return await self._get("/config/providers", directory=directory)

    # === agent ===

    async def agents(self, directory: str = None) -> list:
        return await self._get("/agent", directory=directory)

    # === command ===

    async def commands(self, directory: str = None) -> list:
        return await self._get("/command", directory=directory)

    # === session ===

    async def session_list(self, directory: str = None) -> list:
        return await self._get("/session", directory=directory)

    async def session_status(self, directory: str) -> dict:
        return await self._get("/session/status", directory=directory)

    async def session_create(self, title: str = None,
                             parent_id: str = None,
                             directory: str = None) -> dict:
        body = {}
        if title:
            body["title"] = title
        if parent_id:
            body["parentID"] = parent_id
        return await self._post("/session", directory=directory, json_data=body)

    async def session_get(self, session_id: str, directory: str) -> dict:
        return await self._get(f"/session/{session_id}", directory=directory)

    async def session_update(self, session_id: str, title: str,
                             directory: str) -> dict:
        return await self._patch(
            f"/session/{session_id}", directory=directory, json_data={"title": title}
        )

    async def session_delete(self, session_id: str, directory: str = None) -> dict:
        return await self._delete(f"/session/{session_id}", directory=directory)

    async def session_children(self, session_id: str, directory: str) -> list:
        return await self._get(f"/session/{session_id}/children", directory=directory)

    async def session_fork(self, session_id: str, message_id: str = None,
                           directory: str = None) -> dict:
        body = {}
        if message_id:
            body["messageID"] = message_id
        return await self._post(f"/session/{session_id}/fork",
                                directory=directory, json_data=body)

    async def session_abort(self, session_id: str, directory: str) -> dict:
        return await self._post(f"/session/{session_id}/abort", directory=directory)

    async def session_share(self, session_id: str, directory: str) -> dict:
        return await self._post(f"/session/{session_id}/share", directory=directory)

    async def session_unshare(self, session_id: str, directory: str) -> dict:
        return await self._delete(f"/session/{session_id}/share", directory=directory)

    async def session_summarize(self, session_id: str, provider_id: str,
                                model_id: str, directory: str) -> dict:
        body = {"providerID": provider_id, "modelID": model_id}
        return await self._post(
            f"/session/{session_id}/summarize",
            directory=directory, json_data=body
        )

    async def session_diff(self, session_id: str, message_id: str = None,
                           directory: str = None) -> list:
        params = {}
        if message_id:
            params["messageID"] = message_id
        return await self._get(
            f"/session/{session_id}/diff", directory=directory, params=params
        )

    async def session_revert(self, session_id: str, message_id: str,
                             part_id: str = None, directory: str = None) -> dict:
        body = {"messageID": message_id}
        if part_id:
            body["partID"] = part_id
        return await self._post(
            f"/session/{session_id}/revert", directory=directory, json_data=body
        )

    async def session_unrevert(self, session_id: str, directory: str) -> dict:
        return await self._post(
            f"/session/{session_id}/unrevert", directory=directory
        )

    async def session_git_diff(self, session_id: str, directory: str) -> str:
        """Get git diff for the current session's changes."""
        resp = await self.request("GET", f"/session/{session_id}/git-diff", directory=directory)
        resp.raise_for_status()
        return resp.text()

    # === message ===

    async def session_messages(self, session_id: str, directory: str,
                               limit: int = 50) -> list:
        params = {"limit": limit}
        return await self._get(
            f"/session/{session_id}/message",
            directory=directory, params=params
        )

    async def session_message_get(self, session_id: str, message_id: str,
                                  directory: str) -> dict:
        return await self._get(
            f"/session/{session_id}/message/{message_id}", directory=directory
        )

    async def session_prompt(self, session_id: str, text: str,
                             directory: str,
                             model: dict = None,
                             agent: str = None,
                             variant: str = None,
                             no_reply: bool = False) -> dict:
        body = {"parts": [{"type": "text", "text": text}]}
        if model:
            body["model"] = model
        if agent:
            body["agent"] = agent
        if variant:
            body["variant"] = variant
        if no_reply:
            body["noReply"] = True
        return await self._post(
            f"/session/{session_id}/message",
            directory=directory, json_data=body
        )

    async def session_prompt_async(self, session_id: str, text: str,
                                   directory: str,
                                   model: dict = None,
                                   agent: str = None,
                                   variant: str = None) -> None:
        body = {"parts": [{"type": "text", "text": text}]}
        if model:
            body["model"] = model
        if agent:
            body["agent"] = agent
        if variant:
            body["variant"] = variant
        resp = await self.request(
            "POST",
            f"/session/{session_id}/prompt_async",
            directory=directory,
            json_data=body,
        )
        resp.raise_for_status()

    async def session_command(self, session_id: str, command: str,
                              arguments: str = "",
                              directory: str = None,
                              agent: str = None,
                              model: str = None) -> dict:
        body = {"command": command, "arguments": arguments}
        if agent:
            body["agent"] = agent
        if model:
            body["model"] = model
        return await self._post(
            f"/session/{session_id}/command",
            directory=directory, json_data=body
        )

    async def session_shell(self, session_id: str, command: str,
                            directory: str, agent: str = "build",
                            model: dict = None) -> dict:
        body = {"agent": agent, "command": command}
        if model:
            body["model"] = model
        return await self._post(
            f"/session/{session_id}/shell",
            directory=directory, json_data=body
        )

    # === permission ===

    async def permission_respond(self, session_id: str, permission_id: str,
                                 response: str, remember: bool = False,
                                 directory: str = None) -> dict:
        # Map user-friendly response to API values: "once", "always", "reject"
        if response in ("allow", "yes", "approve"):
            reply_val = "always" if remember else "once"
        elif response in ("deny", "no", "reject"):
            reply_val = "reject"
        else:
            reply_val = response
        logger.info("permission_respond: sid=%s pid=%s dir=%s reply=%s",
                    session_id[:12], permission_id, directory, reply_val)
        # Try newer endpoint first: POST /permission/{requestID}/reply
        try:
            result = await self._post(
                f"/permission/{permission_id}/reply",
                directory=directory, json_data={"reply": reply_val}
            )
            return result
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.info("permission reply 404, trying legacy endpoint")
            else:
                logger.warning("permission reply failed: %s", e)
                raise
        # Fallback: legacy endpoint POST /session/{sessionID}/permissions/{permissionID}
        try:
            result = await self._post(
                f"/session/{session_id}/permissions/{permission_id}",
                directory=directory, json_data={"response": reply_val}
            )
            return result
        except httpx.HTTPError as e:
            logger.warning("permission_respond legacy failed: %s", e)
            raise

    # === file ===

    async def file_list(self, path: str = None, directory: str = None) -> list:
        params = {}
        if path:
            params["path"] = path
        return await self._get("/file", directory=directory, params=params)

    async def file_content(self, file_path: str, directory: str) -> dict:
        params = {"path": file_path}
        return await self._get("/file/content", directory=directory, params=params)

    async def file_write(self, file_path: str, content: str, directory: str) -> dict:
        """Write text content to a file in the workspace."""
        body = {"path": file_path, "content": content}
        return await self._post("/file/content", directory=directory, json_data=body)

    async def file_status(self, directory: str) -> list:
        return await self._get("/file/status", directory=directory)

    async def find_file(self, query: str, directory: str,
                        file_type: str = None, limit: int = 50) -> list:
        params = {"query": query, "limit": limit}
        if file_type:
            params["type"] = file_type
        return await self._get("/find/file", directory=directory, params=params)

    async def find_text(self, pattern: str, directory: str) -> list:
        params = {"pattern": pattern}
        return await self._get("/find", directory=directory, params=params)

    # === tui ===

    async def tui_append_prompt(self, text: str) -> dict:
        return await self._post("/tui/append-prompt", json_data={"text": text})

    async def tui_submit_prompt(self) -> dict:
        return await self._post("/tui/submit-prompt")

    async def tui_clear_prompt(self) -> dict:
        return await self._post("/tui/clear-prompt")

    async def tui_execute_command(self, command: str) -> dict:
        return await self._post("/tui/execute-command", json_data={"command": command})

    async def tui_open_models(self) -> dict:
        return await self._post("/tui/open-models")

    async def tui_open_sessions(self) -> dict:
        return await self._post("/tui/open-sessions")

    async def tui_open_help(self) -> dict:
        return await self._post("/tui/open-help")

    async def tui_show_toast(self, message: str, variant: str = "info",
                             title: str = None) -> dict:
        body = {"message": message, "variant": variant}
        if title:
            body["title"] = title
        return await self._post("/tui/show-toast", json_data=body)

    # === SSE ===

    async def subscribe_events(self) -> httpx.Response:
        sse_client = httpx.AsyncClient(
            base_url=self.server_url,
            auth=self._get_auth(),
            timeout=httpx.Timeout(30, read=None, write=30, connect=30),
        )
        try:
            resp = await sse_client.send(
                sse_client.build_request("GET", "/event"),
                stream=True,
            )
            resp.extensions["sse_client"] = sse_client
            return resp
        except Exception:
            await sse_client.aclose()
            raise

    async def subscribe_global_events(self) -> httpx.Response:
        sse_client = httpx.AsyncClient(
            base_url=self.server_url,
            auth=self._get_auth(),
            timeout=httpx.Timeout(30, read=None, write=30, connect=30),
        )
        try:
            resp = await sse_client.send(
                sse_client.build_request("GET", "/global/event"),
                stream=True,
            )
            resp.extensions["sse_client"] = sse_client
            return resp
        except Exception:
            await sse_client.aclose()
            raise
