import json

import httpx

from wlparser.llm import LocalLLM


def make_llm(handler, *, model=""):
    return LocalLLM(
        base_url="http://ollama.test/v1",
        model=model,
        transport=httpx.MockTransport(handler),
    )


def models_response(*names):
    return httpx.Response(200, json={"object": "list", "data": [{"id": n} for n in names]})


def chat_response(text):
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


async def test_chat_returns_the_model_text():
    seen = {}

    def handler(request):
        if request.url.path == "/v1/models":
            return models_response("llama3.1:latest")
        seen["body"] = json.loads(request.content)
        return chat_response("  Hello world  ")

    llm = make_llm(handler)
    assert await llm.chat("sys", "usr") == "Hello world"
    assert seen["body"]["model"] == "llama3.1:latest"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"}
    assert seen["body"]["messages"][1] == {"role": "user", "content": "usr"}
    await llm.aclose()


async def test_pinned_model_skips_the_models_lookup():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return chat_response("ok")

    llm = make_llm(handler, model="qwen2.5:14b")
    assert await llm.chat("s", "u") == "ok"
    assert paths == ["/v1/chat/completions"]
    await llm.aclose()


async def test_auto_model_skips_embedding_models():
    def handler(request):
        return models_response("nomic-embed-text:latest", "mistral:latest")

    llm = make_llm(handler)
    assert await llm.resolve_model() == "mistral:latest"
    await llm.aclose()


async def test_auto_model_is_cached_after_first_success():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return models_response("llama3.1")

    llm = make_llm(handler)
    await llm.resolve_model()
    await llm.resolve_model()
    assert calls == ["/v1/models"]
    await llm.aclose()


async def test_chat_returns_none_when_ollama_is_down():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    llm = make_llm(handler)
    assert await llm.chat("s", "u") is None
    await llm.aclose()


async def test_chat_returns_none_on_http_error():
    def handler(request):
        if request.url.path == "/v1/models":
            return models_response("llama3.1")
        return httpx.Response(500, text="boom")

    llm = make_llm(handler)
    assert await llm.chat("s", "u") is None
    await llm.aclose()


async def test_chat_returns_none_on_malformed_json():
    def handler(request):
        if request.url.path == "/v1/models":
            return models_response("llama3.1")
        return httpx.Response(200, json={"unexpected": True})

    llm = make_llm(handler)
    assert await llm.chat("s", "u") is None
    await llm.aclose()


async def test_chat_returns_none_when_no_models_are_pulled():
    def handler(request):
        return models_response()

    llm = make_llm(handler)
    assert await llm.chat("s", "u") is None
    await llm.aclose()


async def test_chat_returns_none_for_blank_content():
    def handler(request):
        if request.url.path == "/v1/models":
            return models_response("llama3.1")
        return chat_response("   ")

    llm = make_llm(handler)
    assert await llm.chat("s", "u") is None
    await llm.aclose()


async def test_health_ok_names_the_model():
    llm = make_llm(lambda request: models_response("llama3.1"))
    ok, detail = await llm.health()
    assert ok is True
    assert "llama3.1" in detail
    await llm.aclose()


async def test_health_reports_unreachable():
    def handler(request):
        raise httpx.ConnectError("refused")

    llm = make_llm(handler)
    ok, detail = await llm.health()
    assert ok is False
    assert "не отвечает" in detail
    await llm.aclose()


async def test_health_explains_missing_models():
    llm = make_llm(lambda request: models_response())
    ok, detail = await llm.health()
    assert ok is False
    assert "ollama pull" in detail
    await llm.aclose()


async def test_health_survives_a_non_ollama_answer():
    llm = make_llm(lambda request: httpx.Response(200, text="<html>not json</html>"))
    ok, _ = await llm.health()
    assert ok is False
    await llm.aclose()
