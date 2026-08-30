import pytest
from httpx import ASGITransport, AsyncClient
from tiramisu_agents.api.main import app, create_app
from tiramisu_agents.api.settings import Settings


@pytest.mark.asyncio
async def test_health_endpoint() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "tiramisu-api",
        "version": "0.1.0",
        "environment": "development",
    }


@pytest.mark.asyncio
async def test_event_ingestion_is_disabled_without_development_opt_in() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/events",
            headers={"X-Tiramisu-Tenant-ID": "00000000-0000-0000-0000-000000000001"},
            json={
                "event_type": "enquiry.created",
                "source": "stub.website",
                "source_event_id": "source-1",
                "occurred_at": "2026-08-30T00:00:00Z",
            },
        )

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_naive_event_timestamp_is_request_validation_error() -> None:
    custom_app = create_app(settings=Settings(allow_unsafe_development_tenant_header=True))
    transport = ASGITransport(app=custom_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/events",
            headers={"X-Tiramisu-Tenant-ID": "00000000-0000-0000-0000-000000000001"},
            json={
                "event_type": "enquiry.created",
                "source": "stub.website",
                "source_event_id": "source-1",
                "occurred_at": "2026-08-30T12:00:00",
            },
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_health_uses_constructed_app_settings() -> None:
    custom_app = create_app(settings=Settings(environment="test"))
    transport = ASGITransport(app=custom_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["environment"] == "test"
