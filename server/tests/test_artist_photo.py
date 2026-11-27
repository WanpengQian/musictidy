"""艺人照片: 用户上传后 GET 直接命中缓存, 不再去 TheAudioDB."""

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
MBID = "11111111-2222-3333-4444-555555555555"


def _upload(client, data, ct="image/jpeg"):
    return client.post(
        f"/api/v1/artists/{MBID}/photo/upload",
        files={"file": ("photo.jpg", data, ct)},
    )


def test_upload_then_get_serves_custom(client, env):
    r = _upload(client, JPEG)
    assert r.status_code == 200, r.text
    assert r.json()["bytes"] == len(JPEG)
    assert (env["data_dir"] / "artist_photos" / f"{MBID}.jpg").read_bytes() == JPEG

    r = client.get(f"/api/v1/artists/{MBID}/photo")
    assert r.status_code == 200
    assert r.content == JPEG
    assert r.headers["content-type"].startswith("image/jpeg")


def test_upload_overwrites_and_sniffs_png(client):
    assert _upload(client, JPEG).status_code == 200
    assert _upload(client, PNG, ct="image/png").status_code == 200
    r = client.get(f"/api/v1/artists/{MBID}/photo")
    assert r.content == PNG
    assert r.headers["content-type"].startswith("image/png")


def test_upload_rejects_empty_and_non_image(client):
    assert _upload(client, b"").status_code == 400
    assert _upload(client, b"hello world, not an image").status_code == 400


def test_upload_rejects_orphan_bucket(client):
    r = client.post(
        "/api/v1/artists/__orphan__/photo/upload",
        files={"file": ("photo.jpg", JPEG, "image/jpeg")},
    )
    assert r.status_code == 400
