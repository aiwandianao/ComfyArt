from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest


CLI = Path(__file__).resolve().parents[1] / "scripts" / "comfyart.py"
GRAPH = {"1": {"class_type": "SaveImage", "inputs": {"images": ["2", 0]}}}
TOOLS = [{"name": "add_text", "description": "Add text", "inputSchema": {"type": "object"}},
         {"name": "wait_generation", "description": "Observe", "inputSchema": {"type": "object"}}]


def invoke(*args, stdin=None, wrapper=None):
    prefix = [sys.executable, str(CLI)] if wrapper is None else [sys.executable, "-c", wrapper, str(CLI)]
    proc = subprocess.run([*prefix, *args], input=stdin.encode("utf-8") if stdin is not None else None,
                          capture_output=True, timeout=10)
    assert proc.stderr == b"", proc.stderr.decode(errors="replace")
    return proc.returncode, json.loads(proc.stdout.decode("ascii"))


@contextmanager
def host(reply, drip_delay=None, drip_headers=False, finished=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_request(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.loads(raw) if raw else None
            requests.append((self.command, self.path, body, dict(self.headers)))
            status, value, headers = reply(self.command, self.path, body)
            data = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
            try:
                if drip_headers:
                    data = f"HTTP/1.0 {status} OK\r\nContent-Length: {len(data)}\r\n\r\n".encode("ascii") + data
                else:
                    self.send_response(status)
                    for key, value in headers.items():
                        self.send_header(key, value)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                if drip_delay:
                    for byte in data:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(drip_delay)
                else:
                    self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                if finished is not None:
                    finished.set()

        do_GET = handle_request
        do_POST = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def mcp_reply(result=None, failure=None):
    def reply(method, path, body):
        assert method == "POST"
        assert urlsplit(path).path.endswith("/comfyart/mcp")
        value = ({"protocolVersion": "2025-06-18"} if body["method"] == "initialize" else
                 {"tools": TOOLS} if body["method"] == "tools/list" else result)
        response = {"jsonrpc": "2.0", "id": body["id"], "result": value}
        if body["method"] == "tools/call" and failure is not None:
            response = failure(body, response)
        return 200, response, {}
    return reply


def graph_file(tmp_path):
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(GRAPH), encoding="utf-8")
    return str(path)


def test_schema_is_offline_and_help_is_text():
    code, result = invoke("--url", "http://127.0.0.1:1", "schema")
    assert code == 0
    assert result["data"]["wait_options"] == {"--wait-timeout": 300, "--interval": 1.5}
    assert result["data"]["exit_codes"]["130"] == "interrupted; server jobs are not cancelled"
    help_result = subprocess.run([sys.executable, str(CLI), "--help"], capture_output=True)
    assert help_result.returncode == 0
    assert b"usage:" in help_result.stdout


@pytest.mark.parametrize("args", [[], ["bogus"], ["tools"], ["--timeout", "nan", "tools", "list"],
                                  ["run"], ["tools", "call", "add_text", "--args", "{}", "--args-file", "-"]])
def test_parser_errors_are_json(args):
    code, result = invoke(*args)
    assert code == 2
    assert result["ok"] is False
    assert result["error"]["submission"] == "not-sent"


@pytest.mark.parametrize("text", ["[]", "{", '{"x":NaN}', '{"x":1e999}'])
def test_invalid_tool_arguments_are_rejected_before_network(text):
    code, result = invoke("--url", "http://127.0.0.1:1", "tools", "call", "add_text", "--args", text)
    assert (code, result["error"]["kind"]) == (2, "input")


def test_deep_json_is_an_input_error_before_network():
    text = '{"nested":' + '[' * 1100 + '0' + ']' * 1100 + '}'
    code, result = invoke("--url", "http://127.0.0.1:1", "tools", "call", "add_text", "--args-file", "-", stdin=text)
    assert (code, result["error"]["kind"], result["error"]["submission"]) == (2, "input", "not-sent")


def test_live_tools_and_lossless_unicode_stdin():
    blocks = [{"type": "text", "text": '{"text":"你好画布"}'},
              {"type": "image", "mimeType": "image/png", "data": "YWJj"},
              {"type": "future-block", "resource": {"id": 7}}]
    with host(mcp_reply({"content": blocks, "isError": False})) as (url, requests):
        code, result = invoke("--url", url + "/prefix", "--user", "alice", "--project", "画布 & 1",
                              "tools", "call", "add_text", "--args-file", "-", stdin='{"text":"你好"}')
    assert code == 0
    assert result["data"]["content"] == blocks
    assert result["data"]["value"] == {"text": "你好画布"}
    call = requests[-1]
    assert call[2]["params"] == {"name": "add_text", "arguments": {"text": "你好"}}
    assert parse_qs(urlsplit(call[1]).query) == {"project": ["画布 & 1"]}
    assert call[3]["Comfy-User"] == "alice"
    assert [r[2]["method"] for r in requests] == ["initialize", "tools/list", "tools/call"]


def test_tool_show_uses_live_schema_and_unknown_tool_is_not_called():
    with host(mcp_reply()) as (url, requests):
        code, result = invoke("--url", url, "tools", "show", "add_text")
        assert (code, result["data"]) == (0, {"name": "add_text", "description": "Add text", "input_schema": {"type": "object"}})
        code, result = invoke("--url", url, "tools", "call", "missing")
    assert (code, result["error"]["kind"]) == (2, "input")
    assert all(r[2]["method"] != "tools/call" for r in requests)


def test_disabled_mcp_is_visible():
    with host(lambda *_: (403, {"error": {"message": "MCP is off"}}, {})) as (url, requests):
        code, result = invoke("--url", url, "tools", "list")
    assert code == 3
    assert result["error"]["details"] == {"http_status": 403, "response": {"error": {"message": "MCP is off"}}}
    assert len(requests) == 1


def test_tool_error_keeps_unknown_effect_and_content():
    blocks = [{"type": "text", "text": "TimeoutError: command may still complete"}]
    with host(mcp_reply({"content": blocks, "isError": True})) as (url, requests):
        code, result = invoke("--url", url, "tools", "call", "add_text")
    assert (code, result["error"]["submission"]) == (4, "unknown")
    assert result["error"]["details"]["result"]["content"] == blocks
    assert len([r for r in requests if r[2]["method"] == "tools/call"]) == 1


@pytest.mark.parametrize("failure,code,effect", [
    (lambda body, response: {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32602, "message": "unknown tool"}}, 4, "rejected"),
    (lambda body, response: {**response, "id": "wrong"}, 6, "unknown"),
    (lambda body, response: {**response, "result": {"content": "bad", "isError": False}}, 6, "unknown"),
    (lambda body, response: {**response, "result": {"content": [{"type": "text", "text": None}]}}, 6, "unknown"),
    (lambda body, response: {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32603, "message": "internal error"}}, 4, "unknown"),
    (lambda body, response: {"jsonrpc": "2.0", "id": body["id"], "error": "bad"}, 6, "unknown"),
])
def test_rpc_failures(failure, code, effect):
    with host(mcp_reply({"content": []}, failure)) as (url, requests):
        actual, result = invoke("--url", url, "tools", "call", "add_text")
    assert (actual, result["error"]["submission"]) == (code, effect)
    assert len([r for r in requests if r[2]["method"] == "tools/call"]) == 1


def test_wait_generation_done_preserves_individual_failure():
    value = {"status": "done", "results": [{"id": "g", "status": "error", "error": "out of memory"}]}
    with host(mcp_reply({"content": [{"type": "text", "text": json.dumps(value)}]})) as (url, _):
        code, result = invoke("--url", url, "tools", "call", "wait_generation", "--args", '{"ids":["g"]}')
    assert (code, result["data"]["value"]) == (0, value)


@pytest.mark.parametrize("route,args,value", [
    ("/comfyart/projects", ["projects", "list"], {"projects": [{"id": "p"}]}),
    ("/comfyart/projects/p", ["projects", "get", "p"], {"store": {}}),
    ("/comfyart/workflows", ["workflows", "list"], {"workflows": [{"name": "a.api.json"}]}),
    ("/comfyart/workflows/a.api.json", ["workflows", "show", "a.api.json"], GRAPH),
    ("/api/object_info/SaveImage", ["nodes", "info", "SaveImage"], {"SaveImage": {"input": {}}}),
])
def test_host_discovery(route, args, value):
    with host(lambda *_: (200, value, {})) as (url, requests):
        code, result = invoke("--url", url, *args)
    assert (code, result["data"]) == (0, value)
    assert [(r[0], r[1]) for r in requests] == [("GET", route)]


@pytest.mark.parametrize("name", ["../secret.json", "..%2fsecret.json", "..\\secret.json", "missing-suffix"])
def test_workflow_path_traversal_is_rejected(name):
    code, result = invoke("--url", "http://127.0.0.1:1", "workflows", "show", name)
    assert (code, result["error"]["kind"]) == (2, "input")


@pytest.mark.parametrize("url", ["http://user:secret@localhost", "http://localhost/?token=secret",
                                 "http://localhost/#fragment", "file:///tmp", "http://localhost/../path"])
def test_unsafe_base_url_is_rejected(url):
    code, result = invoke("--url", url, "tools", "list")
    assert (code, result["error"]["kind"]) == (2, "input")
    assert "secret" not in result["error"]["message"]


def test_graph_submission_sends_once_and_returns_receipt(tmp_path):
    with host(lambda *_: (200, {"prompt_id": "p", "number": 4, "node_errors": {}}, {})) as (url, requests):
        code, result = invoke("--url", url, "run", "--file", graph_file(tmp_path))
    assert (code, result["data"]) == (0, {"prompt_id": "p", "number": 4, "node_errors": {}, "state": "submitted"})
    assert [(r[0], r[1], r[2]) for r in requests] == [("POST", "/api/prompt", {"prompt": GRAPH})]


def test_partial_graph_validation_keeps_accepted_prompt_receipt(tmp_path):
    receipt = {"prompt_id": "p", "number": 4, "node_errors": {"5": {"errors": ["missing input"]}}}
    with host(lambda *_: (200, receipt, {})) as (url, requests):
        code, result = invoke("--url", url, "run", "--file", graph_file(tmp_path))
    assert (code, result["data"]) == (0, {**receipt, "state": "submitted"})
    assert len(requests) == 1


@pytest.mark.parametrize("graph", [{}, {"nodes": []}, {"prompt": GRAPH}, {"1": {"class_type": "X", "inputs": []}}])
def test_invalid_graph_never_submits(graph):
    code, result = invoke("--url", "http://127.0.0.1:1", "run", "--file", "-", stdin=json.dumps(graph))
    assert (code, result["error"]["submission"]) == (2, "not-sent")


@pytest.mark.parametrize("status,value,headers,code,effect", [
    (307, {}, {"Location": "/accepted"}, 3, "unknown"),
    (302, {}, {"Location": "/accepted"}, 3, "unknown"),
    (200, b"{broken", {}, 6, "unknown"),
    (200, {"unexpected": True}, {}, 6, "unknown"),
    (400, {"error": "bad graph", "node_errors": {"1": {}}}, {}, 4, "rejected"),
    (408, {"error": "gateway timeout"}, {}, 3, "unknown"),
])
def test_submission_failures_never_retry(status, value, headers, code, effect, tmp_path):
    with host(lambda *_: (status, value, headers)) as (url, requests):
        actual, result = invoke("--url", url, "run", "--file", graph_file(tmp_path))
    assert (actual, result["error"]["submission"]) == (code, effect)
    assert len(requests) == 1


def test_timeout_does_not_resubmit_accepted_mutation(tmp_path):
    def slow(*_):
        time.sleep(0.3)
        return 200, {"prompt_id": "accepted"}, {}
    with host(slow) as (url, requests):
        code, result = invoke("--url", url, "--timeout", "0.08", "run", "--file", graph_file(tmp_path))
    assert (code, result["error"]["submission"]) == (5, "unknown")
    assert len(requests) == 1


@pytest.mark.parametrize("event,state", [("execution_error", "failed"), ("execution_interrupted", "interrupted")])
def test_failed_history_is_never_success(event, state):
    entry = {"outputs": {"7": {"text": ["partial"]}}, "status": {
        "completed": True, "status_str": "success", "messages": [[event, {"message": "failure"}]]}}
    with host(lambda *_: (200, {"p": entry}, {})) as (url, requests):
        code, result = invoke("--url", url, "wait", "p")
    assert code == 7
    assert result["error"]["details"]["prompt"]["state"] == state
    assert result["error"]["details"]["prompt"]["outputs"] == entry["outputs"]
    assert [(r[0], r[1]) for r in requests] == [("GET", "/api/history/p")]


def test_complete_history_returns_all_outputs_and_same_host_view_urls():
    outputs = {"9": {"images": [{"filename": "图 #1.png", "subfolder": "a/b", "type": "output"}],
                     "text": ["caption"], "custom": {"score": 3}},
               "10": {"audio": [{"filename": "voice.flac", "type": "output"}]}}
    entry = {"outputs": outputs, "status": {"completed": True, "status_str": "success", "messages": []}}
    with host(lambda *_: (200, {"p": entry}, {})) as (url, _):
        code, result = invoke("--url", url + "/prefix", "status", "p")
    assert (code, result["data"]["state"], result["data"]["outputs"]) == (0, "complete", outputs)
    assert result["data"]["media"][0]["url"] == url + "/prefix/api/view?filename=%E5%9B%BE+%231.png&subfolder=a%2Fb&type=output"
    assert len(result["data"]["media"]) == 2


def test_wait_follows_existing_prompt_from_queue_to_complete():
    history_reads = []

    def reply(method, path, body):
        if path == "/api/queue":
            return 200, {"queue_running": [], "queue_pending": [[1, "p"]]}, {}
        history_reads.append(path)
        entry = {"outputs": {"9": {"text": ["ready"]}}, "status": {"completed": True}}
        return 200, {} if len(history_reads) == 1 else {"p": entry}, {}

    with host(reply) as (url, requests):
        code, result = invoke("--url", url, "wait", "p", "--interval", "0.01")
    assert (code, result["data"]["state"], result["data"]["outputs"]) == (0, "complete", {"9": {"text": ["ready"]}})
    assert [(r[0], r[1]) for r in requests] == [("GET", "/api/history/p"), ("GET", "/api/queue"), ("GET", "/api/history/p")]


def test_error_status_without_execution_messages_is_failure():
    entry = {"outputs": {}, "status": {"status_str": "error", "completed": False}}
    with host(lambda *_: (200, {"p": entry}, {})) as (url, _):
        code, result = invoke("--url", url, "status", "p")
    assert (code, result["error"]["details"]["prompt"]["state"]) == (7, "failed")


@pytest.mark.parametrize("value", [b"NaN", b"{\"x\":1e999}", b"[]", b"not JSON"])
def test_malformed_get_response_is_protocol_failure(value):
    with host(lambda *_: (200, value, {})) as (url, _):
        code, result = invoke("--url", url, "projects", "list")
    assert (code, result["error"]["kind"], result["error"]["submission"]) == (6, "protocol", "not-sent")


@pytest.mark.parametrize("queue,state", [({"queue_running": [[1, "p"]], "queue_pending": []}, "running"),
                                         ({"queue_running": [], "queue_pending": [[1, "p"]]}, "queued"),
                                         ({"queue_running": [], "queue_pending": []}, "unknown")])
def test_empty_history_uses_queue_without_claiming_completion(queue, state):
    with host(lambda method, path, body: (200, queue if path.endswith("/queue") else {}, {})) as (url, requests):
        code, result = invoke("--url", url, "status", "p")
    assert (code, result["data"]["state"]) == (0, state)
    assert [r[1] for r in requests] == ["/api/history/p", "/api/queue"]


def test_unknown_history_waits_to_deadline_and_clamps_request_timeout():
    def slow(*_):
        time.sleep(0.3)
        return 200, {}, {}
    with host(slow) as (url, requests):
        start = time.monotonic()
        code, result = invoke("--url", url, "--timeout", "210", "wait", "p", "--wait-timeout", "0.08")
        elapsed = time.monotonic() - start
    assert (code, result["error"]["kind"]) == (5, "timeout")
    assert result["error"]["details"]["prompt_id"] == "p"
    assert elapsed < 2
    assert [(r[0], r[1]) for r in requests] in ([], [("GET", "/api/history/p")])


def test_repeated_unknown_observations_are_not_completion():
    def reply(method, path, body):
        return 200, {"queue_running": [], "queue_pending": []} if path.endswith("/queue") else {}, {}
    with host(reply) as (url, requests):
        code, result = invoke("--url", url, "wait", "p", "--wait-timeout", "0.8", "--interval", "0.03")
    assert code == 5
    assert result["error"]["details"]["last_state"]["state"] == "unknown"
    assert len(requests) >= 2
    assert all(r[0] == "GET" for r in requests)


def process_alive(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0x00000102
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@pytest.mark.parametrize("drip_headers", [False, True], ids=["body", "headers"])
def test_wait_deadline_stops_drip_and_reaps_actual_worker(tmp_path, drip_headers):
    pidfile = tmp_path / "worker.pid"
    wrapper = """import multiprocessing.context, pathlib, runpy, sys
original = multiprocessing.context.SpawnProcess.start
pidfile = pathlib.Path(sys.argv[2])
def start(process):
    original(process)
    pidfile.write_text(str(process.pid), encoding='ascii')
multiprocessing.context.SpawnProcess.start = start
sys.argv = [sys.argv[1], *sys.argv[3:]]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
    entry = {"p": {"outputs": {}, "status": {"completed": True}}}
    finished = threading.Event()
    with host(lambda *_: (200, entry, {}), drip_delay=0.05,
              drip_headers=drip_headers, finished=finished) as (url, requests):
        started = time.monotonic()
        code, result = invoke(str(pidfile), "--url", url, "wait", "p", "--wait-timeout", "0.6", wrapper=wrapper)
        elapsed = time.monotonic() - started
        assert finished.wait(0.6), "The timed-out worker left its HTTP connection open"
    assert (code, result["error"]["kind"]) == (5, "timeout")
    assert result["error"]["details"]["prompt_id"] == "p"
    assert elapsed < 1.5
    assert not process_alive(int(pidfile.read_text(encoding="ascii")))
    assert [(r[0], r[1]) for r in requests] == [("GET", "/api/history/p")]


def test_interrupted_wait_retains_prompt_id_without_cancel():
    wrapper = ("import runpy,sys,time; "
               "time.sleep=lambda _: (_ for _ in ()).throw(KeyboardInterrupt()); "
               "sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')")
    def reply(method, path, body):
        return 200, {"queue_running": [], "queue_pending": [[1, "p"]]} if path.endswith("/queue") else {}, {}
    with host(reply) as (url, requests):
        code, result = invoke("--url", url, "wait", "p", wrapper=wrapper)
    assert (code, result["error"]["details"], result["error"]["submission"]) == (130, {"prompt_id": "p"}, "not-sent")
    assert [r[1] for r in requests] == ["/api/history/p", "/api/queue"]
