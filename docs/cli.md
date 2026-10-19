# Operate ComfyArt from an agent or terminal

Use `scripts/comfyart.py` with Python 3.10 or newer. The CLI uses only the standard library and needs no package installation or frontend build.

From the ComfyArt directory, use the Python environment that runs ComfyUI.

```powershell
python scripts/comfyart.py schema
python scripts/comfyart.py --url http://127.0.0.1:8188 tools list
```

If Python is not on `PATH`, invoke the executable directly. For a Windows portable installation, adjust these paths to your installation.

```powershell
& 'C:\ComfyUI_windows_portable\python_embeded\python.exe' 'C:\ComfyUI_windows_portable\ComfyUI\custom_nodes\ComfyArt\scripts\comfyart.py' schema
```

For a virtual environment, activate it first or use its `Scripts\python.exe`. Put global options before the command. See the [CLI reference](cli-reference.md) for all options, output fields, and exit codes.

## Edit an open canvas

1. Start ComfyUI and enable MCP in ComfyArt's **Agent settings**.
2. Open the target project in a browser and keep that tab connected.
3. Obtain the saved project ID and check its live canvas.

```powershell
python scripts/comfyart.py projects list
python scripts/comfyart.py --project project-id tools call get_canvas
python scripts/comfyart.py tools show add_text
```

Create a UTF-8 file named `title.json` with the tool arguments.

```json
{"text":"Quarterly review","x":100,"y":100}
```

Invoke the edit once, then read the canvas to check the result.

```powershell
python scripts/comfyart.py --project project-id tools call add_text --args-file title.json
python scripts/comfyart.py --project project-id tools call get_canvas
```

For short arguments, use `--args '{"text":"Hello","x":100,"y":100}'`. Use `--args-file -` to read a UTF-8 JSON object from stdin. The host's tool schema defines the available arguments. Saved projects do not prove that a live browser tab is available.

## Start and observe canvas generation

Inspect the live schema and prepare `generation.json` with explicit model and generation arguments for your host.

```powershell
python scripts/comfyart.py tools show generate_image
python scripts/comfyart.py --project project-id tools call generate_image --args-file generation.json
```

Read the returned IDs from `data.value`. Create `generation-ids.json` with those IDs, then observe them.

```json
{"ids":["returned-generation-id"],"timeout_s":170}
```

```powershell
python scripts/comfyart.py --project project-id tools call wait_generation --args-file generation-ids.json
```

Inspect each entry in `data.value.results`. An overall `status` of `done` means that no items remain running. Individual items can still have `status: "error"` or `status: "unknown id"`. Exit code 0 confirms the tool invocation, not successful generation. If the overall status is `running`, observe the same IDs again.

The browser uses its existing generation provider. A canvas generation call can use Comfy Cloud if that provider is already configured. The CLI does not select a provider or change settings.

## Run a prepared graph without a canvas

List the workflows shipped on the selected host and inspect installed node inputs.

```powershell
python scripts/comfyart.py workflows list
python scripts/comfyart.py workflows show qwen-image-2.1-t2i.api.json
python scripts/comfyart.py nodes info CheckpointLoaderSimple
```

The workflow index lists bundled JSON files. It does not list saved user workflows or prove that their models are installed.

Prepare a ComfyUI API graph with concrete inputs and model filenames from this host. Exporting an API-format workflow from ComfyUI is one way to create it. Use the graph object itself, not a visual-editor workflow or a `/prompt` envelope. The CLI does not substitute checkpoint names or translate model-specific inputs.

Submit the graph once and preserve its receipt.

```powershell
python scripts/comfyart.py run --file prepared.api.json > receipt.json
if ($LASTEXITCODE -ne 0) { throw 'Submission was not confirmed. Inspect receipt.json.' }
$receipt = Get-Content receipt.json -Raw | ConvertFrom-Json
python scripts/comfyart.py status $receipt.data.prompt_id
python scripts/comfyart.py wait $receipt.data.prompt_id --wait-timeout 300
```

`run` returns a `prompt_id` immediately. `wait` observes that existing prompt and exits nonzero on execution failure, interruption, or deadline. A wait deadline leaves the server job running. Use `status` or `wait` again with the same ID.

A completed result retains all node outputs in `data.outputs`. `data.media` contains recognized file references and same-host `/api/view` URLs. Open those URLs to retrieve the files. Headless outputs are not added to a ComfyArt canvas.

## Handle an uncertain submission

Read both the process exit code and the JSON envelope. For example, an agent can call the script through Python's subprocess API.

```python
import json
import subprocess
import sys

result = subprocess.run(
	[sys.executable, "scripts/comfyart.py", "--project", project_id,
	 "tools", "call", "add_text", "--args-file", "title.json"],
	capture_output=True,
	check=False,
)
envelope = json.loads(result.stdout)
if result.returncode:
	raise RuntimeError(envelope["error"])
content = envelope["data"]
```

If `error.submission` is `unknown`, inspect the target canvas or ComfyUI queue before another submission. The CLI does not retry requests or follow redirects. A timeout, lost response, or tool error can occur after an action has already taken effect.

Operational stdout is one ASCII-safe JSON document. Unicode text survives JSON decoding. Input files and stdin use UTF-8. Help output is human-readable text.
