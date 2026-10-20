"""Per-turn audio routing through both orchestrators and SSE, with isolated data."""
import base64
import io
import json
import subprocess
import wave

import imageio_ffmpeg
from test_agent_image_input import _session, _turn
from test_agent_image_input import image_runtime as _image_runtime

from app.core.llm.models import LLMAudioBlock
from app.core.local_config import LLMModelConfigOverride

image_runtime = _image_runtime


def wav_bytes():
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b"\x00\x01" * 1600)
    return out.getvalue()


def audio_env(env):
    env.client._transport.raise_server_exceptions = True
    env.provider.audio_model_overrides = {"audio_test": True}
    env.runtime.agent_llm_client.config.audio_model = "audio_test"
    env.runtime.agent_llm_client.config.clients[0].model_overrides["audio_test"] = LLMModelConfigOverride(context_window_tokens=200000)
    original_answer = env.provider._answer
    env.provider._answer = lambda req: "Audio heard." if req.metadata.get("stage") in {"answer", "context_answer"} and req.model == "audio_test" else original_answer(req)
    return env


def upload(env, sid):
    raw = wav_bytes()
    response = env.client.post(f"/sessions/{sid}/attachments", content=raw, headers={"Content-Type": "audio/x-wav", "X-Filename": "sound.wav"})
    assert response.status_code == 200, response.text
    assert response.json()["kind"] == "audio"
    return response.json()["attachment_id"], raw


def audios(req):
    return [base64.b64decode(block.input_audio.data) for msg in req.messages if isinstance(msg.content, list) for block in msg.content if isinstance(block, LLMAudioBlock)]


def test_audio_routing_relisten_then_default_text_and_no_payload_persistence(image_runtime):
    env = audio_env(image_runtime)
    sid = _session(env)
    attachment, raw = upload(env, sid)
    result = _turn(env, sid, user_input="Describe sound.", attachment_ids=[attachment], llm={"model": "text_only"})
    assert result["answer"] == "Audio heard."
    for req in env.provider.requests:
        assert req.model == "audio_test"
        if req.metadata.get("stage") in {"route", "decision", "answer", "context_answer"}:
            assert audios(req) == [raw]
    detail = env.client.get(f"/sessions/{sid}").json()
    assert next(m for m in detail["messages"] if m["role"] == "user")["payload"]["attachment_ids"] == [attachment]
    encoded = base64.b64encode(raw)
    for path in env.data_dir.rglob("*"):
        if path.is_file():
            assert encoded not in path.read_bytes(), str(path)
    env.provider.requests.clear()
    _turn(env, sid, user_input="Listen again.", attachment_ids=[attachment])
    assert all(req.model == "audio_test" for req in env.provider.requests)
    for selection in [{}, {"attachment_ids": None}, {"attachment_ids": []}]:
        env.provider.requests.clear()
        result = _turn(env, sid, user_input="Rewrite the analysis.", **selection)
        assert result["answer"] != "Audio heard."
        assert all(req.model == "visual" and not audios(req) for req in env.provider.requests)
    assert env.runtime.agent_llm_client.registry.get(env.provider.name).default_model == "visual"
    if env.orchestrator == "langgraph":
        assert env.runtime.agent_turn_runner.get_state(result["run_id"]).values["request"]["attachment_ids"] == []


def test_audio_stream_and_audio_only_input(image_runtime):
    env = audio_env(image_runtime)
    sid = _session(env)
    attachment, raw = upload(env, sid)
    response = env.client.post("/agent/turn/stream", json={"session_id": sid, "attachment_ids": [attachment]})
    assert response.status_code == 200, response.text
    frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert any(f["type"] == "llm_delta" and f["payload"].get("display_target") == "assistant_answer" and f["payload"].get("delta") == "Audio heard." for f in frames)
    assert any(f["type"] == "run_completed" for f in frames)
    assert base64.b64encode(raw).decode() not in response.text
    assert all(req.model == "audio_test" for req in env.provider.requests)


def test_audio_unavailable_and_cross_session_rejected_before_run(image_runtime):
    env = audio_env(image_runtime)
    sid, other = _session(env), _session(env)
    attachment, _raw = upload(env, sid)
    response = env.client.post("/agent/turn", json={"session_id": other, "user_input": "Listen", "attachment_ids": [attachment]})
    assert response.status_code == 404
    env.runtime.agent_llm_client.config.audio_model = None
    response = env.client.post("/agent/turn", json={"session_id": sid, "user_input": "Listen", "attachment_ids": [attachment]})
    assert response.status_code == 422
    assert not env.provider.requests


def test_m4a_upload_keeps_original_and_dispatches_normalized_wav(image_runtime, tmp_path):
    env = audio_env(image_runtime)
    sid = _session(env)
    source, target = tmp_path / "source.wav", tmp_path / "sound.m4a"
    source.write_bytes(wav_bytes())
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe", "-i", str(source), "-c:a", "aac", str(target)], check=True, timeout=10)
    raw = target.read_bytes()
    response = env.client.post(f"/sessions/{sid}/attachments", content=raw, headers={"Content-Type": "audio/mp4", "X-Filename": "sound.m4a"})
    assert response.status_code == 200, response.text
    metadata = response.json()
    assert metadata["audio_format"] == "wav" and metadata["source_format"] == "m4a"
    attachment = metadata["attachment_id"]
    assert env.client.get(f"/sessions/{sid}/attachments/{attachment}/raw").content == raw
    assert _turn(env, sid, user_input="Listen to the M4A.", attachment_ids=[attachment])["answer"] == "Audio heard."
    dispatched = [data for req in env.provider.requests for data in audios(req)]
    assert dispatched
    for data in dispatched:
        assert data.startswith(b"RIFF") and data[8:12] == b"WAVE"
        assert data != raw
        with wave.open(io.BytesIO(data)) as f:
            assert f.getframerate() == 16000 and f.getnchannels() == 1
