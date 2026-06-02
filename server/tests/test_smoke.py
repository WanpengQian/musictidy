"""P0 smoke test —— FastAPI 起得来、健康检查通、stats 端点返回."""


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["allow_file_writes"] is False


def test_root_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "MusicTidy" in r.text


def test_admin_stats_empty(client):
    r = client.get("/api/v1/admin/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["items_total"] == 0
    assert body["queue"] == {"queued": 0, "running": 0, "done": 0, "failed": 0}


def test_admin_scan_returns_ok(client):
    """Mock 掉 scan_and_import 本身。我们这里只验 endpoint 调度逻辑."""
    r = client.post("/api/v1/admin/scan")
    assert r.status_code == 200
    # 立即返回 ok=True；真正 scan 在 background
    assert r.json()["ok"] is True


def test_queue_endpoint(client):
    r = client.get("/api/v1/admin/queue")
    assert r.status_code == 200
    assert r.json() == {"rows": []}
