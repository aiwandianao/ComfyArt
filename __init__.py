from pathlib import Path

from aiohttp import web
from server import PromptServer

from . import api
from .nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

ROOT = Path(__file__).parent
DIST = ROOT / "dist"
WORKFLOWS = ROOT / "workflows"

routes = PromptServer.instance.routes


@routes.get("/comfyart")
async def comfyart_redirect(request: web.Request) -> web.Response:
    raise web.HTTPFound("/comfyart/")


@routes.get("/comfyart/")
async def comfyart_index(request: web.Request) -> web.StreamResponse:
    index = DIST / "index.html"
    if not index.is_file():
        return web.Response(status=503, text="ComfyArt is not built yet: run `npm install && npm run build` in " + str(ROOT))
    return web.FileResponse(index, headers={"Cache-Control": "no-cache"})


@routes.get("/comfyart/workflows")
async def comfyart_workflows(request: web.Request) -> web.Response:
    root = WORKFLOWS.resolve()
    names = []
    for path in WORKFLOWS.glob("*.json"):
        try:
            resolved = path.resolve()
            if resolved.parent == root and resolved.suffix == ".json" and resolved.is_file():
                names.append(path.name)
        except (OSError, RuntimeError):
            continue
    return web.json_response({"workflows": [{"name": name} for name in sorted(names)]})


@routes.get("/comfyart/workflows/{name}")
async def comfyart_workflow(request: web.Request) -> web.StreamResponse:
    name = request.match_info["name"]
    path = (WORKFLOWS / name).resolve()
    if path.parent != WORKFLOWS.resolve() or path.suffix != ".json" or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path)


if (DIST / "assets").is_dir():
    routes.static("/comfyart/assets", DIST / "assets")

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]
