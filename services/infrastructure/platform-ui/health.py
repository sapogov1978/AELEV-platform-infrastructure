"""Probe the actual HTTPS API with its dedicated local client identity."""

import ssl
from urllib.request import urlopen

context = ssl.create_default_context(cafile="/run/secrets/api/ca.crt")
context.load_cert_chain("/run/secrets/api/health.crt", "/run/secrets/api/health.key")
with urlopen(
    "https://127.0.0.1:8444/api/readyz", context=context, timeout=2
) as response:
    assert response.status == 200
    assert response.read() == b'{"status":"ready"}'
