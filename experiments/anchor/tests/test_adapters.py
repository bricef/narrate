"""Each adapter against canned responses shaped as its API documents them: the request it builds, and
how it reads the answer. No network; the shapes come from the specs behind #87."""

import base64
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from anchor.providers import adapter


class Response:
    def __init__(self, body=None, status=200, content=b"", text=None):
        self.body, self.status_code, self.content = body, status, content
        self.ok = status < 400
        self.text = text if text is not None else json.dumps(body) if body is not None else ""

    def json(self):
        return self.body

    def iter_content(self, size):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Fake:
    """Stands in for `requests`: answers by the first route whose text is in the URL, and keeps every call."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def _answer(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        for (m, part), answer in self.routes.items():
            if m == method and part in url:
                return answer(kwargs) if callable(answer) else answer
        raise AssertionError(f"unexpected {method} {url}")

    def post(self, url, **kwargs):
        return self._answer("POST", url, kwargs)

    def get(self, url, **kwargs):
        return self._answer("GET", url, kwargs)


def png_bytes(size=(64, 36), fmt="PNG"):
    out = io.BytesIO()
    Image.new("RGB", size, "teal").save(out, fmt)
    return out.getvalue()


@pytest.fixture
def files(tmp_path):
    (tmp_path / "portraits").mkdir()
    portrait = tmp_path / "portraits" / "m-male-1.png"
    portrait.write_bytes(png_bytes((400, 225)))
    audio = tmp_path / "line.wav"
    audio.write_bytes(b"RIFF....WAVE")
    return {"portrait": portrait, "audio": audio, "prompt": "An anchor speaks."}


def service(provider, settings, env, monkeypatch, module, fake):
    monkeypatch.setattr(f"anchor.providers.{module}.requests", fake)
    return adapter(provider)(settings, env)


def test_openai(monkeypatch):
    fake = Fake({("POST", "/v1/images/generations"): Response({"data": [{"b64_json": base64.b64encode(b"img").decode()}]})})
    s = service("openai", {"model": "gpt-image-2.5-flare", "quality": "max", "size": "2048x1152"},
                {"OPENAI_API_KEY": "k"}, monkeypatch, "openai", fake)
    made = s.generate("a prompt", "16:9")
    sent = fake.calls[0][2]
    assert sent["json"]["size"] == "2048x1152" and sent["json"]["quality"] == "max" and "seed" not in sent["json"]
    assert sent["headers"]["Authorization"] == "Bearer k"
    assert made.data == b"img" and made.cost_usd == pytest.approx(5650 * 30 / 1e6, abs=1e-4)


def test_bfl_flux3_polls_then_fetches_the_signed_result(monkeypatch):
    states = iter([{"status": "Pending"}, {"status": "Ready", "result": {"sample": "https://delivery.bfl.ai/x.png"}}])
    fake = Fake({("POST", "/v1/flux-3-image"): Response({"id": "1", "polling_url": "https://api.bfl.ai/poll/1",
                                                         "cost": 10, "output_mp": 4.2}),
                 ("GET", "/poll/1"): lambda kw: Response(next(states)),
                 ("GET", "delivery.bfl.ai"): Response(content=b"png")})
    monkeypatch.setattr("anchor.providers.bfl.time.sleep", lambda s: None)
    s = service("bfl", {"model": "flux-3-image", "resolution": "2k"}, {"BFL_API_KEY": "k"}, monkeypatch, "bfl", fake)
    made = s.generate("a prompt", "16:9")
    body = fake.calls[0][2]["json"]
    assert body == {"prompt": "a prompt", "aspect_ratio": "16:9", "resolution": "2k", "safety_tolerance": 2,
                    "grounding": False}  # nothing FLUX 3 would refuse
    assert "x-key" not in (fake.calls[-1][2].get("headers") or {})  # the signed URL gets no key
    assert made.data == b"png" and made.cost_usd == 0.10


def test_gemini_skips_thought_images_and_saves_png(monkeypatch):
    final = base64.b64encode(png_bytes(fmt="JPEG")).decode()
    body = {"candidates": [{"content": {"parts": [
        {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(b"draft").decode()}, "thought": True},
        {"inlineData": {"mimeType": "image/jpeg", "data": final}}]}}],
        "usageMetadata": {"candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": 1680}], "thoughtsTokenCount": 400}}
    fake = Fake({("POST", ":generateContent"): Response(body)})
    s = service("google", {"model": "gemini-nano-banana-2.1", "size": "2K"}, {"GEMINI_API_KEY": "k"},
                monkeypatch, "google", fake)
    made = s.generate("a prompt", "16:9")
    sent = fake.calls[0][2]
    assert sent["json"]["generationConfig"]["imageConfig"] == {"aspectRatio": "16:9", "imageSize": "2K"}
    assert sent["headers"]["x-goog-api-key"] == "k"
    assert made.data.startswith(b"\x89PNG") and made.cost_usd == pytest.approx(0.0504 + 400 * 7.5 / 1e6, abs=1e-4)


def test_seedream_turns_the_watermark_off(monkeypatch):
    fake = Fake({("POST", "/images/generations"): Response({"data": [{"b64_json": base64.b64encode(b"img").decode()}],
                                                           "usage": {"generated_images": 1}})})
    s = service("byteplus-image", {"model": "dola-seedream-5-0-pro-260628", "size": "2048x1152", "est_usd": 0.045},
                {"ARK_API_KEY": "k"}, monkeypatch, "byteplus", fake)
    made = s.generate("a prompt", "16:9")
    body = fake.calls[0][2]["json"]
    assert body["watermark"] is False and body["size"] == "2048x1152" and made.cost_usd == 0.045


def test_hedra_uploads_estimates_and_submits_once(monkeypatch, files):
    fake = Fake({("POST", "/v3/files"): Response({"url": "https://files.hedra.com/x"}, 201),
                 ("POST", "/estimate"): Response({"cost": 0.625, "currency": "USD"}),
                 ("POST", "/models/hedra-avatar"): Response({"job_id": "j1"}, 202),
                 ("GET", "/jobs/j1/status"): Response({"status": "COMPLETED"}),
                 ("GET", "/jobs/j1"): Response({"status": "COMPLETED", "outputs": [{"status": "COMPLETED",
                                                                                     "url": "https://out/x.mp4"}]})})
    monkeypatch.setattr("anchor.uploads.requests", fake)
    s = service("hedra", {"resolution": "1080p", "aspect": "16:9"}, {"HEDRA_API_KEY": "id:secret"},
                monkeypatch, "hedra", fake)
    job = s.submit(files, 10.0)
    submit = next(kw for m, url, kw in fake.calls if url.endswith("/models/hedra-avatar"))
    assert submit["json"]["input"]["resolution"] == "1080p" and submit["json"]["idempotency_key"]
    assert submit["headers"]["Authorization"] == "Key id:secret"
    status = s.poll(job)
    assert status.state == "done" and status.made.url == "https://out/x.mp4" and status.made.cost_usd == 0.625


def test_heygen_uses_v3_avatar_iv_at_1080p(monkeypatch, files):
    fake = Fake({("POST", "/v3/assets"): Response({"data": {"asset_id": "a1"}}),
                 ("POST", "/v3/videos"): Response({"data": {"video_id": "v1"}}),
                 ("GET", "/v3/videos/v1"): Response({"data": {"status": "completed", "video_url": "https://out/v.mp4"}})})
    s = service("heygen", {}, {"HEYGEN_API_KEY": "k"}, monkeypatch, "heygen", fake)
    job = s.submit(files, 10.0)
    create = next((url, kw) for m, url, kw in fake.calls if url.endswith("/v3/videos"))
    assert create[1]["json"]["type"] == "image" and create[1]["json"]["resolution"] == "1080p"
    assert create[1]["headers"]["Idempotency-Key"]
    assert s.poll(job).made.url == "https://out/v.mp4"


def test_kling_avatar_sends_raw_base64_in_pro_mode(monkeypatch, files):
    fake = Fake({("POST", "/v1/videos/avatar/image2video"): Response({"code": 0, "data": {"task_id": "t1"}}),
                 ("GET", "/v1/videos/avatar/image2video/t1"): Response({"code": 0, "data": {
                     "task_status": "succeed", "final_unit_deduction": 8,
                     "task_result": {"videos": [{"url": "https://out/k.mp4"}]}}})})
    s = service("kling-avatar", {"mode": "pro"}, {"KLING_API_KEY": "k"}, monkeypatch, "kling", fake)
    job = s.submit(files, 10.0)
    body = fake.calls[0][2]["json"]
    assert body["mode"] == "pro" and not body["image"].startswith("data:")
    assert base64.b64decode(body["sound_file"]) == b"RIFF....WAVE"
    status = s.poll(job)
    assert status.made.url == "https://out/k.mp4" and status.made.cost_usd == pytest.approx(8 * 0.14)


def test_omnihuman_publishes_its_inputs_and_reads_the_nested_result(monkeypatch, files):
    published = []

    def litterbox(kw):
        published.append(kw["files"]["fileToUpload"][0])
        return Response(text=f"https://litter.catbox.moe/{len(published)}")

    fake = Fake({("POST", "litterbox.catbox.moe"): litterbox,
                 ("POST", "/submit_task"): Response({"code": 10000, "data": {"task_id": "o1"}}),
                 ("POST", "/get_result"): Response({"code": 10000, "data": {
                     "status": "done", "resp_data": json.dumps({"video_url": "https://out/o.mp4"})}})})
    monkeypatch.setattr("anchor.uploads.requests", fake)
    s = service("omnihuman", {"resolution": "1080p", "est_usd_per_s": 0.12}, {"BYTEPLUS_CV_API_KEY": "k"},
                monkeypatch, "byteplus", fake)
    job = s.submit(files, 10.0)
    body = next(kw for m, url, kw in fake.calls if url.endswith("/submit_task"))["json"]
    assert body["image_url"].startswith("https://litter") and body["audio_url"].startswith("https://litter")
    assert body["output_resolution"] == 1080 and published == ["m-male-1.jpg", "line.wav"]
    status = s.poll(job)
    assert status.made.url == "https://out/o.mp4" and status.made.cost_usd == pytest.approx(1.2)


def test_publishing_can_be_refused(files):
    from anchor.providers.base import ServiceError
    from anchor.uploads import publish

    with pytest.raises(ServiceError, match="uploads: none"):
        publish(Path(files["audio"]), "none")
