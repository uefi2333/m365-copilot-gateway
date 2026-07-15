from __future__ import annotations

"""SignalR / ChatHub frame builders.

Protocol behavior documented by community reverse engineering, especially:
- cramt/m365-copilot-proxy (docs/m365-copilot-api.md, session.ts)
- kuchris/m365-copilot-openai-proxy (substrate_client.py)
- HEXUXIU/M365-Copilot2API (payload.py)

This file is an independent re-implementation of the observed wire format.
"""


import json
import uuid
from typing import Any
from urllib.parse import quote

SIGNALR_SEP = "\x1e"

WS_BASE = "wss://substrate.office.com/m365Copilot/Chathub"

# Feature flags observed on officeweb Bizchat clients (trimmed, configurable later).
DEFAULT_VARIANTS = (
    "EnableMcpServerWidgets,feature.EnableMcpServerWidgets,feature.EnableLuForChatCIQ,"
    "feature.enableChatCIQPlugin,EnableRequestPlugins,feature.EnableSensitivityLabels,"
    "EnableUnsupportedUrlDetector,feature.IsCustomEngineCopilotEnabled,feature.bizchatfluxv3,"
    "feature.enablechatpages,feature.enableCodeCanvas,feature.turnOnWorkTabRecommendation,"
    "feature.turnOnDARecommendation,feature.IsStreamingModeInChatRequestEnabled,"
    "IncludeSourceAttributionsConcise,SkipPublishEmptyMessage,"
    "feature.EnableDeduplicatingSourceAttributions,Enable3PActionProgressMessages,"
    "feature.EnableReferencesListCompleteSignal,feature.disabledisallowedmsgs,"
    "feature.enableGenerateGraphicArtOptionsSet,cdximagen,"
    "feature.OfficeWebToHelix,feature.OfficeDesktopToHelix,feature.M365TeamsHubToHelix,"
    "feature.OwaHubToHelix,feature.MonarchHubToHelix,feature.Win32OutlookHubToHelix,"
    "feature.MacOutlookHubToHelix,Agt_bizchat_enableGpt5ForHelix"
)

DEFAULT_OPTIONS_SETS = [
    "search_result_progress_messages_with_search_queries",
    "cwc_flux_image",
    "cwc_code_interpreter",
    "cwc_code_interpreter_amsfix",
    "cwcfluxgptv",
    "cwc_code_interpreter_citation_fix",
    "code_interpreter_interactive_charts",
    "cwc_fileupload_odb",
    "update_memory_plugin",
    "add_custom_instructions",
    "cwc_flux_v3",
    "flux_v3_progress_messages",
    "enable_batch_token_processing",
    "enable_gg_gpt",
]

ALLOWED_MESSAGE_TYPES = [
    "Chat",
    "Suggestion",
    "InternalSearchQuery",
    "Disengaged",
    "InternalLoaderMessage",
    "Progress",
    "GeneratedCode",
    "RenderCardRequest",
    "AdsQuery",
    "SemanticSerp",
    "GenerateContentQuery",
    "GenerateGraphicArt",
    "SearchQuery",
    "ConfirmationCard",
    "AuthError",
    "DeveloperLogs",
    "TriggerPlugin",
    "HintInvocation",
    "MemoryUpdate",
    "EndOfRequest",
    "TriggerConfirmation",
    "ResumeInvokeAction",
    "ResumeUserInputRequest",
    "TriggerUserInputRequest",
    "EscapeHatch",
    "TriggerPluginAuth",
    "ResumePluginAuth",
    "SideBySide",
    "ReferencesListComplete",
    "SwitchRespondingEndpoint",
]


def handshake_frame() -> str:
    return json.dumps({"protocol": "json", "version": 1}, ensure_ascii=False) + SIGNALR_SEP


def stop_frame() -> str:
    # Captured behavior documented by cramt (type 1 target stop).
    return (
        json.dumps({"arguments": [{}], "invocationId": "1", "target": "stop", "type": 1}, ensure_ascii=False)
        + SIGNALR_SEP
    )


def build_hub_url(
    *,
    oid: str,
    tid: str,
    access_token: str,
    conversation_id: str,
    session_id: str,
    request_id: str | None = None,
    variants: str = DEFAULT_VARIANTS,
) -> str:
    req = request_id or str(uuid.uuid4())
    token = quote(access_token, safe="")
    return (
        f"{WS_BASE}/{oid}@{tid}"
        f"?ClientRequestId={req}"
        f"&X-SessionId={session_id}"
        f"&ConversationId={conversation_id}"
        f"&access_token={token}"
        f"&variants={variants}"
        f"&source=officeweb&product=Office&agentHost=Bizchat.FullScreen"
        f"&licenseType=Starter&agent=web&scenario=OfficeWebIncludedCopilot"
    )


def build_chat_invoke(
    *,
    text: str,
    session_id: str,
    request_id: str,
    tone: str = "Magic",
    is_start_of_session: bool = True,
    time_zone: str = "Asia/Shanghai",
    time_zone_offset: int = 8,
    locale: str = "zh-cn",
    options_sets: list[str] | None = None,
    plugins: list[dict[str, Any]] | None = None,
    message_history: list[dict[str, Any]] | None = None,
    message_extras: dict[str, Any] | None = None,
) -> str:
    message: dict[str, Any] = {
        "author": "user",
        "inputMethod": "Keyboard",
        "text": text,
        "entityAnnotationTypes": ["People", "File", "Event", "Email", "TeamsMessage"],
        "requestId": request_id,
        "locationInfo": {"timeZoneOffset": time_zone_offset, "timeZone": time_zone},
        "locale": locale,
        "messageType": "Chat",
        "experienceType": "Default",
        "adaptiveCards": [],
        "clientPreferences": {},
    }
    if message_extras:
        # Best-effort multimodal keys (imageBase64 / imageUrl / …)
        message.update(message_extras)
    payload: dict[str, Any] = {
        "arguments": [
            {
                "source": "officeweb",
                "clientCorrelationId": request_id,
                "sessionId": session_id,
                "optionsSets": options_sets or DEFAULT_OPTIONS_SETS,
                "streamingMode": "ConciseWithPadding",
                "spokenTextMode": "None",
                "options": {},
                "extraExtensionParameters": {},
                "allowedMessageTypes": ALLOWED_MESSAGE_TYPES,
                "sliceIds": [],
                "threadLevelGptId": {},
                "traceId": request_id,
                "isStartOfSession": is_start_of_session,
                "clientInfo": {
                    "clientPlatform": "mcmcopilot-web",
                    "clientAppName": "Office",
                    "clientEntrypoint": "mcmcopilot-officeweb",
                    "clientSessionId": session_id,
                    "clientAppType": "Web",
                    "deviceOS": "Linux",
                    "deviceType": "Desktop",
                },
                "message": message,
                "plugins": plugins or [{"Id": "BingWebSearch", "Source": "BuiltIn"}],
                "isSbsSupported": True,
                "tone": tone,
                "renderReferencesBehindEOS": True,
            }
        ],
        "invocationId": "0",
        "target": "chat",
        "type": 4,
    }
    if message_history:
        payload["arguments"][0]["messageHistory"] = message_history
    return json.dumps(payload, ensure_ascii=False) + SIGNALR_SEP
