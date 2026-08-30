import pytest
from pydantic import ValidationError
from tiramisu_agents.extensions import ExtensionManifest


def test_extension_manifest_exposes_registered_identifiers() -> None:
    manifest = ExtensionManifest(
        extension_id="fictional_booking",
        extension_version="0.1.0",
        tiramisu_compatibility=">=0.1,<0.2",
        process_definitions=("enquiry_to_booking.v1",),
        adapters=("stub.messaging.v1", "stub.booking.v1"),
        policies=("fictional.default.v1",),
    )

    assert manifest.registered_identifiers() == {
        "enquiry_to_booking.v1",
        "stub.messaging.v1",
        "stub.booking.v1",
        "fictional.default.v1",
    }
    assert len(manifest.fingerprint()) == 64
    assert manifest.fingerprint() == manifest.model_copy().fingerprint()


def test_extension_manifest_rejects_duplicate_registration() -> None:
    with pytest.raises(ValidationError, match="must be unique"):
        ExtensionManifest(
            extension_id="fictional_booking",
            extension_version="0.1.0",
            tiramisu_compatibility=">=0.1,<0.2",
            adapters=("stub.messaging.v1", "stub.messaging.v1"),
        )
    with pytest.raises(ValidationError, match="across categories"):
        ExtensionManifest(
            extension_id="fictional_booking",
            extension_version="0.1.0",
            tiramisu_compatibility=">=0.1,<0.2",
            adapters=("shared.identifier.v1",),
            policies=("shared.identifier.v1",),
        )
