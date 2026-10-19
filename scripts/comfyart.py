#!/usr/bin/env python3
"""Command-line access to ComfyArt and ComfyUI. Requires only Python 3.10+."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from http.client import HTTPException
import json
import math
import multiprocessing
from pathlib import Path
import socket
import sys
import time
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid


EXIT_CODES = {
    0: "success (an observation may still be running or unknown)",
    1: "unexpected CLI failure",
    2: "invalid command or input; no mutation sent",
    3: "service unavailable or denied",
    4: "tool, JSON-RPC, or graph submission rejected",
    5: "request timeout or wait deadline",
    6: "invalid server response",
    7: "workflow failed or interrupted",
    130: "interrupted; server jobs are not cancelled",
}
COMMANDS = {
    "schema": "Describe CLI commands and output offline",
    "tools list": "List live MCP tools and their input schemas",
    "tools show NAME": "Read one live MCP tool definition",
    "tools call NAME": "Invoke one MCP tool with --args JSON or --args-file FILE",
    "projects list": "List saved projects (not connected browser tabs)",
    "projects get ID": "Read a saved project snapshot",
    "workflows list": "List JSON workflows installed on the target host",
    "workflows show NAME": "Read a named host workflow, including its .json suffix",
    "nodes info [CLASS]": "Read installed ComfyUI node metadata",
    "run --file FILE": "Submit one prepared ComfyUI API graph; FILE may be - for stdin",
    "status PROMPT_ID": "Observe one existing ComfyUI prompt",
    "wait PROMPT_ID": "Observe an existing prompt until terminal or --wait-timeout",
}


class CliError(Exception):
    def __init__(self, kind, message, exit_code, submission="not-sent", **details):
        super().__init__(message)
        self.kind = kind
        self.exit_code = exit_code
        self.submission = submission
        self.details = details

    def output(self):
        return {"kind": self.kind, "message": str(self), "submission": self.submission,
                "details": self.details}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict


@dataclass(frozen=True)
class PromptState:
    prompt_id: str
    state: Literal["queued", "running", "complete", "failed", "interrupted", "unknown"]
    outputs: dict
    media: list[dict]
    messages: list
    status: dict


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise CliError("input", message, 2)


def positive_number(value):
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive finite number") from None
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return number


def parser():
    p = Parser(description=__doc__, allow_abbrev=False)
    p.add_argument("--url", default="http://127.0.0.1:8188", help="ComfyUI base URL; preserves a path prefix")
    p.add_argument("--user", help="Comfy-User header for a host's existing user selection")
    p.add_argument("--project", help="project ID for MCP canvas commands; keep its browser tab open")
    p.add_argument("--timeout", default=210.0, type=positive_number, help="HTTP timeout in seconds (default: 210)")
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("schema", help=COMMANDS["schema"])
    for group, actions in (("tools", ("list", "show", "call")),
                           ("projects", ("list", "get")),
                           ("workflows", ("list", "show")), ("nodes", ("info",))):
        parent = commands.add_parser(group)
        children = parent.add_subparsers(dest="action", required=True)
        for action in actions:
            child = children.add_parser(action)
            if action in ("show", "get", "call"):
                child.add_argument("subject")
            if group == "nodes":
                child.add_argument("subject", nargs="?")
            if action == "call":
                inputs = child.add_mutually_exclusive_group()
                inputs.add_argument("--args", default=None, help="JSON object (default: {})")
                inputs.add_argument("--args-file", help="UTF-8 JSON object file, or - for stdin")
    run = commands.add_parser("run", help=COMMANDS["run --file FILE"])
    run.add_argument("--file", required=True, help="UTF-8 API graph file, or - for stdin")
    for name in ("status", "wait"):
        job = commands.add_parser(name)
        job.add_argument("subject", metavar="PROMPT_ID")
        if name == "wait":
            job.add_argument("--wait-timeout", default=300.0, type=positive_number,
                             help="total observation deadline in seconds (default: 300)")
            job.add_argument("--interval", default=1.5, type=positive_number,
                             help="poll interval in seconds (default: 1.5)")
    return p


def schema():
    return {
        "version": 1,
        "commands": COMMANDS,
        "global_options": {
            "--url": {"default": "http://127.0.0.1:8188", "type": "http(s) base URL"},
            "--user": {"type": "Comfy-User header"},
            "--project": {"type": "MCP project ID"},
            "--timeout": {"default": 210, "type": "positive seconds"},
        },
        "option_position": "Global options precede the command. Command options follow it.",
        "wait_options": {"--wait-timeout": 300, "--interval": 1.5},
        "envelope": {"success": {"ok": True, "command": "command.action", "data": "value"},
                     "failure": {"ok": False, "command": "command.action", "error": {
                         "kind": "string", "message": "string", "submission": "not-sent|rejected|unknown",
                         "details": "object"}}},
        "exit_codes": EXIT_CODES,
        "canvas": "Enable MCP in Agent settings and keep the target project open. Use --project to select it.",
        "tool_result": "content preserves every MCP block; value decodes the first text block when it is JSON.",
        "generation": "Invocation success is not generation success. wait_generation status=done can contain "
                      "error or unknown-id results. Inspect each value.results item.",
        "headless": "run accepts a nonempty API graph object with class_type and inputs per node, not a UI workflow "
                    "or /prompt envelope. status and wait use an existing prompt_id. Unknown is never complete.",
        "transport": "No retries or redirects. A timeout or invalid response after a mutation has an unknown effect. "
                     "Reconcile the canvas or queue before another submission. Wait deadlines do not cancel jobs.",
    }


def finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite JSON number")
    return number


def reject_constant(value):
    raise ValueError(f"nonfinite JSON value {value}")


def decode_json(text):
    try:
        return json.loads(text, parse_float=finite_float, parse_constant=reject_constant)
    except RecursionError:
        raise ValueError("JSON nesting exceeds the supported depth") from None


def input_object(value=None, filename=None):
    try:
        text = (sys.stdin.buffer.read().decode("utf-8-sig") if filename == "-" else
                Path(filename).read_text(encoding="utf-8-sig")) if filename else value
        data = decode_json(text if text is not None else "{}")
    except (OSError, ValueError, UnicodeError) as exc:
        raise CliError("input", f"Cannot read JSON input: {exc}", 2) from None
    if not isinstance(data, dict):
        raise CliError("input", "JSON input must be an object", 2)
    return data


def parse_graph(graph):
    if not graph:
        raise CliError("input", "API graph must contain at least one node", 2)
    for node_id, node in graph.items():
        if (not node_id or not isinstance(node, dict) or not isinstance(node.get("class_type"), str)
                or not node["class_type"].strip() or not isinstance(node.get("inputs"), dict)):
            raise CliError("input", "Each API graph node needs a nonempty class_type and object inputs", 2,
                           node_id=node_id)
    return graph


def segment(value, label):
    decoded = unquote(value)
    if not value or decoded in (".", "..") or any(c in decoded for c in "/\\") or any(ord(c) < 32 for c in decoded):
        raise CliError("input", f"{label} must be a single nonempty path segment", 2)
    return quote(value, safe="")


def base_url(value):
    try:
        parts = urlsplit(value)
        valid = (parts.scheme in ("http", "https") and parts.hostname and not parts.username
                 and not parts.password and "@" not in parts.netloc and not parts.query and not parts.fragment)
        parts.port
        path = unquote(parts.path)
        valid = valid and not any(c.isspace() or ord(c) < 32 for c in value)
        valid = valid and "\\" not in path and not any(p in (".", "..") for p in path.split("/"))
    except ValueError:
        valid = False
    if not valid:
        raise CliError("input", "--url must be an HTTP(S) base URL without credentials, query, fragment, or traversal", 2)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def observation_request(connection, url, timeout, user, route):
    try:
        client = ComfyArt(url, timeout, user)
        connection.send({"data": client.request("GET", route)})
    except CliError as exc:
        connection.send({"error": exc.output(), "exit_code": exc.exit_code})
    finally:
        connection.close()


class ComfyArt:
    def __init__(self, url, timeout, user=None, project=None):
        self.url = base_url(url)
        self.timeout = timeout
        self.project = project
        self.headers = {"Accept": "application/json"}
        if user is not None:
            if not user or any(ord(c) < 32 or ord(c) > 255 for c in user):
                raise CliError("input", "--user must be a nonempty HTTP header value", 2)
            self.headers["Comfy-User"] = user
        self.opener = build_opener(NoRedirect())
        self.submission = "not-sent"

    def request(self, method, route, *, body=None, query=None, mutation=False):
        url = self.url + route + ("?" + urlencode(query) if query else "")
        data = None if body is None else json.dumps(body, allow_nan=False).encode("utf-8")
        headers = {**self.headers, **({"Content-Type": "application/json"} if data is not None else {})}
        req = Request(url, data=data, headers=headers, method=method)
        effect = "unknown" if mutation else "not-sent"
        self.submission = effect
        try:
            try:
                response = self.opener.open(req, timeout=self.timeout)
            except HTTPError as exc:
                response = exc
            with response:
                raw = response.read()
                status = response.status
        except (TimeoutError, socket.timeout):
            raise CliError("timeout", "Request timed out. Inspect the target before submitting again." if mutation else "Request timed out",
                           5, effect) from None
        except HTTPException as exc:
            raise CliError("protocol", f"Invalid HTTP response: {exc}", 6, effect) from None
        except (URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            is_timeout = isinstance(reason, (TimeoutError, socket.timeout))
            raise CliError("timeout" if is_timeout else "service", str(reason), 5 if is_timeout else 3, effect) from None
        if status >= 300:
            try:
                detail = decode_json(raw)
            except (ValueError, UnicodeError):
                detail = raw.decode("utf-8", errors="replace")
            rejected = mutation and status in (400, 401, 403, 404, 405, 413, 415, 422)
            code = 4 if mutation and status == 400 else 3
            message = f"HTTP {status}; redirects are not followed" if 300 <= status < 400 else f"HTTP {status}"
            raise CliError("http", message, code, "rejected" if rejected else effect,
                           http_status=status, response=detail)
        try:
            value = decode_json(raw)
        except (ValueError, UnicodeError):
            raise CliError("protocol", "Server response is not finite JSON", 6, effect) from None
        if not isinstance(value, dict):
            raise CliError("protocol", "Server response must be a JSON object", 6, effect)
        return value

    def observation_request(self, route, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CliError("timeout", "Observation deadline elapsed; the server job is not cancelled", 5)
        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        worker = context.Process(target=observation_request, args=(
            send, self.url, min(self.timeout, remaining), self.headers.get("Comfy-User"), route), daemon=True)
        try:
            worker.start()
            send.close()
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not receive.poll(remaining):
                raise CliError("timeout", "Observation deadline elapsed; the server job is not cancelled", 5)
            result = receive.recv()
            if time.monotonic() >= deadline:
                raise CliError("timeout", "Observation deadline elapsed; the server job is not cancelled", 5)
        except (EOFError, OSError) as exc:
            raise CliError("service", f"Observation worker failed: {exc}", 3) from None
        finally:
            send.close()
            if worker.pid is not None:
                if worker.is_alive():
                    worker.terminate()
                worker.join()
            worker.close()
            receive.close()
        if "error" in result:
            error = result["error"]
            raise CliError(error["kind"], error["message"], result["exit_code"],
                           error["submission"], **error["details"])
        return result["data"]

    def rpc(self, method, params, mutation=False):
        request_id = uuid.uuid4().hex
        result = self.request("POST", "/comfyart/mcp", body={"jsonrpc": "2.0", "id": request_id,
                              "method": method, "params": params},
                              query={"project": self.project} if self.project else None, mutation=mutation)
        effect = "unknown" if mutation else "not-sent"
        if (result.get("jsonrpc") != "2.0" or result.get("id") != request_id
                or (("result" in result) == ("error" in result))):
            raise CliError("protocol", "Invalid or mismatched JSON-RPC response", 6, effect)
        if "error" in result:
            error = result["error"]
            if (not isinstance(error, dict) or type(error.get("code")) is not int
                    or not isinstance(error.get("message"), str)):
                raise CliError("protocol", "Malformed JSON-RPC error", 6, effect)
            rejected = error["code"] in (-32700, -32600, -32601, -32602)
            raise CliError("rpc", "JSON-RPC request failed", 4,
                           "rejected" if mutation and rejected else effect, response=error)
        if not isinstance(result["result"], dict):
            raise CliError("protocol", "JSON-RPC result must be an object", 6, effect)
        return result["result"]

    def tools(self):
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "comfyart-cli", "version": "1"}})
        result = self.rpc("tools/list", {})
        raw = result.get("tools")
        if not isinstance(raw, list):
            raise CliError("protocol", "tools/list response must contain a tools array", 6)
        tools = []
        for item in raw:
            if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                    or not isinstance(item.get("inputSchema"), dict)
                    or not isinstance(item.get("description", ""), str)):
                raise CliError("protocol", "Malformed MCP tool definition", 6)
            tools.append(Tool(item["name"], item.get("description", ""), item["inputSchema"]))
        return tools

    def call(self, name, arguments):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments}, mutation=True)
        content = result.get("content")
        if (not isinstance(content, list) or not all(isinstance(block, dict) for block in content)
                or not isinstance(result.get("isError", False), bool)):
            raise CliError("protocol", "Malformed MCP tool result", 6, "unknown")
        for block in content:
            kind = block.get("type")
            if (not isinstance(kind, str)
                    or (kind == "text" and not isinstance(block.get("text"), str))
                    or (kind in ("image", "audio") and
                        not all(isinstance(block.get(key), str) for key in ("data", "mimeType")))):
                raise CliError("protocol", "Malformed MCP content block", 6, "unknown", result=result)
        value = None
        for block in content:
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                value = block["text"]
                try:
                    value = decode_json(value)
                except ValueError:
                    pass
                break
        data = {**result, "value": value}
        if result.get("isError"):
            raise CliError("tool", "MCP tool reported an error; inspect its content before retrying", 4, "unknown",
                           tool=name, result=data)
        return data

    def submit(self, graph):
        result = self.request("POST", "/api/prompt", body={"prompt": graph}, mutation=True)
        if isinstance(result.get("prompt_id"), str) and result["prompt_id"]:
            return {**result, "state": "submitted"}
        if result.get("error"):
            raise CliError("submission", "ComfyUI rejected the graph", 4, "rejected", response=result)
        raise CliError("protocol", "Submission response has no prompt_id; inspect the queue before retrying", 6, "unknown",
                       response=result)

    def observe(self, prompt_id, deadline=None):
        encoded_id = segment(prompt_id, "PROMPT_ID")
        route = "/api/history/" + encoded_id
        history = (self.request("GET", route) if deadline is None else
                   self.observation_request(route, deadline))
        if prompt_id in history:
            entry = history[prompt_id]
            if (not isinstance(entry, dict) or not isinstance(entry.get("outputs", {}), dict)
                    or not isinstance(entry.get("status", {}), dict)):
                raise CliError("protocol", "Malformed prompt history entry", 6)
            status = entry.get("status", {})
            messages = status.get("messages", [])
            if not isinstance(messages, list):
                raise CliError("protocol", "History messages must be an array", 6)
            events = {m[0] for m in messages if isinstance(m, list) and m and isinstance(m[0], str)}
            if "execution_interrupted" in events:
                state = "interrupted"
            elif status.get("status_str") == "error" or "execution_error" in events:
                state = "failed"
            elif status.get("completed") is True or "execution_success" in events:
                state = "complete"
            else:
                state = "unknown"
            outputs = entry.get("outputs", {})
            return PromptState(prompt_id, state, outputs, self.media(outputs), messages, status)
        queue = (self.request("GET", "/api/queue") if deadline is None else
                 self.observation_request("/api/queue", deadline))
        for key, state in (("queue_running", "running"), ("queue_pending", "queued")):
            items = queue.get(key)
            if not isinstance(items, list):
                raise CliError("protocol", "Queue response must contain queue_running and queue_pending arrays", 6)
            if any(isinstance(item, list) and len(item) > 1 and item[1] == prompt_id for item in items):
                return PromptState(prompt_id, state, {}, [], [], {})
        return PromptState(prompt_id, "unknown", {}, [], [], {})

    def media(self, outputs):
        refs = []

        def visit(value, node_id):
            if isinstance(value, dict):
                if isinstance(value.get("filename"), str):
                    query = {key: value[key] for key in ("filename", "subfolder", "type")
                             if isinstance(value.get(key), str)}
                    refs.append({"node_id": node_id, **value, "url": self.url + "/api/view?" + urlencode(query)})
                for child in value.values():
                    visit(child, node_id)
            elif isinstance(value, list):
                for child in value:
                    visit(child, node_id)

        for node_id, value in outputs.items():
            visit(value, node_id)
        return refs

    def wait(self, prompt_id, timeout, interval):
        deadline = time.monotonic() + timeout
        last = None
        while True:
            try:
                last = self.observe(prompt_id, deadline)
            except CliError as exc:
                if exc.exit_code == 5:
                    exc.details.update(prompt_id=prompt_id, last_state=asdict(last) if last else None)
                raise
            if last.state in ("complete", "failed", "interrupted"):
                return last
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CliError("timeout", "Observation deadline elapsed; the server job is not cancelled", 5,
                               prompt_id=prompt_id, last_state=asdict(last))
            time.sleep(min(interval, remaining))


def execute(args, client):
    if args.command == "tools":
        arguments = input_object(args.args, args.args_file) if args.action == "call" else None
        tools = client.tools()
        if args.action == "list":
            return {"tools": [asdict(tool) for tool in tools]}
        tool = next((t for t in tools if t.name == args.subject), None)
        if tool is None:
            raise CliError("input", "Unknown tool; use tools list on this host", 2, tool=args.subject)
        return asdict(tool) if args.action == "show" else client.call(tool.name, arguments)
    if args.command in ("projects", "workflows", "nodes"):
        route = "/api/object_info" if args.command == "nodes" else "/comfyart/" + args.command
        if getattr(args, "subject", None) is not None:
            name = args.subject
            encoded = segment(name, "Name")
            if args.command == "workflows" and not name.endswith(".json"):
                raise CliError("input", "Workflow name must include its .json suffix", 2)
            route += "/" + encoded
        return client.request("GET", route)
    if args.command == "run":
        return client.submit(parse_graph(input_object(filename=args.file)))
    state = (client.observe(args.subject) if args.command == "status" else
             client.wait(args.subject, args.wait_timeout, args.interval))
    data = asdict(state)
    if state.state in ("failed", "interrupted"):
        raise CliError("execution", f"Workflow {state.state}", 7, prompt=data)
    return data


def main(argv=None):
    command = "parse"
    client = None
    args = None
    try:
        args = parser().parse_args(argv)
        command = args.command + ("." + args.action if hasattr(args, "action") else "")
        if args.command == "schema":
            data = schema()
        else:
            client = ComfyArt(args.url, args.timeout, args.user, args.project)
            data = execute(args, client)
        output = {"ok": True, "command": command, "data": data}
        code = 0
    except CliError as exc:
        output = {"ok": False, "command": command, "error": exc.output()}
        code = exc.exit_code
    except KeyboardInterrupt:
        error = CliError("interrupted", "Interrupted; server operations are not cancelled", 130,
                         client.submission if client else "not-sent")
        if args is not None and args.command in ("status", "wait"):
            error.details["prompt_id"] = args.subject
        output = {"ok": False, "command": command, "error": error.output()}
        code = 130
    except Exception as exc:
        error = CliError("internal", f"{type(exc).__name__}: {exc}", 1,
                         client.submission if client else "not-sent")
        output = {"ok": False, "command": command, "error": error.output()}
        code = 1
    print(json.dumps(output, ensure_ascii=True, allow_nan=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
