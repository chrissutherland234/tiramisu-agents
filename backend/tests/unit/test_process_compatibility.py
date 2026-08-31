import pytest
from tiramisu_agents.processes.compatibility import (
    DeploymentCompatibility,
    DeploymentCompatibilityError,
)


def test_deployment_compatibility_requires_every_exact_pin() -> None:
    compatibility = DeploymentCompatibility(
        client_pack_fingerprint="a" * 64,
        extension_manifest_hash="b" * 64,
        definition_fingerprints={("example", "1"): "c" * 64},
    )
    values = {
        "process_type": "example",
        "definition_version": "1",
        "client_pack_fingerprint": "a" * 64,
        "extension_manifest_hash": "b" * 64,
        "process_definition_fingerprint": "c" * 64,
    }

    compatibility.require_process(**values)

    for field in (
        "client_pack_fingerprint",
        "extension_manifest_hash",
        "process_definition_fingerprint",
    ):
        mismatched = {**values, field: "d" * 64}
        with pytest.raises(DeploymentCompatibilityError, match="do not match"):
            compatibility.require_process(**mismatched)


def test_deployment_compatibility_rejects_unknown_definition_and_invalid_hash() -> None:
    compatibility = DeploymentCompatibility(
        client_pack_fingerprint="a" * 64,
        extension_manifest_hash="b" * 64,
        definition_fingerprints={("example", "1"): "c" * 64},
    )

    with pytest.raises(DeploymentCompatibilityError, match="not present"):
        compatibility.require_process(
            process_type="other",
            definition_version="1",
            client_pack_fingerprint="a" * 64,
            extension_manifest_hash="b" * 64,
            process_definition_fingerprint="c" * 64,
        )
    with pytest.raises(ValueError, match="SHA-256"):
        DeploymentCompatibility(
            client_pack_fingerprint="not-a-hash",
            extension_manifest_hash="b" * 64,
            definition_fingerprints={("example", "1"): "c" * 64},
        )
