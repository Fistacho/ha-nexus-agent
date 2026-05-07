"""Voice assistant pipelines (Assist).

Manages STT/TTS/wake-word pipelines used by HA Assist. Each pipeline ties
together a conversation engine, STT engine, TTS engine, and optional
wake-word detector.
"""
from fastmcp import FastMCP
import ha_client as ha

mcp = FastMCP("voice")


@mcp.tool()
def list_pipelines() -> dict:
    """List all Assist pipelines plus the preferred pipeline id.

    Returns: {"pipelines": [...], "preferred_pipeline": "<id>"}
    """
    return ha._ws_call("assist_pipeline/pipeline/list")


@mcp.tool()
def get_pipeline(pipeline_id: str | None = None) -> dict:
    """Get a single pipeline by id, or the preferred pipeline if id is omitted."""
    kwargs = {}
    if pipeline_id:
        kwargs["pipeline_id"] = pipeline_id
    return ha._ws_call("assist_pipeline/pipeline/get", **kwargs)


@mcp.tool()
def create_pipeline(
    name: str,
    conversation_engine: str = "homeassistant",
    conversation_language: str = "en",
    language: str = "en",
    stt_engine: str | None = None,
    stt_language: str | None = None,
    tts_engine: str | None = None,
    tts_language: str | None = None,
    tts_voice: str | None = None,
    wake_word_entity: str | None = None,
    wake_word_id: str | None = None,
) -> dict:
    """Create a new Assist pipeline.

    Engines are entity ids of the relevant providers (e.g. `stt.faster_whisper`,
    `tts.piper`, `wake_word.openwakeword`). Use list_services or list_entities
    to discover what's installed.
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


@mcp.tool()
def update_pipeline(
    pipeline_id: str,
    name: str | None = None,
    conversation_engine: str | None = None,
    conversation_language: str | None = None,
    language: str | None = None,
    stt_engine: str | None = None,
    stt_language: str | None = None,
    tts_engine: str | None = None,
    tts_language: str | None = None,
    tts_voice: str | None = None,
    wake_word_entity: str | None = None,
    wake_word_id: str | None = None,
) -> dict:
    """Update an existing Assist pipeline. Only fields you pass are sent.

    HA's WS API requires the full set of fields, so missing ones are filled
    in from the current pipeline state.
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


@mcp.tool()
def delete_pipeline(pipeline_id: str) -> dict:
    """Delete an Assist pipeline. Cannot delete the preferred pipeline — change preference first."""
    return ha._ws_call("assist_pipeline/pipeline/delete", pipeline_id=pipeline_id)


@mcp.tool()
def set_preferred_pipeline(pipeline_id: str) -> dict:
    """Set the default Assist pipeline (used when no pipeline is specified)."""
    return ha._ws_call("assist_pipeline/pipeline/set_preferred", pipeline_id=pipeline_id)


@mcp.tool()
def list_stt_engines() -> list[dict]:
    """List installed Speech-to-Text engines (entities in the `stt` domain)."""
    return [
        {"entity_id": s["entity_id"], "state": s.get("state"), "attributes": s.get("attributes", {})}
        for s in ha.get_states()
        if s["entity_id"].startswith("stt.")
    ]


@mcp.tool()
def list_tts_engines() -> list[dict]:
    """List installed Text-to-Speech engines (entities in the `tts` domain)."""
    return [
        {"entity_id": s["entity_id"], "state": s.get("state"), "attributes": s.get("attributes", {})}
        for s in ha.get_states()
        if s["entity_id"].startswith("tts.")
    ]


@mcp.tool()
def list_wake_word_engines() -> list[dict]:
    """List installed wake-word detection engines (entities in the `wake_word` domain)."""
    return [
        {"entity_id": s["entity_id"], "state": s.get("state"), "attributes": s.get("attributes", {})}
        for s in ha.get_states()
        if s["entity_id"].startswith("wake_word.")
    ]


@mcp.tool()
def list_conversation_agents() -> dict:
    """List conversation agents available for the conversation step of a pipeline."""
    return ha._ws_call("conversation/agent/list")
