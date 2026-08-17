"""S0 健康检查测试。"""

from fastapi.testclient import TestClient


def test_health_returns_ok(client: TestClient) -> None:
    """/health 不需要鉴权且返回 ok。"""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["pid"] > 0
    assert data["version"] == "0.0.2"
    assert data["started_at"]


def test_protected_endpoint_requires_token(client: TestClient) -> None:
    """业务接口缺少令牌时返回 401。"""
    response = client.post("/rag/ingest", json={"path": "C:/tmp"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
