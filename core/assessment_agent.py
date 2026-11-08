"""Bounded, stateless DeepSeek Responses API loop for assessment tools."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from core.assessment_model import AssessmentModelError, _OPENER


API_URL = "https://api.deepseek.com/responses"
_MODELS = {"deepseek-v4-pro", "deepseek-flash"}
_EFFORTS = {"none", "low", "high", "max", "minimal", "medium", "xhigh"}
_TIMEOUT_SECONDS = 120
_MAX_REQUEST_BYTES = 600_000
_MAX_RESPONSE_BYTES = 1_000_000
_MAX_OUTPUT_TOKENS = 12_000
_MAX_ROUNDS = 8
_MAX_CALLS = 24
_MAX_ARGUMENT_BYTES = 32_000
_MAX_TOOL_OUTPUT_BYTES = 160_000
_MAX_SOURCES = 24
_INVALID_RESPONSE = "DeepSeek 返回内容格式无效，请稍后重试。"


def _check_cancelled(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise AssessmentModelError("已停止测验代理。")


def _request_response(api_key: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(body) > _MAX_REQUEST_BYTES:
        raise AssessmentModelError("测验代理上下文过长，请缩小本次题目范围。")
    request = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with _OPENER.open(request, timeout=_TIMEOUT_SECONDS) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        if 300 <= code < 400:
            raise AssessmentModelError("DeepSeek 接口拒绝了重定向请求。") from None
        raise AssessmentModelError("DeepSeek 请求失败，请检查密钥、额度或服务状态。") from None
    except (OSError, http.client.HTTPException):
        raise AssessmentModelError("无法连接 DeepSeek 接口，请稍后重试。") from None
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise AssessmentModelError("DeepSeek 响应过大，已停止测验代理。")
    try:
        response = json.loads(raw)
    except (ValueError, RecursionError):
        raise AssessmentModelError(_INVALID_RESPONSE) from None
    if not isinstance(response, dict):
        raise AssessmentModelError(_INVALID_RESPONSE)
    if response.get("status") == "incomplete":
        raise AssessmentModelError("DeepSeek 未完整输出，请缩小题目范围后重试。")
    if response.get("status") != "completed" or response.get("error") is not None:
        raise AssessmentModelError("DeepSeek 未完成本次测验代理请求，请稍后重试。")
    output = response.get("output")
    if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
        raise AssessmentModelError(_INVALID_RESPONSE)
    return output


def _text_parts(value: Any, kind: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [
        part for part in value
        if isinstance(part, dict) and part.get("type") == kind
        and isinstance(part.get("text"), str)
    ]


def _collect_sources(candidates: Any, sources: dict[str, dict[str, str]]) -> None:
    if not isinstance(candidates, list):
        return
    for source in candidates:
        if len(sources) >= _MAX_SOURCES:
            break
        if not isinstance(source, dict):
            continue
        url = source.get("url")
        if (not isinstance(url, str) or len(url) > 2_048 or url in sources
                or any(ord(char) <= 32 or ord(char) == 127 for char in url)):
            continue
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username is not None:
                continue
        except ValueError:
            continue
        title = source.get("title")
        title = title.strip()[:240] if isinstance(title, str) else ""
        sources[url] = {"title": title or parsed.hostname, "url": url}


def _parse_call(item: dict[str, Any], names: set[str], seen: set[str]) -> tuple[str, str, dict[str, Any]]:
    call_id = item.get("call_id")
    name = item.get("name")
    raw = item.get("arguments")
    if (not isinstance(call_id, str) or not call_id.strip() or len(call_id) > 256
            or call_id in seen or not isinstance(name, str) or name not in names
            or item.get("status", "completed") != "completed"):
        raise AssessmentModelError("DeepSeek 返回了无效或未经授权的测验工具调用。")
    if not isinstance(raw, str) or len(raw.encode("utf-8", errors="surrogatepass")) > _MAX_ARGUMENT_BYTES:
        raise AssessmentModelError("DeepSeek 返回的测验工具参数无效或过长。")
    try:
        arguments = json.loads(raw)
    except (ValueError, RecursionError):
        raise AssessmentModelError("DeepSeek 返回的测验工具参数不是有效 JSON。") from None
    if not isinstance(arguments, dict):
        raise AssessmentModelError("DeepSeek 返回的测验工具参数必须是 JSON 对象。")
    seen.add(call_id)
    return call_id, name, arguments


def run_agent(
    api_key: str,
    messages: list[dict[str, str]],
    *,
    model: str,
    effort: str,
    web_search: bool,
    tools: list[dict[str, Any]],
    execute: Callable[[str, dict[str, Any]], dict[str, Any]],
    cancelled: Callable[[], bool],
) -> dict[str, Any]:
    """Run approved local tools and return final text, provider reasoning and sources."""
    _check_cancelled(cancelled)
    key = api_key.strip() if isinstance(api_key, str) else ""
    if not key or any(not 33 <= ord(char) <= 126 for char in key):
        raise AssessmentModelError("未提供有效的 DeepSeek API 密钥。")
    if model not in _MODELS or effort not in _EFFORTS:
        raise AssessmentModelError("DeepSeek 模型或推理档位无效。")
    if web_search and model != "deepseek-v4-pro":
        raise AssessmentModelError("联网检索需要选择 DeepSeek V4 Pro。")

    names = {tool["name"] for tool in tools if tool.get("type") == "function"}
    if len(names) != len(tools):
        raise AssessmentModelError("测验代理工具配置无效。")
    history: list[dict[str, Any]] = [dict(message) for message in messages]
    payload = {
        "model": model,
        "reasoning": {"effort": effort},
        "max_output_tokens": _MAX_OUTPUT_TOKENS,
        "tools": [*tools, {"type": "web_search"}] if web_search else tools,
        "input": history,
    }
    seen_calls: set[str] = set()
    reasoning: list[str] = []
    sources: dict[str, dict[str, str]] = {}
    for turn in range(_MAX_ROUNDS):
        _check_cancelled(cancelled)
        output = _request_response(key, payload)
        _check_cancelled(cancelled)
        calls = [item for item in output if item.get("type") == "function_call"]
        if calls and (turn == _MAX_ROUNDS - 1 or len(seen_calls) + len(calls) > _MAX_CALLS):
            raise AssessmentModelError("测验代理已达到工具调用上限，请缩小题目范围后重试。")
        pending = [_parse_call(item, names, seen_calls) for item in calls]

        text: list[str] = []
        for item in output:
            kind = item.get("type")
            if kind == "reasoning":
                parts = _text_parts(item.get("content"), "reasoning_text")
                shown = parts or _text_parts(item.get("summary"), "summary_text")
                reasoning.extend(part["text"] for part in shown)
                if parts:
                    history.append({"type": "reasoning", "content": parts})
            elif kind == "message":
                parts = _text_parts(item.get("content"), "output_text")
                text.extend(part["text"] for part in parts)
                if parts:
                    history.append({
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": part["text"]} for part in parts],
                    })
                if web_search:
                    for part in parts:
                        citations = part.get("annotations")
                        if isinstance(citations, list):
                            _collect_sources([
                                entry for entry in citations
                                if isinstance(entry, dict) and entry.get("type") == "url_citation"
                            ], sources)
            elif kind == "function_call":
                history.append({
                    "type": "function_call", "call_id": item["call_id"],
                    "name": item["name"], "arguments": item["arguments"],
                })
            elif kind == "web_search_call" and web_search:
                # The provider uses this item to recover search context on the next call.
                history.append(item)
                action = item.get("action")
                if isinstance(action, dict):
                    _collect_sources(action.get("sources"), sources)

        if not pending:
            final_text = "\n".join(text).strip()
            if not final_text:
                raise AssessmentModelError("DeepSeek 未返回测验代理的最终答复。")
            return {
                "text": final_text,
                "reasoning": "\n".join(reasoning).strip(),
                "sources": list(sources.values()),
            }

        for call_id, name, arguments in pending:
            _check_cancelled(cancelled)
            try:
                result = execute(name, arguments)
                serialized = json.dumps(result, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
            except AssessmentModelError:
                raise
            except Exception:
                raise AssessmentModelError("测验工具执行失败，请检查当前题页后重试。") from None
            _check_cancelled(cancelled)
            if not isinstance(result, dict):
                raise AssessmentModelError("测验工具返回了无效结果。")
            if len(serialized) > _MAX_TOOL_OUTPUT_BYTES:
                raise AssessmentModelError("测验工具返回内容过长，请缩小题目范围后重试。")
            history.append({"type": "function_call_output", "call_id": call_id, "output": serialized})

    raise AssessmentModelError("测验代理已达到调用上限。")
