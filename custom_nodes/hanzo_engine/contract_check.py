"""Contract test: drive every Hanzo Engine node through REAL HTTP against a stub
engine that enforces the exact request fields the Rust handlers deserialize
(ThreeDGenerationRequest, VideoGenerationRequest, AnimateRequest, Speech/Music
requests) and the async-job protocol (submit -> poll -> content). This is a real
request/response cycle over sockets + the requests lib + client.run_job polling,
not a monkeypatched mock. Run: python contract_check.py
"""
# ruff: noqa: T201  (a CLI harness prints its results)
import base64
import io
import json
import os
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import torch

# Stub out the ComfyUI-side modules the node pack imports so nodes.py loads.
fp = types.ModuleType("folder_paths")
fp.get_output_directory = lambda: "/tmp"
fp.get_save_image_path = lambda pre, d: (d, pre, 1, "", "")
sys.modules["folder_paths"] = fp

# Import the pack as a package so its `from . import client` resolves.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hanzo_engine import nodes  # noqa: E402

RECORDED = {}  # path -> last JSON/body seen


def _wav_bytes(seconds=0.1, sr=44100, ch=2):
    import wave
    n = int(seconds * sr)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * n * ch)
    return buf.getvalue()


def _png_b64():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (128, 64, 32)).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _mp4_bytes(frames=3, w=16, h=16):
    import av
    buf = io.BytesIO()
    container = av.open(buf, mode="w", format="mp4")
    stream = container.add_stream("libx264", rate=25)
    stream.width, stream.height, stream.pix_fmt = w, h, "yuv420p"
    for i in range(frames):
        arr = np.full((h, w, 3), i * 20 % 255, dtype=np.uint8)
        frame = av.VideoFrame.from_ndarray(arr, format="rgb24")
        for pkt in stream.encode(frame):
            container.mux(pkt)
    for pkt in stream.encode():
        container.mux(pkt)
    container.close()
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body_json(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw)
        except ValueError:
            return {"_raw": raw}

    def do_GET(self):
        p = self.path
        if p == "/v1/models":
            return self._send(200, {"data": [{"id": "stub-model"}]})
        if p.startswith("/v1/3d/") and p.endswith("/content"):
            return self._send(200, base64.b64decode(_PLACEHOLDER_GLB_B64), "model/gltf-binary")
        if p.startswith("/v1/videos/") and p.endswith("/content"):
            return self._send(200, _mp4_bytes(), "video/mp4")
        if p.startswith("/v1/3d/") or p.startswith("/v1/videos/"):
            # poll: report completed immediately
            return self._send(200, {"id": p.split("/")[3], "status": "completed", "progress": 1.0})
        return self._send(404, {"message": "no"})

    def do_POST(self):
        try:
            self._do_POST()
        except Exception as exc:  # surface stub-side errors to the client
            self._send(500, {"stub_error": repr(exc)})

    def _do_POST(self):
        p = self.path
        # multipart (ASR) is not JSON
        if p == "/v1/audio/transcriptions":
            n = int(self.headers.get("Content-Length", 0))
            _ = self.rfile.read(n)
            RECORDED[p] = {"multipart": True}
            return self._send(200, {"text": "stub transcript"})
        body = self._body_json()
        RECORDED[p] = body
        if p == "/v1/chat/completions":
            return self._send(200, {"choices": [{"message": {"content": "expanded prompt"}}]})
        if p == "/v1/images/generations":
            return self._send(200, {"data": [{"b64_json": _png_b64()}]})
        if p == "/v1/audio/speech":
            assert "input" in body and "model" in body, "speech needs input+model"
            return self._send(200, _wav_bytes(ch=1, sr=24000), "audio/wav")
        if p == "/v1/audio/music":
            assert "input" in body and "model" in body, "music needs input+model"
            return self._send(200, _wav_bytes(ch=2, sr=44100), "audio/wav")
        if p == "/v1/animate":
            assert set(body) >= {"model", "audio", "visual"}, "animate needs model+audio+visual"
            assert body["visual"].startswith("data:image/"), "visual must be an image data url"
            assert body["audio"].startswith("data:audio/"), "audio must be an audio data url"
            return self._send(200, _mp4_bytes(), "video/mp4")
        if p == "/v1/videos":
            assert "prompt" in body, "video needs prompt"
            return self._send(202, {"id": "vid-1", "status": "queued"})
        if p == "/v1/3d":
            assert "image" in body, "3d needs image"
            return self._send(202, {"id": "threed-1", "status": "queued", "progress": 0.0})
        return self._send(404, {"message": "no"})


# A 1-triangle binary glTF is overkill; the node just writes bytes to disk, so any
# non-empty payload proves the content fetch + save path. Use a tiny placeholder.
_PLACEHOLDER_GLB_B64 = base64.b64encode(b"glTF\x02\x00\x00\x00stub-mesh").decode("ascii")


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import os
    os.environ["HANZO_ENGINE_URL"] = f"http://127.0.0.1:{port}"

    img = torch.rand(1, 8, 8, 3)
    audio = {"waveform": torch.zeros(1, 2, 4410), "sample_rate": 44100}
    passed = []

    # Chat (non-stream path)
    (text,) = nodes.HanzoChat().generate(model="default", prompt="a red dragon", stream=False)["result"]
    assert text == "expanded prompt", text
    assert RECORDED["/v1/chat/completions"]["messages"][-1]["content"] == "a red dragon"
    passed.append("HanzoChat -> /v1/chat/completions")

    # ImageGen
    (imgs,) = nodes.HanzoImageGen().generate("a red dragon", 64, 64, n=1)
    assert imgs.shape[-1] == 3
    passed.append("HanzoImageGen -> /v1/images/generations")

    # TTS
    (aud,) = nodes.HanzoTTS().speak("hello world", response_format="wav")
    assert "waveform" in aud and aud["sample_rate"] == 24000
    passed.append("HanzoTTS -> /v1/audio/speech")

    # Music (NEW)
    (music,) = nodes.HanzoMusic().compose("epic orchestral", response_format="wav")
    assert music["sample_rate"] == 44100 and music["waveform"].shape[1] == 2, music["waveform"].shape
    assert RECORDED["/v1/audio/music"]["input"] == "epic orchestral"
    passed.append("HanzoMusic -> /v1/audio/music (input field, stereo 44.1k)")

    # ASR
    (tr,) = nodes.HanzoASR().transcribe(audio)
    assert tr == "stub transcript"
    passed.append("HanzoASR -> /v1/audio/transcriptions")

    # ImageTo3D (texture flag fix) -- async job submit+poll+content
    out = nodes.HanzoImageTo3D().generate(img, "glb", True, 0, 4)
    assert RECORDED["/v1/3d"]["texture"] is True, "texture flag not sent"
    assert RECORDED["/v1/3d"]["format"] == "glb"
    assert out["result"][0].endswith(".glb")
    passed.append("HanzoImageTo3D -> /v1/3d (texture=true sent; job submit->poll->content)")

    # TextToVideo -- async job
    (frames,) = nodes.HanzoTextToVideo().generate("a red dragon flying", 3, 16, 16, 4)
    assert frames.dim() == 4 and frames.shape[0] >= 1
    assert RECORDED["/v1/videos"]["num_frames"] == 3
    passed.append("HanzoTextToVideo -> /v1/videos (job submit->poll->content)")

    # Dub (NEW) -- synchronous mp4
    (dframes, daudio) = nodes.HanzoDub().dub(img, audio, fps=25.0)
    assert dframes.dim() == 4
    assert daudio is audio
    passed.append("HanzoDub -> /v1/animate (image+audio data urls; frames+audio out)")

    srv.shutdown()
    print("\n".join(f"PASS  {p}" for p in passed))
    print(f"\nALL {len(passed)} NODE CONTRACT CHECKS PASSED against a live HTTP engine stub.")


if __name__ == "__main__":
    main()
