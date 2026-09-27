"""Voice assistant pipelines (Assist).

Manages STT/TTS/wake-word pipelines used by HA Assist. Each pipeline ties
together a conversation engine, STT engine, TTS engine, and optional
wake-word detector.
"""
from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from pydantic import Field

import ha_client as ha
from tools._contract import destructive, read, write

mcp = FastMCP("voice")

TOOL_CONTRACT = {"version": 1, "long_ok": {}, "heuristic_exceptions": {}}


@mcp.tool(annotations=read("List Assist pipelines"))
def list_pipelines() -> dict:
    """List all Assist pipelines plus the preferred pipeline id.

    Calls WS `assist_pipeline/pipeline/list` and returns its payload
    unchanged.

    Use when: you need every configured pipeline, e.g. to pick one for
    `voice_set_preferred_pipeline` or before editing one with
    `voice_update_pipeline`.
    Not for: a single pipeline's full detail — use `voice_get_pipeline`.
    Returns: `{"pipelines": [...], "preferred_pipeline": "<id>"}`.
    """
    return ha._ws_call("assist_pipeline/pipeline/list")


@mcp.tool(annotations=read("Get an Assist pipeline"))
def get_pipeline(
    pipeline_id: Annotated[
        str | None,
        Field(
            description=(
                "Pipeline ID to fetch, from `voice_list_pipelines`. Omit to "
                "return the current preferred pipeline instead."
            )
        ),
    ] = None,
) -> dict:
    """Get a single Assist pipeline by id, or the preferred one if omitted.

    Calls WS `assist_pipeline/pipeline/get`, passing `pipeline_id` only when
    given.

    Use when: you need one pipeline's full configuration (engines,
    languages) rather than the whole list.
    Not for: every pipeline at once — use `voice_list_pipelines`.
    Returns: dict with the pipeline's engine/language fields, as returned by
    HA.
    """
    kwargs = {}
    if pipeline_id:
        kwargs["pipeline_id"] = pipeline_id
    return ha._ws_call("assist_pipeline/pipeline/get", **kwargs)


@mcp.tool(annotations=write("Create an Assist pipeline", idempotent=False))
def create_pipeline(
    name: Annotated[
        str,
        Field(description="Display name for the new pipeline, e.g. 'Kitchen Assist'."),
    ],
    conversation_engine: Annotated[
        str,
        Field(
            description=(
                "Entity ID of the conversation agent, e.g. 'homeassistant' "
                "for the built-in agent."
            )
        ),
    ] = "homeassistant",
    conversation_language: Annotated[
        str,
        Field(description="Language code for the conversation agent, e.g. 'en' or 'pl'."),
    ] = "en",
    language: Annotated[
        str,
        Field(description="Overall pipeline language code, e.g. 'en' or 'pl'."),
    ] = "en",
    stt_engine: Annotated[
        str | None,
        Field(
            description=(
                "Entity ID of the speech-to-text engine, e.g. "
                "'stt.faster_whisper'. Omit for no STT. Discover installed "
                "ones with `voice_list_stt_engines`."
            )
        ),
    ] = None,
    stt_language: Annotated[
        str | None,
        Field(description="Language code for the STT engine. Omit to use its default."),
    ] = None,
    tts_engine: Annotated[
        str | None,
        Field(
            description=(
                "Entity ID of the text-to-speech engine, e.g. 'tts.piper'. "
                "Omit for no TTS. Discover installed ones with "
                "`voice_list_tts_engines`."
            )
        ),
    ] = None,
    tts_language: Annotated[
        str | None,
        Field(description="Language code for the TTS engine. Omit to use its default."),
    ] = None,
    tts_voice: Annotated[
        str | None,
        Field(description="Voice ID for the TTS engine. Omit to use its default voice."),
    ] = None,
    wake_word_entity: Annotated[
        str | None,
        Field(
            description=(
                "Entity ID of the wake-word detector, e.g. "
                "'wake_word.openwakeword'. Omit for no wake word. Discover "
                "installed ones with `voice_list_wake_word_engines`."
            )
        ),
    ] = None,
    wake_word_id: Annotated[
        str | None,
        Field(description="Specific wake word model ID within `wake_word_entity`. Omit for its default."),
    ] = None,
) -> dict:
    """Create a new Assist pipeline via WS `assist_pipeline/pipeline/create`.

    Every parameter is forwarded as-is to HA; engine parameters are entity
    IDs of the relevant provider (`stt.*`, `tts.*`, `wake_word.*`) rather
    than free-form names. Calling this twice with the same `name` creates
    two separate pipelines with distinct IDs, since HA assigns a new ID each
    time.

    Use when: setting up a new voice pipeline with a specific combination of
    conversation/STT/TTS/wake-word engines.
    Not for: changing an existing pipeline — use `voice_update_pipeline`.
    Returns: the created pipeline dict, as returned by HA (includes its new
    `id`).
    Limits: engine parameters must already be installed entity IDs; discover
    them with `voice_list_stt_engines`, `voice_list_tts_engines`,
    `voice_list_wake_word_engines` and `voice_list_conversation_agents`
    first.
    """
    payload: dict = {
        "name": name,
        "conversation_engine": conversation_engine,
        "conversation_language": conversation_language,
        "language": language,
        "stt_engine": stt_engine,
        "stt_language": stt_language,
        "tts_engine": tts_engine,
        "tts_language": tts_language,
        "tts_voice": tts_voice,
        "wake_word_entity": wake_word_entity,
        "wake_word_id": wake_word_id,
    }
    return ha._ws_call("assist_pipeline/pipeline/create", **payload)


@mcp.tool(annotations=destructive("Update an Assist pipeline", idempotent=False))
def update_pipeline(
    pipeline_id: Annotated[
        str,
        Field(description="ID of the pipeline to update, from `voice_list_pipelines`."),
    ],
    name: Annotated[
        str | None,
        Field(description="New display name. Omit to keep the current name."),
    ] = None,
    conversation_engine: Annotated[
        str | None,
        Field(description="New conversation agent entity ID. Omit to keep the current one."),
    ] = None,
    conversation_language: Annotated[
        str | None,
        Field(description="New conversation language code. Omit to keep the current one."),
    ] = None,
    language: Annotated[
        str | None,
        Field(description="New overall pipeline language code. Omit to keep the current one."),
    ] = None,
    stt_engine: Annotated[
        str | None,
        Field(description="New STT engine entity ID. Omit to keep the current one."),
    ] = None,
    stt_language: Annotated[
        str | None,
        Field(description="New STT language code. Omit to keep the current one."),
    ] = None,
    tts_engine: Annotated[
        str | None,
        Field(description="New TTS engine entity ID. Omit to keep the current one."),
    ] = None,
    tts_language: Annotated[
        str | None,
        Field(description="New TTS language code. Omit to keep the current one."),
    ] = None,
    tts_voice: Annotated[
        str | None,
        Field(description="New TTS voice ID. Omit to keep the current one."),
    ] = None,
    wake_word_entity: Annotated[
        str | None,
        Field(description="New wake-word detector entity ID. Omit to keep the current one."),
    ] = None,
    wake_word_id: Annotated[
        str | None,
        Field(description="New wake word model ID. Omit to keep the current one."),
    ] = None,
) -> dict:
    """Update an existing Assist pipeline's fields via WS `assist_pipeline/pipeline/update`.

    HA's WS API requires the full set of fields on every update call, so
    this first fetches the current pipeline with WS
    `assist_pipeline/pipeline/get` and fills in any parameter left as
    `None` from that current state before sending the update — only the
    parameters you actually pass are changed; the previous value of any
    field you omit is lost from this call's perspective and replaced by
    whatever `assist_pipeline/pipeline/get` returned for it.

    Use when: changing one or more fields of an existing pipeline.
    Not for: creating a new pipeline — use `voice_create_pipeline`.
    Returns: the updated pipeline dict, as returned by HA.
    """
    current = ha._ws_call("assist_pipeline/pipeline/get", pipeline_id=pipeline_id)
    payload: dict = {"pipeline_id": pipeline_id}
    fields = {
        "name": name,
        "conversation_engine": conversation_engine,
        "conversation_language": conversation_language,
        "language": language,
        "stt_engine": stt_engine,
        "stt_language": stt_language,
        "tts_engine": tts_engine,
        "tts_language": tts_language,
        "tts_voice": tts_voice,
        "wake_word_entity": wake_word_entity,
        "wake_word_id": wake_word_id,
    }
    for k, v in fields.items():
        payload[k] = v if v is not None else current.get(k)
    return ha._ws_call("assist_pipeline/pipeline/update", **payload)


@mcp.tool(annotations=destructive("Delete an Assist pipeline", idempotent=True))
def delete_pipeline(
    pipeline_id: Annotated[
        str,
        Field(description="ID of the pipeline to delete, from `voice_list_pipelines`."),
    ],
) -> dict:
    """Delete an Assist pipeline via WS `assist_pipeline/pipeline/delete`.

    HA rejects deleting the pipeline currently marked preferred — call
    `voice_set_preferred_pipeline` with a different id first in that case.

    Use when: permanently removing a pipeline that is no longer needed.
    Not for: the preferred pipeline without switching preference first, and
    not for temporarily disabling a pipeline (HA has no such state).
    Returns: HA's WS response for `assist_pipeline/pipeline/delete`
    (typically empty on success).
    """
    return ha._ws_call("assist_pipeline/pipeline/delete", pipeline_id=pipeline_id)


@mcp.tool(annotations=write("Set the preferred Assist pipeline", idempotent=True))
def set_preferred_pipeline(
    pipeline_id: Annotated[
        str,
        Field(description="ID of the pipeline to make preferred, from `voice_list_pipelines`."),
    ],
) -> dict:
    """Set the default Assist pipeline, used when no pipeline is specified.

    Calls WS `assist_pipeline/pipeline/set_preferred`.

    Use when: changing which pipeline Assist uses by default.
    Not for: reading the current preference — use `voice_list_pipelines`,
    whose response includes `preferred_pipeline`.
    Returns: HA's WS response for `assist_pipeline/pipeline/set_preferred`
    (typically empty on success).
    """
    return ha._ws_call("assist_pipeline/pipeline/set_preferred", pipeline_id=pipeline_id)


@mcp.tool(annotations=read("List speech-to-text engines"))
def list_stt_engines() -> list[dict]:
    """List installed speech-to-text engines as their `stt.*` entities.

    Filters `ha.get_states()` for entity IDs starting with `stt.`.

    Use when: discovering which STT engine entity ID to pass to
    `voice_create_pipeline`/`voice_update_pipeline`.
    Not for: TTS or wake-word engines — use `voice_list_tts_engines`/
    `voice_list_wake_word_engines`.
    Returns: list of `{"entity_id", "state", "attributes"}` for each `stt.*`
    entity.
    """
    return [
        {"entity_id": s["entity_id"], "state": s.get("state"), "attributes": s.get("attributes", {})}
        for s in ha.get_states()
        if s["entity_id"].startswith("stt.")
    ]


@mcp.tool(annotations=read("List text-to-speech engines"))
def list_tts_engines() -> list[dict]:
    """List installed text-to-speech engines as their `tts.*` entities.

    Filters `ha.get_states()` for entity IDs starting with `tts.`.

    Use when: discovering which TTS engine entity ID to pass to
    `voice_create_pipeline`/`voice_update_pipeline`.
    Not for: STT or wake-word engines — use `voice_list_stt_engines`/
    `voice_list_wake_word_engines`.
    Returns: list of `{"entity_id", "state", "attributes"}` for each `tts.*`
    entity.
    """
    return [
        {"entity_id": s["entity_id"], "state": s.get("state"), "attributes": s.get("attributes", {})}
        for s in ha.get_states()
        if s["entity_id"].startswith("tts.")
    ]


@mcp.tool(annotations=read("List wake-word engines"))
def list_wake_word_engines() -> list[dict]:
    """List installed wake-word detection engines as their `wake_word.*` entities.

    Filters `ha.get_states()` for entity IDs starting with `wake_word.`.

    Use when: discovering which wake-word entity ID to pass to
    `voice_create_pipeline`/`voice_update_pipeline`.
    Not for: STT or TTS engines — use `voice_list_stt_engines`/
    `voice_list_tts_engines`.
    Returns: list of `{"entity_id", "state", "attributes"}` for each
    `wake_word.*` entity.
    """
    return [
        {"entity_id": s["entity_id"], "state": s.get("state"), "attributes": s.get("attributes", {})}
        for s in ha.get_states()
        if s["entity_id"].startswith("wake_word.")
    ]


@mcp.tool(annotations=read("List conversation agents"))
def list_conversation_agents() -> dict:
    """List conversation agents available for a pipeline's conversation step.

    Calls WS `conversation/agent/list` and returns its payload unchanged.

    Use when: discovering which `conversation_engine` value to pass to
    `voice_create_pipeline`/`voice_update_pipeline`.
    Not for: STT/TTS/wake-word engines — use `voice_list_stt_engines`/
    `voice_list_tts_engines`/`voice_list_wake_word_engines`.
    Returns: dict as returned by HA's `conversation/agent/list` WS command.
    """
    return ha._ws_call("conversation/agent/list")
