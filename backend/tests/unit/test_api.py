import pytest
from httpx import ASGITransport, AsyncClient
from tiramisu_agents.api.main import app


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
