"""Copilot Studio declarative agent (minimalBots) for tool-call compliance.

Native M365 ChatHub has NO OpenAI-style tool registration. The only proven way
to raise tool-call compliance is a server-side declarative agent whose
instructions bake the fence protocol (cramt agent.ts, June 2026).

This module:
1. Discovers Power Platform default environment (BAP)
2. Creates/publishes a versioned minimalBot with format-contract instructions
3. Returns agent id: T_{titleId}.{botId}.gpt.default for threadLevelGptId

Requires extra tokens:
- https://api.bap.microsoft.com/.default
- https://api.powerplatform.com/.default
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("mcg.agent.studio")

BAP_API = "https://api.bap.microsoft.com"
AGENT_BASE = "mcg-tool-agent"
# 48x48 blue PNG — required by publish
_BOT_ICON_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAIAAADYYG7QAAAAB3RJTUUH6AMbAAAoLbOJEAAAABl0"
    "RVh0Q29tbWVudABDcmVhdGVkIHdpdGggR0lNUFeBDhcAAAAoSURBVFjD7cExAQAAAMKg9U9tDB+g"
    "AAAAAAAAAAAAAAAAAAAAAAAA/BgwMAAB/0LuMgAAAABJRU5ErkJggg=="
)

AGENT_INSTRUCTIONS = """You are the execution core of an automated agent. Your output is parsed by a program.
When the incoming message lists tools, you are in execution mode. To act, output ONLY a single Markdown code fence whose info-string is the exact tool name — nothing before or after. A fenced block is an ACTION the runtime executes immediately:
```
tool_name
{"arg":"value"}
```
Emit exactly one fenced tool call per turn, then stop. When no tools are needed, answer in plain language without fences. Never invent tools that were not listed. Never narrate tool usage."""


def instructions_hash() -> str:
    return hashlib.sha256(AGENT_INSTRUCTIONS.encode()).hexdigest()[:8]


def agent_display_name() -> str:
    return f"{AGENT_BASE}-{instructions_hash()}"


class StudioAgentError(RuntimeError):
    pass


class StudioAgentManager:
    def __init__(
        self,
        *,
        cache_path: str | Path,
        bap_token: str | None,
        pp_token: str | None,
        timeout: float = 60.0,
    ) -> None:
        self.cache_path = Path(cache_path)
        self.bap_token = bap_token
        self.pp_token = pp_token
        self.timeout = timeout
        self._cached_id: str | None = None

    def load_cache(self) -> dict[str, Any] | None:
        if not self.cache_path.is_file():
            return None
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None

    def save_cache(self, data: dict[str, Any]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    async def get_agent_id(self, *, force: bool = False) -> str | None:
        """Return agent id or None if tokens/env unavailable."""
        want = instructions_hash()
        if not force and self._cached_id:
            return self._cached_id
        cached = self.load_cache()
        if not force and cached and cached.get("instructionsHash") == want and cached.get("agentId"):
            self._cached_id = str(cached["agentId"])
            return self._cached_id
        if not self.bap_token or not self.pp_token:
            log.info("studio agent skipped: missing BAP/PP token")
            return None
        try:
            env_url = await self._resolve_env_url()
            bot_id = await self._ensure_bot(env_url)
            title_id = await self._publish(env_url, bot_id)
            agent_id = f"{title_id}.{bot_id}.gpt.default"
            self.save_cache(
                {
                    "agentId": agent_id,
                    "botId": bot_id,
                    "instructionsHash": want,
                }
            )
            self._cached_id = agent_id
            log.info("studio agent ready: %s", agent_id)
            return agent_id
        except Exception as exc:  # noqa: BLE001
            log.warning("studio agent provision failed: %s", exc)
            return None

    async def _resolve_env_url(self) -> str:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{BAP_API}/providers/Microsoft.BusinessAppPlatform/environments/~default",
                params={"api-version": "2023-06-01"},
                headers={"Authorization": f"Bearer {self.bap_token}"},
            )
            if r.status_code >= 400:
                raise StudioAgentError(f"BAP env: {r.status_code} {r.text[:200]}")
            data = r.json()
            env_name = str(data.get("name") or "")
            env_id = env_name.replace("Default-", "").replace("-", "").lower()
            base = ".df.environment.api.powerplatform.com"
            candidates = [
                f"https://default{env_id}{base}",
                f"https://default{env_id[:-2]}{base}" if len(env_id) > 2 else "",
            ]
            for url in candidates:
                if not url:
                    continue
                try:
                    probe = await client.head(
                        f"{url}/copilotstudio/minimalBots/api",
                        params={"api-version": "2022-03-01-preview"},
                        headers={"Authorization": f"Bearer {self.pp_token}"},
                    )
                    # any HTTP response means DNS + host ok
                    if probe.status_code < 500:
                        return url
                except Exception:  # noqa: BLE001
                    continue
            return candidates[0]

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "x-ms-user-agent": "PVA-Portal/1.0.0 (Web; ReactNative: false)",
        }

    async def _list_bots(self, env_url: str) -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(
                f"{env_url}/copilotstudio/minimalBots/api",
                params={"api-version": "2022-03-01-preview"},
                headers=self._headers(self.pp_token or ""),
            )
            if r.status_code >= 400:
                raise StudioAgentError(f"list bots: {r.status_code} {r.text[:200]}")
            data = r.json()
            if isinstance(data, list):
                return data
            return list(data.get("value") or data.get("bots") or [])

    async def _ensure_bot(self, env_url: str) -> str:
        name = agent_display_name()
        bots = await self._list_bots(env_url)
        for b in bots:
            if b.get("shortBotName") == name or b.get("displayName") == name:
                bid = b.get("botId") or b.get("schemaName") or b.get("cdsBotId")
                if bid:
                    return str(bid)
        return await self._create_bot(env_url, name)

    async def _create_bot(self, env_url: str, name: str) -> str:
        body = {
            "botComponentChanges": [
                {
                    "component": {
                        "diagnostics": [],
                        "displayName": name,
                        "id": "00000000-0000-0000-0000-000000000000",
                        "metadata": {
                            "tools": [],
                            "conversationStarters": [],
                            "diagnostics": [],
                            "instructions": {
                                "$kind": "TemplateLine",
                                "segments": [
                                    {
                                        "$kind": "TextSegment",
                                        "value": AGENT_INSTRUCTIONS,
                                        "diagnostics": [],
                                    }
                                ],
                                "diagnostics": [],
                            },
                            "knowledgeSources": {
                                "diagnostics": [],
                                "$kind": "SearchAllKnowledgeSources",
                            },
                            "$kind": "GptComponentMetadata",
                            "gptCapabilities": {
                                "diagnostics": [],
                                "$kind": "GptCapabilities",
                                "codeInterpreter": False,
                                "generateImages": False,
                                "webBrowsing": False,
                                "searchOneDriveAndSharePoint": False,
                                "searchTeams": False,
                                "searchMeetings": False,
                                "searchEmails": False,
                                "searchPeople": False,
                            },
                            "aISettings": {
                                "diagnostics": [],
                                "$kind": "AISettings",
                                "useModelKnowledge": True,
                            },
                        },
                        "schemaName": "00000000-0000-0000-0000-000000000000.gpt.default",
                        "$kind": "GptComponent",
                        "description": "MCG auto tool agent",
                    },
                    "$kind": "BotComponentInsert",
                }
            ],
            "cloudFlowDefinitionChanges": [],
            "connectorDefinitionChanges": [],
            "environmentVariableChanges": [],
            "connectionReferenceChanges": [],
            "aIPluginOperationChanges": [],
            "componentCollectionChanges": [],
            "dataverseTableSearchChanges": [],
            "dataverseTableSearchEntityConfigurationChanges": [],
            "dataverseTableSearchGlossaryConfigurationChanges": [],
            "dataverseTableSearchEntityColumnSynonymChanges": [],
            "aIModelChanges": [],
            "connectedAgentDefinitionChanges": [],
            "bot": {
                "authorizedSecurityGroupIds": [],
                "supportedLanguages": [],
                "diagnostics": [],
                "displayName": name,
                "language": 1033,
                "schemaName": "00000000-0000-0000-0000-000000000000",
                "template": "gpt-1.1.0",
                "$kind": "BotEntity",
                "iconBase64": _BOT_ICON_B64,
            },
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                f"{env_url}/copilotstudio/minimalBots/api",
                params={"api-version": "2022-03-01-preview"},
                headers=self._headers(self.pp_token or ""),
                json=body,
            )
            if r.status_code >= 400:
                raise StudioAgentError(f"create bot: {r.status_code} {r.text[:300]}")
            data = r.json()
            bot = data.get("bot") or {}
            bid = bot.get("schemaName") or bot.get("cdsBotId") or data.get("botId")
            if not bid:
                raise StudioAgentError(f"create bot missing id: {str(data)[:200]}")
            return str(bid)

    async def _publish(self, env_url: str, bot_id: str) -> str:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(
                f"{env_url}/copilotstudio/minimalBots/api/{bot_id}/publish",
                params={"api-version": "2022-03-01-preview"},
                headers=self._headers(self.pp_token or ""),
            )
            if r.status_code >= 400:
                raise StudioAgentError(f"publish: {r.status_code} {r.text[:300]}")
            data = r.json()
            title = data.get("TitleId") or data.get("titleId")
            if not title:
                raise StudioAgentError(f"publish missing TitleId: {str(data)[:200]}")
            return str(title)
