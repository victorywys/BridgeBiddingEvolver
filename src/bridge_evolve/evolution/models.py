"""Pinned local model access and durable, conservative request/token accounting."""

import json
import logging
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from bridge_evolve.automation import GATEWAY, _gateway_key
from bridge_evolve.evolution.config import DEFAULT_MODEL
from bridge_evolve.evolution.execution import EvolutionStopped
from bridge_evolve.evolution.feedback import json_bytes
from bridge_evolve.evolution.costs import budget_usage, move_request_to_maintenance, receipt_usage
from bridge_evolve.evaluation.experiment import timestamp

LOGGER = logging.getLogger(__name__)


class ModelRequestError(RuntimeError):
    pass


class MaintenanceBudgetExhausted(EvolutionStopped):
    pass


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


def request_body(config, messages, tools, output_tokens=None, *, tool_choice=None):
    payload = {"model": config["model"], "store": False, "stream": False, "input": messages, "tools": tools,
               "max_output_tokens": output_tokens or config["max_output_tokens"],
               "reasoning": {"effort": config["reasoning_effort"]}}
    if tools:
        payload["parallel_tool_calls"] = False
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    return json_bytes(payload)


def token_reservation(body, output_tokens):
    return len(body) + output_tokens + 4096


class ModelClient:
    def __init__(self, config: dict, state: dict, checkpoint, stop, request_timeout=None, *, budget="research", reserve=None):
        self.config, self.state, self.checkpoint, self.stop = config, state, checkpoint, stop
        self.key = None
        self.request_timeout = request_timeout
        self.budget = budget
        self.reserve = reserve

    def request(self, messages: list, tools: list, *, label: str, output_tokens=None, reserved_request=None, budget=None, tool_choice=None) -> dict:
        original_activity = self.state.get("activity")
        for attempt in range(self.config.get("model_retries", 0) + 1):
            try:
                result = self._request(messages, tools, label=label, output_tokens=output_tokens,
                                       reserved_request=reserved_request if attempt == 0 else None, budget=budget, tool_choice=tool_choice)
            except RetryableModelError:
                if self.config.get("maintenance_budget"):
                    move_request_to_maintenance(self, self.state["agent_requests"], "Transient model gateway failure.")
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

    def _request(self, messages: list, tools: list, *, label: str, output_tokens=None, reserved_request=None, budget=None, tool_choice=None) -> dict:
        self.stop()
        config, state = self.config, self.state
        pool = budget or self.budget
        if pool not in ("research", "maintenance"):
            raise ValueError("Unknown model budget pool.")
        maintenance = config.get("maintenance_budget")
        separate = maintenance is not None
        if not separate:
            pool = "research"
        used = budget_usage(state, pool) if separate else {
            "requests": state["agent_requests"], "charged_tokens": state.get("model_usage", {}).get("charged_tokens", 0)}
        limits = ({"requests": maintenance["max_requests"], "tokens": maintenance["max_tokens"]}
                  if pool == "maintenance" else {"requests": config["max_agent_requests"], "tokens": config["max_agent_tokens"]})
        exhausted = MaintenanceBudgetExhausted if pool == "maintenance" else EvolutionStopped
        if pool == "maintenance" and state.get("maintenance_seconds", 0) >= maintenance["max_seconds"]:
            raise exhausted("The separate maintenance time budget is exhausted.")
        reserve = self.reserve() if self.reserve else {}
        if reserve.get("budget", "research") != pool:
            reserve = {}
        timeout = min(config["agent_timeout"], self.request_timeout()) if self.request_timeout else config["agent_timeout"]
        timeout -= reserve.get("seconds", 0)
        if timeout <= 0:
            raise exhausted(f"No unreserved {pool} time remains for a model request.")
        if reserved_request is not None and (
                type(reserved_request) is not int or reserved_request != state["agent_requests"] or reserved_request < 1
                or any(row["request"] == reserved_request for row in state.get("model_requests", []))):
            raise ValueError("Invalid or already-used model request reservation.")
        if used["requests"] + (reserved_request is None) + reserve.get("requests", 0) > limits["requests"]:
            raise exhausted("Model request budget cannot cover the next request and delivery closure." if reserve else
                            "Model request budget exhausted; no model fallback was used.")
        maximum_output = output_tokens or config["max_output_tokens"]
        body = request_body(config, messages, tools, maximum_output, tool_choice=tool_choice)
        if len(body) > config["max_context_bytes"]:
            raise ValueError("Research context exceeds max_context_bytes; use smaller paged reads or a research handoff.")
        reservation = token_reservation(body, maximum_output)
        usage = state.setdefault("model_usage", {"input_tokens": 0, "output_tokens": 0, "charged_tokens": 0,
                                                "unreported_requests": 0, "by_model": {}})
        if used["charged_tokens"] + reservation + reserve.get("tokens", 0) > limits["tokens"]:
            raise exhausted("Model token budget cannot cover the next request plus its delivery-closeout reserve." if reserve else
                            "Model token budget cannot cover the next conservatively reserved request.")
        if self.key is None:
            self.key = _gateway_key(Path(config["project"]), config["model"])
        if reserved_request is None:
            state["agent_requests"] += 1
        request_number = state["agent_requests"]
        row = {"request": request_number, "label": label, "model": config["model"], "status": "reserved", "budget": pool,
               "reserved_tokens": reservation, "max_output_tokens": maximum_output,
               "started_at": timestamp()}
        if tool_choice is not None:
            row["tool_choice"] = tool_choice
        state.setdefault("model_requests", []).append(row)
        usage["charged_tokens"] += reservation
        usage["unreported_requests"] += 1
        previous_activity = state.get("activity")
        state["activity"] = {"kind": "model", "request": request_number, "label": label, "model": config["model"],
                             "started_at": row["started_at"]}
        self.checkpoint()
        started = time.monotonic()
        request = Request(GATEWAY + "/responses", data=body,
                          headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"})
        try:
            with build_opener(ProxyHandler({})).open(request, timeout=timeout) as response:
                contents = response.read(8 * 1024 * 1024 + 1)
            if len(contents) > 8 * 1024 * 1024:
                raise ValueError("Model response exceeds the transport limit.")
            result = json.loads(contents)
            if (not isinstance(result, dict) or not isinstance(result.get("output"), list)
                    or any(not isinstance(item, dict) or not isinstance(item.get("type"), str) for item in result["output"])):
                raise ValueError("Model returned an invalid Responses envelope.")
            row.update(status="received", returned_model=result.get("model"), finished_at=timestamp())
            row.update(receipt_usage(result))
            if row.get("receipt_error"):
                LOGGER.warning("Request %s: %s", request_number, row["receipt_error"])
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
                if separate:
                    move_request_to_maintenance(self, request_number, "Gateway substituted a different frozen model.")
                self.checkpoint()
                raise ModelRequestError("The gateway returned a different model than the frozen choice; the response was not used.")
            return result
        except HTTPError as error:
            code = error.code
            error.close()
            row.update(status="error", error=f"HTTP {code}; token reservation retained because usage is unknown.", finished_at=timestamp())
            if separate:
                move_request_to_maintenance(self, request_number, f"Model gateway HTTP {code}.")
            state["activity"] = {"kind": "model_error", "request": request_number, "error": row["error"], "started_at": timestamp()}
            self.checkpoint()
            error_type = RetryableModelError if code in (429, 502, 503, 504) else ModelRequestError
            raise error_type(f"The configured model returned HTTP {code}; no fallback was used.") from None
        except (URLError, TimeoutError, ValueError, OSError) as error:
            row.update(status="error", error=f"{type(error).__name__}; unreported token reservation retained.", finished_at=timestamp())
            if separate:
                move_request_to_maintenance(self, request_number, "Model transport or response envelope failed.")
            state["activity"] = {"kind": "model_error", "request": request_number, "error": row["error"], "started_at": timestamp()}
            self.checkpoint()
            raise ModelRequestError("The configured model request failed; its request budget remains spent.") from error
        finally:
            if pool == "maintenance":
                state["maintenance_seconds"] = state.get("maintenance_seconds", 0) + time.monotonic() - started
                self.checkpoint()


def response_text(response: dict) -> str:
    return "\n".join(content["text"] for item in response["output"] if item.get("type") == "message"
                     for content in item.get("content", [])
                     if content.get("type") == "output_text" and isinstance(content.get("text"), str))


class RetryableModelError(ModelRequestError):
    pass
