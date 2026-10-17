"""Minimal LLM provider clients (pure Python + requests, no SDKs).

Embeddings: ``embed(...)`` (OpenAI-compatible ``/embeddings`` and Gemini
``batchEmbedContents``) for semantic search.

Chat entry point::

    chat(provider_type, base_url, api_key, model, system, messages,
         temperature, max_tokens, timeout) -> dict(text, input_tokens, output_tokens, model)

``messages`` is ``[{"role": "user"|"assistant", "content": str}, ...]``.

Supported ``provider_type``:
  openai     OpenAI Chat Completions (``max_completion_tokens``)
  local      OpenAI-compatible server (Ollama, vLLM, llama.cpp, LM Studio) - ``max_tokens``
  custom     OpenAI-compatible endpoint at ``base_url`` - ``max_tokens``
  anthropic  Anthropic Messages API
  google     Gemini generateContent

Errors raise ``LLMError`` with a short, secret-free message.
"""
from __future__ import annotations

import os

import requests

DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
    "google": "https://generativelanguage.googleapis.com",
    "local": "http://127.0.0.1:11434/v1",
}
ANTHROPIC_VERSION = "2023-06-01"


class LLMError(Exception):
    pass


def resolve_secret(value: str | None) -> str | None:
    """``env:NAME`` reads the key from the server environment (keeps it out of the DB)."""
    if value and value.startswith("env:"):
        name = value[4:].strip()
        secret = os.environ.get(name)
        if not secret:
            raise LLMError(f"Environment variable {name} is not set on the Odoo server.")
        return secret
    return value


def _post(url, headers, payload, timeout):
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.Timeout as exc:
        raise LLMError(f"Provider timed out after {timeout}s.") from exc
    except requests.RequestException as exc:
        raise LLMError(f"Provider unreachable: {type(exc).__name__}.") from exc
    if response.status_code >= 400:
        detail = ""
        try:
            body = response.json()
            err = body.get("error") if isinstance(body, dict) else None
            detail = (err.get("message") if isinstance(err, dict) else err) or body.get("message") or ""
        except ValueError:
            detail = response.text[:300]
        raise LLMError(f"Provider HTTP {response.status_code}: {str(detail)[:300]}")
    try:
        return response.json()
    except ValueError as exc:
        raise LLMError("Provider returned a non-JSON response.") from exc


def _openai_like(base_url, api_key, model, system, messages, temperature, max_tokens, timeout, official):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {"model": model, "messages": [{"role": "system", "content": system}] + messages}
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens:
        # OpenAI deprecated max_tokens for newer models; compatible servers still expect it
        payload["max_completion_tokens" if official else "max_tokens"] = max_tokens
    data = _post(base_url.rstrip("/") + "/chat/completions", headers, payload, timeout)
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError("Unexpected response shape (chat completions).") from exc
    usage = data.get("usage") or {}
    return {
        "text": text,
        "input_tokens": int(usage.get("prompt_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or 0),
        "model": data.get("model") or model,
    }


def _anthropic(base_url, api_key, model, system, messages, temperature, max_tokens, timeout):
    if not api_key:
        raise LLMError("Anthropic API key is missing.")
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
    }
    payload = {"model": model, "max_tokens": max_tokens or 1024, "system": system, "messages": messages}
    if temperature is not None:
        payload["temperature"] = temperature
    data = _post(base_url.rstrip("/") + "/v1/messages", headers, payload, timeout)
    try:
        text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
    except (KeyError, TypeError) as exc:
        raise LLMError("Unexpected response shape (messages).") from exc
    usage = data.get("usage") or {}
    return {
        "text": text,
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "model": data.get("model") or model,
    }


def _google(base_url, api_key, model, system, messages, temperature, max_tokens, timeout):
    if not api_key:
        raise LLMError("Gemini API key is missing.")
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [
            {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
            for m in messages
        ],
        "generationConfig": {},
    }
    if temperature is not None:
        payload["generationConfig"]["temperature"] = temperature
    if max_tokens:
        payload["generationConfig"]["maxOutputTokens"] = max_tokens
    url = f"{base_url.rstrip('/')}/v1beta/models/{model}:generateContent"
    data = _post(url, headers, payload, timeout)
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, TypeError) as exc:
        reason = (data.get("promptFeedback") or {}).get("blockReason") if isinstance(data, dict) else None
        raise LLMError(f"No answer from Gemini{f' (blocked: {reason})' if reason else ''}.") from exc
    usage = data.get("usageMetadata") or {}
    return {
        "text": text,
        "input_tokens": int(usage.get("promptTokenCount") or 0),
        "output_tokens": int(usage.get("candidatesTokenCount") or 0),
        "model": data.get("modelVersion") or model,
    }


# ---------------------------------------------------------------- streaming
def _sse_events(response):
    """Yield parsed JSON payloads of a Server-Sent Events response ("data: {...}")."""
    import json

    for raw in response.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data:"):
            continue
        data = raw[5:].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except ValueError:
            continue


def _open_stream(url, headers, payload, timeout):
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=timeout, stream=True)
    except requests.Timeout as exc:
        raise LLMError(f"Provider timed out after {timeout}s.") from exc
    except requests.RequestException as exc:
        raise LLMError(f"Provider unreachable: {type(exc).__name__}.") from exc
    if response.status_code >= 400:
        detail = ""
        try:
            body = response.json()
            err = body.get("error") if isinstance(body, dict) else None
            detail = (err.get("message") if isinstance(err, dict) else err) or body.get("message") or ""
        except ValueError:
            detail = (response.text or "")[:300]
        raise LLMError(f"Provider HTTP {response.status_code}: {str(detail)[:300]}")
    return response


def chat_stream(provider_type, base_url, api_key, model, system, messages,
                temperature=0.2, max_tokens=1024, timeout=60):
    """Like ``chat`` but yields ``("delta", text)`` while the model writes, then one
    ``("done", dict(text, input_tokens, output_tokens, model))``. Raises LLMError."""
    if not model:
        raise LLMError("No model name configured.")
    base = base_url or DEFAULT_BASE_URLS.get(provider_type)
    if not base:
        raise LLMError("No API base URL configured for this provider.")
    key = resolve_secret(api_key)
    parts, usage, model_out = [], {"input_tokens": 0, "output_tokens": 0}, model
    response = None
    try:
        if provider_type in ("openai", "local", "custom"):
            headers = {"Content-Type": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            payload = {"model": model, "messages": [{"role": "system", "content": system}] + messages, "stream": True}
            if temperature is not None:
                payload["temperature"] = temperature
            if max_tokens:
                payload["max_completion_tokens" if provider_type == "openai" else "max_tokens"] = max_tokens
            if provider_type == "openai":
                payload["stream_options"] = {"include_usage": True}
            response = _open_stream(base.rstrip("/") + "/chat/completions", headers, payload, timeout)
            for event in _sse_events(response):
                model_out = event.get("model") or model_out
                if event.get("usage"):
                    usage = {"input_tokens": int(event["usage"].get("prompt_tokens") or 0),
                             "output_tokens": int(event["usage"].get("completion_tokens") or 0)}
                for choice in event.get("choices") or []:
                    text = (choice.get("delta") or {}).get("content")
                    if text:
                        parts.append(text)
                        yield ("delta", text)
        elif provider_type == "anthropic":
            if not key:
                raise LLMError("Anthropic API key is missing.")
            headers = {"Content-Type": "application/json", "x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
            payload = {"model": model, "max_tokens": max_tokens or 1024, "system": system,
                       "messages": messages, "stream": True}
            if temperature is not None:
                payload["temperature"] = temperature
            response = _open_stream(base.rstrip("/") + "/v1/messages", headers, payload, timeout)
            for event in _sse_events(response):
                kind = event.get("type")
                if kind == "message_start":
                    msg = event.get("message") or {}
                    model_out = msg.get("model") or model_out
                    usage["input_tokens"] = int((msg.get("usage") or {}).get("input_tokens") or 0)
                elif kind == "content_block_delta":
                    text = (event.get("delta") or {}).get("text")
                    if text:
                        parts.append(text)
                        yield ("delta", text)
                elif kind == "message_delta":
                    usage["output_tokens"] = int((event.get("usage") or {}).get("output_tokens") or 0)
                elif kind == "error":
                    raise LLMError(f"Provider error: {str((event.get('error') or {}).get('message'))[:300]}")
        elif provider_type == "google":
            if not key:
                raise LLMError("Gemini API key is missing.")
            headers = {"Content-Type": "application/json", "x-goog-api-key": key}
            payload = {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                             for m in messages],
                "generationConfig": {},
            }
            if temperature is not None:
                payload["generationConfig"]["temperature"] = temperature
            if max_tokens:
                payload["generationConfig"]["maxOutputTokens"] = max_tokens
            url = f"{base.rstrip('/')}/v1beta/models/{model}:streamGenerateContent?alt=sse"
            response = _open_stream(url, headers, payload, timeout)
            for event in _sse_events(response):
                for cand in event.get("candidates") or []:
                    for part in (cand.get("content") or {}).get("parts") or []:
                        if part.get("text"):
                            parts.append(part["text"])
                            yield ("delta", part["text"])
                meta = event.get("usageMetadata")
                if meta:
                    usage = {"input_tokens": int(meta.get("promptTokenCount") or 0),
                             "output_tokens": int(meta.get("candidatesTokenCount") or 0)}
                model_out = event.get("modelVersion") or model_out
        else:
            raise LLMError(f"Unsupported provider type: {provider_type}")
    except requests.RequestException as exc:  # connection dropped mid-stream
        raise LLMError(f"Provider stream interrupted: {type(exc).__name__}.") from exc
    finally:
        if response is not None:
            response.close()  # stop the upstream generation when our reader goes away
    yield ("done", {"text": "".join(parts), "model": model_out, **usage})


EMBED_BATCH = 64


def _unit(vec):
    norm = sum(x * x for x in vec) ** 0.5
    if not norm:
        raise LLMError("Provider returned a zero embedding vector.")
    return [x / norm for x in vec]


def embed(provider_type, base_url, api_key, model, texts, kind="document", dimensions=None, timeout=60):
    """Embed ``texts`` -> dict(vectors=[[float]], tokens=int).

    Vectors are L2-normalised, so a dot product is the cosine similarity.
    ``kind`` is "document" or "query" (Gemini optimises each differently).
    Claude has no embeddings API: pair it with another provider for search.
    """
    if not model:
        raise LLMError("No embedding model configured.")
    if provider_type == "anthropic":
        raise LLMError("Claude has no embeddings API; choose another provider for semantic search.")
    base = base_url or DEFAULT_BASE_URLS.get(provider_type)
    if not base:
        raise LLMError("No API base URL configured for this provider.")
    key = resolve_secret(api_key)
    vectors, tokens = [], 0
    for i in range(0, len(texts), EMBED_BATCH):
        batch = [t or " " for t in texts[i:i + EMBED_BATCH]]
        if provider_type in ("openai", "local", "custom"):
            headers = {"Content-Type": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            payload = {"model": model, "input": batch}
            if dimensions:
                payload["dimensions"] = int(dimensions)
            data = _post(base.rstrip("/") + "/embeddings", headers, payload, timeout)
            try:
                rows = sorted(data["data"], key=lambda r: r.get("index", 0))
                got = [r["embedding"] for r in rows]
            except (KeyError, TypeError) as exc:
                raise LLMError("Unexpected response shape (embeddings).") from exc
            tokens += int((data.get("usage") or {}).get("prompt_tokens") or 0)
        elif provider_type == "google":
            if not key:
                raise LLMError("Gemini API key is missing.")
            headers = {"Content-Type": "application/json", "x-goog-api-key": key}
            task = "RETRIEVAL_QUERY" if kind == "query" else "RETRIEVAL_DOCUMENT"
            reqs = []
            for text in batch:
                req = {"model": f"models/{model}", "content": {"parts": [{"text": text}]}, "taskType": task}
                if dimensions:
                    req["outputDimensionality"] = int(dimensions)
                reqs.append(req)
            url = f"{base.rstrip('/')}/v1beta/models/{model}:batchEmbedContents"
            data = _post(url, headers, {"requests": reqs}, timeout)
            try:
                got = [e["values"] for e in data["embeddings"]]
            except (KeyError, TypeError) as exc:
                raise LLMError("Unexpected response shape (batchEmbedContents).") from exc
        else:
            raise LLMError(f"Unsupported provider type: {provider_type}")
        if len(got) != len(batch):
            raise LLMError("Provider returned a different number of embeddings than requested.")
        vectors.extend(_unit([float(x) for x in v]) for v in got)
    dims = {len(v) for v in vectors}
    if len(dims) > 1:
        raise LLMError("Provider returned embeddings of different sizes.")
    return {"vectors": vectors, "tokens": tokens}


def chat(provider_type, base_url, api_key, model, system, messages,
         temperature=0.2, max_tokens=1024, timeout=60):
    if not model:
        raise LLMError("No model name configured.")
    base = base_url or DEFAULT_BASE_URLS.get(provider_type)
    if not base:
        raise LLMError("No API base URL configured for this provider.")
    key = resolve_secret(api_key)
    if provider_type in ("openai", "local", "custom"):
        return _openai_like(base, key, model, system, messages, temperature, max_tokens, timeout,
                            official=provider_type == "openai")
    if provider_type == "anthropic":
        return _anthropic(base, key, model, system, messages, temperature, max_tokens, timeout)
    if provider_type == "google":
        return _google(base, key, model, system, messages, temperature, max_tokens, timeout)
    raise LLMError(f"Unsupported provider type: {provider_type}")
