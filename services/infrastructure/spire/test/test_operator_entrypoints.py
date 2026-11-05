from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPROVAL = "DRP_SPIRE_DOCKER_SOCKET_APPROVAL"
VALUE = "operator-root-equivalent-v1"


def test_shell_entrypoints_require_explicit_socket_approval_before_docker() -> None:
    for name in ("install.sh", "export.sh"):
        text = (ROOT / name).read_text(encoding="utf-8")
        gate = text.index(APPROVAL)
        first_docker = text.index("docker ")
        mount = text.index("source=/var/run/docker.sock")
        assert gate < first_docker < mount
        assert VALUE in text
        assert "--network none" in text
        assert "--pids-limit 128" in text


def test_powershell_entrypoints_require_explicit_socket_approval() -> None:
    for name in ("install.ps1", "export.ps1"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert text.index(APPROVAL) < text.index("docker ")
        assert VALUE in text
        assert "--network none" in text
        assert "--pids-limit 128" in text
