"""Pinned local model access and durable, conservative request/token accounting."""

import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from bridge_evolve.automation import GATEWAY, _gateway_key
from bridge_evolve.evolution.config import DEFAULT_MODEL
from bridge_evolve.evolution.execution import EvolutionStopped
from bridge_evolve.evaluation.experiment import timestamp


def available_models(project: Path) -> list[dict]:
    key = _gateway_key(project, DEFAULT_MODEL)
    request = Request(GATEWAY + "/models", headers={"Authorization": "Bearer " + key})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=30) as response:
            data = json.load(response)["data"]
    except HTTPError as error:
        code = error.code
        error.close()
        raise RuntimeError(f"Local model discovery returned HTTP {code}.") from None
    except (URLError, TimeoutError) as error:
        raise RuntimeError("The authenticated local model gateway is unavailable.") from error
    return [{"id": item["id"], "name": item.get("name", item["id"]),
             "capabilities": item.get("capabilities", {})}
            for item in data]


class ModelClient:
    def __init__(self, config: dict, state: dict, checkpoint, stop):
        self.config, self.state, self.checkpoint, self.stop = config, state, checkpoint, stop
        self.key = None

    def request(self, messages: list, tools: list, *, label: str, output_tokens=None, reserved_request=None) -> dict:
        original_activity = self.state.get("activity")
        for attempt in range(self.config.get("model_retries", 0) + 1):
            try:
                result = self._request(messages, tools, label=label, output_tokens=output_tokens,
                                       reserved_request=reserved_request if attempt == 0 else None)
            except RetryableModelError:
                if attempt >= self.config.get("model_retries", 0):
                    raise
                delay = self.config.get("model_retry_delay", 5) * 2**attempt
                event = {"kind": "model_retry", "request": self.state["agent_requests"], "attempt": attempt + 1,
                         "delay": delay, "model": self.config["model"], "started_at": timestamp(),
                         "summary": "模型网关暂时不可用；保留失败请求额度，按固定模型有限重试。"}
                self.state["activity"] = event
                self.state.setdefault("milestones", {})["model-retry:" + str(self.state["agent_requests"])] = {
                    **event, "id": "model-retry:" + str(self.state["agent_requests"]), "timestamp": event["started_at"]}
                self.checkpoint()
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    self.stop()
                    time.sleep(min(.2, max(0, deadline - time.monotonic())))
            else:
                if attempt:
                    self.state["activity"] = original_activity or {"kind": "research", "started_at": timestamp()}
                    self.checkpoint()
                return result
        raise RuntimeError("The configured model retry loop did not produce an outcome.")

    def _request(self, messages: list, tools: list, *, label: str, output_tokens=None, reserved_request=None) -> dict:
        self.stop()
        config, state = self.config, self.state
        if reserved_request is not None and (
                type(reserved_request) is not int or reserved_request != state["agent_requests"] or reserved_request < 1
                or any(row["request"] == reserved_request for row in state.get("model_requests", []))):
            raise ValueError("Invalid or already-used model request reservation.")
        if state["agent_requests"] + (reserved_request is None) > config["max_agent_requests"]:
            raise EvolutionStopped("Model request budget exhausted; no model fallback was used.")
        payload = {"model": config["model"], "store": False, "stream": False, "input": messages, "tools": tools,
                   "max_output_tokens": output_tokens or config["max_output_tokens"],
                   "reasoning": {"effort": config["reasoning_effort"]}}
        if tools:
            payload["parallel_tool_calls"] = False
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
        if len(body) > config["max_context_bytes"]:
            raise ValueError("Research context exceeds max_context_bytes; use smaller paged reads or a research handoff.")
        reservation = len(body) + payload["max_output_tokens"] + 4096
        usage = state.setdefault("model_usage", {"input_tokens": 0, "output_tokens": 0, "charged_tokens": 0,
                                                "unreported_requests": 0, "by_model": {}})
        if usage["charged_tokens"] + reservation > config["max_agent_tokens"]:
            raise EvolutionStopped("Model token budget cannot cover the next conservatively reserved request.")
        if self.key is None:
            self.key = _gateway_key(Path(config["project"]), config["model"])
        if reserved_request is None:
            state["agent_requests"] += 1
        request_number = state["agent_requests"]
        row = {"request": request_number, "label": label, "model": config["model"], "status": "reserved",
               "reserved_tokens": reservation, "max_output_tokens": payload["max_output_tokens"],
               "started_at": timestamp()}
        state.setdefault("model_requests", []).append(row)
        usage["charged_tokens"] += reservation
        usage["unreported_requests"] += 1
        previous_activity = state.get("activity")
        state["activity"] = {"kind": "model", "request": request_number, "label": label, "model": config["model"],
                             "started_at": row["started_at"]}
        self.checkpoint()
        request = Request(GATEWAY + "/responses", data=body,
                          headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"})
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=config["agent_timeout"]) as response:
                contents = response.read(8 * 1024 * 1024 + 1)
            if len(contents) > 8 * 1024 * 1024:
                raise ValueError("Model response exceeds the transport limit.")
            result = json.loads(contents)
            if (not isinstance(result, dict) or not isinstance(result.get("output"), list)
                    or any(not isinstance(item, dict) or not isinstance(item.get("type"), str) for item in result["output"])):
                raise ValueError("Model returned an invalid Responses envelope.")
            row.update(status="received", returned_model=result.get("model"), finished_at=timestamp())
            reported = result.get("usage")
            if (isinstance(reported, dict) and all(type(reported.get(key)) is int and reported[key] >= 0
                                                  for key in ("input_tokens", "output_tokens"))):
                actual = reported["input_tokens"] + reported["output_tokens"]
                usage["charged_tokens"] += actual - reservation
                usage["unreported_requests"] -= 1
                for key in ("input_tokens", "output_tokens"):
                    usage[key] += reported[key]
                model_usage = usage["by_model"].setdefault(config["model"], {"requests": 0, "input_tokens": 0, "output_tokens": 0})
                model_usage["requests"] += 1
                for key in ("input_tokens", "output_tokens"):
                    model_usage[key] += reported[key]
                row["usage"] = {key: reported[key] for key in ("input_tokens", "output_tokens")}
            else:
                row["usage_unreported"] = True
            state["activity"] = previous_activity or {"kind": "research", "started_at": timestamp()}
            self.checkpoint()
            if result.get("model") != config["model"]:
                row.update(status="model_mismatch")
                self.checkpoint()
                raise RuntimeError("The gateway returned a different model than the frozen choice; the response was not used.")
            return result
        except HTTPError as error:
            code = error.code
            error.close()
            row.update(status="error", error=f"HTTP {code}; token reservation retained because usage is unknown.", finished_at=timestamp())
            state["activity"] = {"kind": "model_error", "request": request_number, "error": row["error"], "started_at": timestamp()}
            self.checkpoint()
            error_type = RetryableModelError if code in (429, 502, 503, 504) else RuntimeError
            raise error_type(f"The configured model returned HTTP {code}; no fallback was used.") from None
        except (URLError, TimeoutError, ValueError, OSError) as error:
            row.update(status="error", error=f"{type(error).__name__}; unreported token reservation retained.", finished_at=timestamp())
            state["activity"] = {"kind": "model_error", "request": request_number, "error": row["error"], "started_at": timestamp()}
            self.checkpoint()
            raise RuntimeError("The configured model request failed; its request budget remains spent.") from error


def response_text(response: dict) -> str:
    return "\n".join(content["text"] for item in response["output"] if item.get("type") == "message"
                     for content in item.get("content", [])
                     if content.get("type") == "output_text" and isinstance(content.get("text"), str))


class RetryableModelError(RuntimeError):
    pass
