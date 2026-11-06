"""Real JWT verification and the local-only Swagger authorization boundary."""

import hashlib
import io
import json
import os
import time
import urllib.request
from typing import Any, cast
from unittest.mock import Mock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jwt.algorithms import RSAAlgorithm
from test_operator_api import client_for

from translator_service import operator_api
from translator_service.operator_auth import Settings, Verifier


@pytest.fixture
def auth() -> tuple[Verifier, rsa.RSAPrivateKey]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(RSAAlgorithm.to_jwk(private.public_key()))
    public.update({"kid": "unit-key", "alg": "RS256"})
    key = jwt.PyJWK.from_dict(public)

    class Keys:
        def get_signing_key_from_jwt(self, token: str) -> jwt.PyJWK:
            return key

    verifier = object.__new__(Verifier)
    verifier.settings = Settings(
        "https://identity.invalid/realms/owner",
        "https://identity.invalid",
        "owner-operator",
        "unused-unit-ca",
    )
    verifier.keys = cast(jwt.PyJWKClient, Keys())
    return verifier, private


def token(auth: tuple[Verifier, rsa.RSAPrivateKey], **overrides: Any) -> str:
    verifier, key = auth
    return jwt.encode(
        {
            "iss": verifier.settings.issuer,
            "aud": verifier.settings.client_id,
            "azp": verifier.settings.client_id,
            "sub": "unit-operator",
            "typ": "Bearer",
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
            "resource_access": {
                verifier.settings.client_id: {"roles": ["operator-read"]}
            },
            **overrides,
        },
        key,
        algorithm="RS256",
        headers={"kid": "unit-key"},
    )


def test_correct_own_read_token_is_accepted(
    auth: tuple[Verifier, rsa.RSAPrivateKey],
) -> None:
    auth[0].verify(token(auth))


@pytest.mark.parametrize(
    "claim,value,status",
    [
        ("iss", "https://foreign.invalid/realms/owner", 401),
        ("aud", "shell", 401),
        ("azp", "shell", 401),
        ("exp", 0, 401),
        ("iat", int(time.time()) + 3600, 401),
        ("sub", "", 401),
        ("typ", "ID", 401),
        ("resource_access", {}, 403),
        ("resource_access", {"owner-operator": {"roles": ["platform-ui-viewer"]}}, 403),
    ],
)
def test_other_identity_expired_token_and_wrong_role_are_denied(
    auth: tuple[Verifier, rsa.RSAPrivateKey], claim: str, value: Any, status: int
) -> None:
    with pytest.raises(HTTPException) as error:
        auth[0].verify(token(auth, **{claim: value}))
    assert error.value.status_code == status


def test_unsigned_or_invalid_signature_is_denied(
    auth: tuple[Verifier, rsa.RSAPrivateKey],
) -> None:
    for value in ("not-a-token", jwt.encode({"sub": "operator"}, "", algorithm="none")):
        with pytest.raises(HTTPException) as error:
            auth[0].verify(value)
        assert error.value.status_code == 401
    foreign = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(HTTPException) as error:
        auth[0].verify(token((auth[0], foreign)))
    assert error.value.status_code == 401


def test_identity_connection_failure_is_fail_closed(
    auth: tuple[Verifier, rsa.RSAPrivateKey],
) -> None:
    class Broken:
        def get_signing_key_from_jwt(self, token: str) -> jwt.PyJWK:
            raise jwt.PyJWKClientConnectionError("unavailable")

    auth[0].keys = cast(jwt.PyJWKClient, Broken())
    with pytest.raises(HTTPException) as error:
        auth[0].verify(token(auth))
    assert error.value.status_code == 503


def test_local_docs_are_public_but_all_operations_require_own_token(
    auth: tuple[Verifier, rsa.RSAPrivateKey],
) -> None:
    application = operator_api.create_app(auth[0].settings, auth[0])
    with client_for(application) as client:
        for path in (
            "/docs",
            "/openapi.json",
            "/docs/init.js",
            "/docs/oauth2-redirect.html",
            "/docs/oauth2-redirect.js",
        ):
            assert client.get(path).status_code == 200
        callback_script = client.get("/docs/oauth2-redirect.js").content
        assert hashlib.sha256(callback_script).hexdigest() == (
            "a5c8a34e09e49737382cc5019446eec7c3db3461963fc5bfe448361d3c5549e9"
        )
        schema = client.get("/openapi.json").json()
        assert len(schema["paths"]) == 6
        assert schema["paths"]["/v1/status"]["get"]["tags"] == ["default"]
        assert schema["paths"]["/v1/translate"]["post"]["tags"] == ["translator"]
        for path, operations in schema["paths"].items():
            for method, operation in operations.items():
                assert "security" in operation
                assert client.request(method, path).status_code == 401
        assert client.get(
            "/healthz", headers={"Authorization": "Bearer " + token(auth)}
        ).json() == {"status": "ok"}
        assert (
            client.get(
                "/healthz",
                headers={"Authorization": "Bearer " + token(auth, aud="shell")},
            ).status_code
            == 401
        )
        setup = client.get("/docs/init.js").text
        assert '"usePkceWithAuthorizationCodeGrant": true' in setup
        assert "persistAuthorization:false" in setup
        assert "clientSecret" not in setup
        assert (
            "connect-src 'self' https://identity.invalid"
            in client.get("/docs").headers["content-security-policy"]
        )
        assert (
            client.post("/v1/translate-text", json={"text": "text"}).status_code == 404
        )


def test_translate_permission_is_separate_from_read(
    auth: tuple[Verifier, rsa.RSAPrivateKey],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from translator_service import operator_translation

    reached: list[str] = []

    class Backend:
        def translate(
            self, payload: operator_translation.TranslationInput, actor: str
        ) -> operator_translation.TranslationOutput:
            reached.append(actor)
            return operator_translation.TranslationOutput(
                request_id="own-request",
                source_language=payload.source_language,
                target_language=payload.target_language,
                queue=payload.queue,
                translated_text="Hallo",
                warnings=[],
            )

    monkeypatch.setattr(operator_translation, "service", lambda: Backend())
    application = operator_api.create_app(auth[0].settings, auth[0])
    body = {
        "source_language": "en",
        "target_language": "de",
        "queue": "normal",
        "text": "Hello",
    }
    with client_for(application) as client:
        assert client.post("/v1/translate", json=body).status_code == 401
        assert (
            client.post(
                "/v1/translate",
                json=body,
                headers={"Authorization": "Bearer " + token(auth)},
            ).status_code
            == 403
        )
        assert reached == []
        translate_token = token(
            auth, resource_access={"owner-operator": {"roles": ["operator-translate"]}}
        )
        reply = client.post(
            "/v1/translate",
            json=body,
            headers={"Authorization": "Bearer " + translate_token},
        )
        assert reply.status_code == 200 and reply.json()["translated_text"] == "Hallo"
        assert reached == [auth[0].verify(translate_token, "operator-translate")]
        assert (
            client.get(
                "/healthz", headers={"Authorization": "Bearer " + translate_token}
            ).status_code
            == 403
        )
        assert client.post("/v1/translate", content=b"x" * 262145).status_code == 413
        assert (
            client.post(
                "/v1/translate", content=iter([b"x" * 131073, b"x" * 131073])
            ).status_code
            == 413
        )
        assert (
            client.post(
                "/v1/translate",
                json={**body, "request_id": "foreign"},
                headers={"Authorization": "Bearer " + translate_token},
            ).status_code
            == 422
        )
        assert len(reached) == 1


def test_mtls_reads_do_not_authorize_translation(
    auth: tuple[Verifier, rsa.RSAPrivateKey],
) -> None:
    application = operator_api.create_app(
        auth[0].settings, auth[0], protect_reads=False
    )
    with client_for(application) as client:
        assert client.get("/healthz").status_code == 200
        assert client.post("/v1/translate", json={}).status_code == 401


def test_disabled_network_translation_has_no_identity_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from translator_service import operator_translation

    for key in list(os.environ):
        if key.startswith(operator_translation.PREFIX + "API_OIDC_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv(operator_translation.PREFIX + "API_TRANSLATION_ENABLED", "false")
    with client_for(operator_api.network_app()) as client:
        assert client.get("/healthz").status_code == 200
        assert len(client.get("/openapi.json").json()["paths"]) == 5
        assert client.post("/v1/translate", json={}).status_code == 404
        assert client.post("/v1/translate", content=b"x" * 262145).status_code == 404


def test_disabled_translation_keeps_read_authorization(
    auth: tuple[Verifier, rsa.RSAPrivateKey],
) -> None:
    application = operator_api.create_app(
        auth[0].settings, auth[0], enable_translation=False
    )
    with client_for(application) as client:
        assert client.get("/healthz").status_code == 401
        assert (
            client.get(
                "/healthz", headers={"Authorization": "Bearer " + token(auth)}
            ).status_code
            == 200
        )
        assert "/v1/translate" not in client.get("/openapi.json").json()["paths"]
        assert client.post("/v1/translate", json={}).status_code == 404


def jwks_client(
    auth: tuple[Verifier, rsa.RSAPrivateKey],
    monkeypatch: pytest.MonkeyPatch,
    extra_keys: list[dict[str, Any]] | None = None,
) -> tuple[jwt.PyJWKClient, Mock]:
    """Use the real JWKS parser/cache with offline HTTP responses."""
    public = json.loads(RSAAlgorithm.to_jwk(auth[1].public_key()))
    public.update({"kid": "unit-key", "alg": "RS256"})
    payload = json.dumps({"keys": [*(extra_keys or []), public]}).encode()

    def reply(*args: object, **kwargs: object) -> io.BytesIO:
        return io.BytesIO(payload)

    response = Mock(side_effect=reply)
    opener = Mock()
    opener.open = response
    monkeypatch.setattr(urllib.request, "urlopen", response)
    monkeypatch.setattr(urllib.request, "build_opener", Mock(return_value=opener))
    client = jwt.PyJWKClient("https://identity.invalid/keys", lifespan=300)
    auth[0].keys = client
    return client, response


@pytest.mark.parametrize("distinct", [False, True])
def test_unknown_key_cannot_force_repeated_identity_fetches(
    auth: tuple[Verifier, rsa.RSAPrivateKey],
    monkeypatch: pytest.MonkeyPatch,
    distinct: bool,
) -> None:
    client, response = jwks_client(auth, monkeypatch)
    good = token(auth)
    auth[0].verify(good)
    for index in range(3):
        kid = "unknown-" + str(index if distinct else 0)
        with pytest.raises(jwt.PyJWKClientError):
            client.get_signing_key(kid)
    assert response.call_count == 1
    auth[0].verify(good)
    assert response.call_count == 1


def test_bad_rsa_key_does_not_block_the_valid_identity_key(
    auth: tuple[Verifier, rsa.RSAPrivateKey],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jwks_client(
        auth,
        monkeypatch,
        [{"kty": "RSA", "kid": "bad", "alg": "RS256", "n": 42, "e": "AQAB"}],
    )
    assert auth[0].verify(token(auth))


def test_valid_rotated_identity_key_is_loaded_after_refresh_cooldown(
    auth: tuple[Verifier, rsa.RSAPrivateKey],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, response = jwks_client(auth, monkeypatch)
    auth[0].verify(token(auth))
    rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(RSAAlgorithm.to_jwk(rotated.public_key()))
    public.update({"kid": "rotated-key", "alg": "RS256"})
    payload = json.dumps({"keys": [public]}).encode()

    def reply(*args: object, **kwargs: object) -> io.BytesIO:
        return io.BytesIO(payload)

    response.side_effect = reply
    after_cooldown = time.monotonic() + 31
    monkeypatch.setattr(time, "monotonic", lambda: after_cooldown)
    claims = jwt.decode(token(auth), options={"verify_signature": False})
    value = jwt.encode(
        claims, rotated, algorithm="RS256", headers={"kid": "rotated-key"}
    )
    assert auth[0].verify(value)
    assert response.call_count == 2


def test_unknown_critical_token_extension_is_rejected(
    auth: tuple[Verifier, rsa.RSAPrivateKey],
) -> None:
    claims = jwt.decode(token(auth), options={"verify_signature": False})
    value = jwt.encode(
        claims,
        auth[1],
        algorithm="RS256",
        headers={"kid": "unit-key", "crit": ["unknown"], "unknown": True},
    )
    with pytest.raises(HTTPException) as error:
        auth[0].verify(value)
    assert error.value.status_code == 401


def test_standard_base64_padding_does_not_break_valid_rsa_tokens(
    auth: tuple[Verifier, rsa.RSAPrivateKey],
) -> None:
    parts = token(auth).split(".")
    parts[-1] += "=" * (-len(parts[-1]) % 4)
    assert auth[0].verify(".".join(parts))


def test_hmac_token_is_rejected_before_identity_lookup(
    auth: tuple[Verifier, rsa.RSAPrivateKey],
) -> None:
    lookup = Mock()
    auth[0].keys = cast(jwt.PyJWKClient, lookup)
    value = jwt.encode({"sub": "unit-operator"}, os.urandom(32), algorithm="HS256")
    with pytest.raises(HTTPException) as error:
        auth[0].verify(value)
    assert error.value.status_code == 401
    lookup.get_signing_key_from_jwt.assert_not_called()
