# CLI reference

The entry point is `python scripts/comfyart.py`. Python 3.10 or newer is required. The script has no third-party dependencies and is not a supported Python SDK.

The [CLI how-to](cli.md) covers canvas edits and headless graph execution. `schema` returns the command contract as JSON without contacting a server.

## Commands

| Command | Result |
| --- | --- |
| `schema` | Offline CLI commands, options, output contract, and exit codes. |
| `tools list` | Live tool names, descriptions, and `input_schema` objects. |
| `tools show NAME` | One live tool definition. |
| `tools call NAME [--args JSON \| --args-file FILE]` | One invocation with a JSON object. Defaults to `{}`. `FILE` can be `-` for stdin. |
| `projects list` | Saved project metadata. |
| `projects get ID` | Saved project snapshot. |
| `workflows list` | Sorted bundled JSON names from the target host. |
| `workflows show NAME` | Host workflow JSON. `NAME` includes `.json`. |
| `nodes info [CLASS]` | Installed ComfyUI node metadata for one class or all classes. |
| `run --file FILE` | Receipt for one API graph submission. `FILE` can be `-` for stdin. |
| `status PROMPT_ID` | Current observation of an existing ComfyUI prompt. |
| `wait PROMPT_ID [--wait-timeout SECONDS] [--interval SECONDS]` | Terminal observation or deadline error. |

`tools call` discovers definitions before invoking the named tool. Arguments must be a JSON object with finite numbers. Use the live `input_schema` to prepare them. The CLI does not implement JSON Schema validation. Tool handlers determine which invalid arguments they reject.

`run` requires a nonempty object keyed by node IDs. Each node has a nonempty string `class_type` and an object `inputs`. Additional node fields are preserved. ComfyUI validates node classes, links, inputs, and installed model files.

A `prompt_id` confirms acceptance even when the receipt includes `node_errors` for invalid branches. The receipt preserves those errors. ComfyUI can schedule the remaining valid output branches.

## Global options

Global options precede the command. Command-specific options follow it.

| Option | Default | Meaning |
| --- | --- | --- |
| `--url URL` | `http://127.0.0.1:8188` | HTTP or HTTPS server base URL. A reverse-proxy path prefix is preserved. Credentials, query strings, fragments, and traversal are rejected. |
| `--user USER` | Unset | Existing ComfyUI `Comfy-User` selection header. It is not authentication. |
| `--project ID` | Unset | MCP target project. Recommended for canvas calls. The server resolves or rejects ambiguous open tabs. |
| `--timeout SECONDS` | `210` | Positive finite HTTP timeout. Allows a 170-second `wait_generation` call plus server allowance. |

`wait --wait-timeout` defaults to 300 seconds. `wait --interval` defaults to 1.5 seconds. Each observation request uses the smaller of the HTTP timeout and the remaining wait budget.

`wait` runs each HTTP observation in a short-lived worker process. The remaining total budget includes worker startup, response headers, and the response body. At the deadline the CLI terminates and joins the worker, then emits a timeout error. This also bounds responses that continuously send small chunks. The standard library's Windows spawn implementation bypasses virtual-environment redirectors, so termination targets the worker itself. Other commands use direct HTTP requests with socket inactivity timeouts.

The CLI never enables MCP, changes a generation provider, starts ComfyUI, opens a browser, uploads inputs, installs models, downloads output files, or cancels jobs.

## JSON output

Every operational result has `ok` and `command`, followed by either `data` or `error`.

```json
{"ok":true,"command":"run","data":{"prompt_id":"abc","state":"submitted"}}
```

```json
{"ok":false,"command":"run","error":{"kind":"timeout","message":"Request timed out. Inspect the target before submitting again.","submission":"unknown","details":{}}}
```

`error.submission` describes mutation uncertainty.

| Value | Meaning |
| --- | --- |
| `not-sent` | No mutating request was dispatched by this command. |
| `rejected` | The server explicitly rejected a mutation through an HTTP validation or access error, a JSON-RPC request rejection, or graph validation. |
| `unknown` | The mutation may have taken effect. Reconciliation is required before another submission. |

`tools call` returns the MCP result fields unchanged and adds `value`. All `content` blocks, including images and unknown block types, remain intact. `value` is the first text block decoded as JSON when valid, otherwise its text, or `null` if no text exists. Tool failures retain that result in `error.details.result`.

An MCP result with `isError: true` exits nonzero and keeps `submission: "unknown"`. A handler can fail after dispatching a browser action. Client timeouts, malformed responses, response-ID mismatches, redirects, and interruptions after mutating dispatch also retain an unknown effect. A JSON-RPC internal error does not establish rejection.

`wait_generation` invocation success does not establish generation success. Its overall `status: "done"` can contain individual error or unknown-ID results. The caller inspects each entry in `value.results`.

## Prompt observations

`status` and `wait` return `prompt_id`, `state`, `outputs`, `media`, `messages`, and `status`. The last field preserves the server's history status object. Every node output remains available, including non-media data. Media references include a `node_id`, the original file fields, and an encoded same-host `/api/view` URL.

| State | Evidence |
| --- | --- |
| `queued` | Prompt ID is in the pending queue. |
| `running` | Prompt ID is in the running queue. |
| `complete` | History records completion or an execution-success message, without an error or interruption. |
| `failed` | History reports an error or an execution-error message. |
| `interrupted` | History includes an execution-interrupted message. |
| `unknown` | Neither terminal history nor a queue entry establishes another state. |

Empty history never means completion. An unknown prompt remains unknown until new evidence or the wait deadline. These commands only observe the same prompt ID and never resubmit a graph. Failed or interrupted observations appear in `error.details.prompt`. A wait timeout retains `prompt_id` and the last full observation when available. An interrupted observation retains `prompt_id`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Command succeeded. A submission or running observation is not completed generation. |
| 1 | Unexpected CLI failure. |
| 2 | Invalid command, JSON input, URL, or graph shape. No mutation dispatched. |
| 3 | Service unavailable, denied, or redirected. Includes MCP disabled and unavailable routes. |
| 4 | Tool error, JSON-RPC error, or rejected graph submission. |
| 5 | HTTP timeout or wait deadline. Server jobs continue. |
| 6 | Invalid JSON, HTTP, or JSON-RPC response. |
| 7 | Observed workflow failure or interruption. |
| 130 | User interrupted the CLI. Server operations are not cancelled. |

Invalid usage emits a JSON error on stdout. `--help` emits human-readable text. Operational JSON uses ASCII escapes for Unicode. UTF-8 files and stdin preserve non-ASCII arguments across Windows subprocess boundaries.

## Server routes

MCP uses `POST /comfyart/mcp`. Saved projects use `GET /comfyart/projects` and `GET /comfyart/projects/{id}`. The workflow index uses `GET /comfyart/workflows`, which returns `{"workflows":[{"name":"example.api.json"}]}`. Named workflow retrieval uses `GET /comfyart/workflows/{name}`. The index applies resolved-directory containment and excludes symlinks to external files.

The graph runner uses `/api/prompt`, `/api/history/{id}`, `/api/queue`, `/api/object_info`, and `/api/view`. Older ComfyArt hosts without the index return an explicit route error. The CLI has no local-catalog fallback or alternate submission endpoint.
