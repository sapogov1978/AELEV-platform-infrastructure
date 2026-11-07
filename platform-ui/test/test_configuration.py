from hashlib import sha256
from pathlib import Path

import pytest

from prepare import env, settings

ROOT = Path(__file__).resolve().parents[1]


def test_no_settings_fallback() -> None:
    compose = (ROOT / "compose.yml").read_text()
    assert ":-" not in compose
    assert "context: ." in compose
    assert "docker.sock" not in compose
    assert "VITE_" not in (ROOT / "Dockerfile").read_text()
    assert "file:../../../" not in (ROOT / "package.json").read_text()
    assert "AELEV_INFRASTRUCTURE_PLATFORM_UI_BACKEND_SUBNET" in compose
    assert "AELEV_INFRASTRUCTURE_PLATFORM_UI_EGRESS_SUBNET" in compose
    assert compose.count("ipam:") == 2


def test_local_keycloak_overlay_only_adds_tls_routing() -> None:
    overlay = (ROOT / "compose.local-keycloak.yml").read_text()
    assert "platform-ui-api:" in overlay
    assert "platform-ui-configuration-init:" in overlay
    assert "platform-ui-keycloak-local" in overlay
    assert "external: true" in overlay
    assert "extra_hosts: !reset []" in overlay
    for forbidden in ("image:", "build:", "volumes:", "environment:", "ports:"):
        assert forbidden not in overlay


def test_required_settings(tmp_path: Path) -> None:
    cfg = tmp_path / ".env"
    data = env(ROOT / ".env.example")
    data.pop("AELEV_INFRASTRUCTURE_PLATFORM_UI_PORT")
    cfg.write_text("\n".join(f"{key}={value}" for key, value in data.items()))
    with pytest.raises(ValueError):
        settings(cfg)


def test_operator_identity_is_platform_wide(tmp_path: Path) -> None:
    cfg = tmp_path / ".env"
    data = env(ROOT / ".env.example")
    data["AELEV_INFRASTRUCTURE_PLATFORM_UI_OPERATOR_USER"] = "service-operator"
    cfg.write_text("\n".join(f"{key}={value}" for key, value in data.items()))
    with pytest.raises(ValueError, match="platform-operator"):
        settings(cfg)


def test_test_excluded() -> None:
    patterns = (ROOT / ".dockerignore").read_text().splitlines()
    assert any(pattern in {"test", "test/", "/test", "/test/"} for pattern in patterns)


def test_no_compiled_service_catalogue() -> None:
    assert "component_id" not in (ROOT / "src/SystemStatusPanel.tsx").read_text()
    assert "@aelev/operator-ui-kit" not in (ROOT / "package-lock.json").read_text()


def test_previous_stylesheet_is_immutable() -> None:
    assert sha256((ROOT / "public/presentation/v1.css").read_bytes()).hexdigest() == (
        "455736e96e27d0ed0327e11e4c4039396df7f1f984852ec50d3ca5b6c949c263"
    )
    assert sha256((ROOT / "public/presentation/v2.css").read_bytes()).hexdigest() == (
        "a772eb9fc2dd707bcbfdc9acd6f982677c7b9c69b6d000e57ca4171dab65bc8e"
    )
    assert sha256((ROOT / "public/presentation/v3.css").read_bytes()).hexdigest() == (
        "15a2b9e53cb0c2a2b7f08f6ce946351d835a653701891aec5e6ea2dd3c1fe41a"
    )
    assert sha256((ROOT / "public/presentation/v4.css").read_bytes()).hexdigest() == (
        "775a842cdc7930517e377c339061264af5514ce827ef992aa1e8a38b465bdd61"
    )
    assert sha256((ROOT / "public/presentation/v5.css").read_bytes()).hexdigest() == (
        "d37b4bc371fc5492a074c0a197f2290acf7c84bf9d3640469989941a70ec1bd8"
    )


def test_owner_presentation_does_not_override_shell_layout() -> None:
    stylesheet = (ROOT / "public/presentation/v6.css").read_text()
    for token in (
        "--aelev-bg: #07130f",
        "--aelev-panel: #0c1f18",
        "--aelev-accent: #79e6af",
        "--aelev-button-bg: #20523e",
        ".aelev-owner-ui .panel",
    ):
        assert token in stylesheet
    shell = (ROOT / "src/App.css").read_text()
    assert "@import url('/presentation/v5.css')" in shell
    assert "@import url('/presentation/v6.css')" not in shell
    assert "height:88px" in shell
    assert "grid-template-columns:240px minmax(0,1fr)" in shell
    assert "background:#0a1412" in shell
    assert "background:#091512" in shell


def test_all_published_stylesheets_are_cross_origin_readable() -> None:
    nginx = (ROOT / "nginx.conf").read_text()
    assert r"^/presentation/v[123456]\.css$" in nginx
    assert "add_header Access-Control-Allow-Origin '*';" in nginx


def test_current_presentation_is_the_sdk_projection() -> None:
    public = ROOT / "public/presentation/v6.css"
    projection = ROOT / "operator-ui-kit/package/generated/presentation-v6.css"
    metadata = ROOT / "operator-ui-kit/package/src/presentation.ts"
    assert public.read_bytes() == projection.read_bytes()
    digest = sha256(public.read_bytes()).hexdigest()
    assert digest == "16b5dbb04ce93570ca83750d71a14406624be4bd9c840cdd98a7c0e876e33496"
    assert f'PLATFORM_PRESENTATION_SHA256 = "{digest}"' in metadata.read_text()
    assert 'PLATFORM_PRESENTATION_PATH = "/presentation/v6.css"' in metadata.read_text()


def test_shell_readiness_requires_metadata_api_but_liveness_does_not() -> None:
    nginx = (ROOT / "nginx.conf").read_text()
    readiness = nginx.split("location = /readyz {", 1)[1].split("}", 1)[0]
    liveness = nginx.split("location = /healthz {", 1)[1].split("}", 1)[0]
    assert "proxy_pass https://platform-ui-api:8444/api/readyz;" in readiness
    assert "return 200" not in readiness
    assert "return 200" in liveness
    compose = (ROOT / "compose.yml").read_text()
    assert "https://127.0.0.1:8443/healthz" in compose
    assert compose.count("io.aelev.recovery.registration: unguarded-automatic-v1") == 3
