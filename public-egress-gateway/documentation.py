"""Same-origin Swagger assets; documentation never grants fetch privileges."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from api_contract import TAGS

ASSETS = Path(__file__).parent / "swagger-assets"
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
    "base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'none'"
)
HTML = (
    b'<!doctype html><html lang="en"><head><meta charset="utf-8">'
    b'<meta name="viewport" content="width=device-width,initial-scale=1">'
    b"<title>DRP Public Egress Gateway API</title>"
    b'<link rel="icon" href="data:,">'
    b'<link rel="stylesheet" href="/docs/assets/swagger-ui.css"></head>'
    b'<body><div id="swagger-ui"></div>'
    b'<script src="/docs/assets/swagger-ui-bundle.js"></script>'
    b'<script src="/docs/assets/initialize.js"></script></body></html>'
)
INITIALIZE = (
    "const order=" + json.dumps([name for name, _ in TAGS]) + ";"
    "window.ui=SwaggerUIBundle({url:'/openapi.json',dom_id:'#swagger-ui',"
    "tagsSorter:(a,b)=>order.indexOf(a)-order.indexOf(b),"
    "docExpansion:'list',deepLinking:true,validatorUrl:null,"
    "queryConfigEnabled:false,persistAuthorization:false,"
    "supportedSubmitMethods:['get']});"
).encode()


@lru_cache(maxsize=4)
def asset(path: str) -> tuple[bytes, str] | None:
    """Never join a user-controlled path to the asset directory."""
    files = {
        "/docs/assets/swagger-ui-bundle.js": (
            "swagger-ui-bundle.js",
            "text/javascript",
        ),
        "/docs/assets/swagger-ui.css": ("swagger-ui.css", "text/css"),
    }
    if path == "/docs/assets/initialize.js":
        return INITIALIZE, "text/javascript"
    if path in files:
        name, mime = files[path]
        return (ASSETS / name).read_bytes(), mime
    return None
