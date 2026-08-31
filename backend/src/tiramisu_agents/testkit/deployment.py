"""Deterministic deployment identities for isolated contract tests."""

from tiramisu_agents.extensions.runtime import DeploymentRelease


def make_test_deployment_release(
    *,
    client_pack_fingerprint: str = "b" * 64,
    deployment_id: str = "test-deployment",
    build_id: str = "test-build",
    model_id: str = "test-model",
) -> DeploymentRelease:
    return DeploymentRelease(
        deployment_id=deployment_id,
        build_id=build_id,
        client_pack_fingerprint=client_pack_fingerprint,
        model_id=model_id,
    )


TEST_DEPLOYMENT_RELEASE = make_test_deployment_release()
