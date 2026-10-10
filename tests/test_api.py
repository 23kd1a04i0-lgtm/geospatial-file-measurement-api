from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
SAMPLE_KML = Path(__file__).parents[1] / "examples" / "sample.kml"


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_upload_kml_and_measurements(tmp_path, monkeypatch):
    # Keep this test's SQLite database and uploads isolated from local developer data.
    import app.main as main

    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "geospatial.sqlite3")
    main.initialize_database()

    with SAMPLE_KML.open("rb") as sample:
        response = client.post(
            "/api/files/",
            files={"file": ("sample.kml", sample, "application/vnd.google-earth.kml+xml")},
        )
    assert response.status_code == 201, response.text
    file_info = response.json()
    assert file_info["feature_count"] == 3
    assert file_info["crs"] == "EPSG:4326"

    measurements = client.get(f"/api/files/{file_info['id']}/measurements/")
    assert measurements.status_code == 200
    results = measurements.json()["measurements"]
    assert len(results) == 3
    polygon = next(item for item in results if item["geometry_type"] == "Polygon")
    line = next(item for item in results if item["geometry_type"] == "LineString")
    point = next(item for item in results if item["geometry_type"] == "Point")
    assert polygon["status"] == "COMPLETED"
    assert polygon["unit"] == "m²"
    assert polygon["value"] > 0
    assert line["status"] == "COMPLETED"
    assert line["unit"] == "m"
    assert line["value"] > 0
    assert point["status"] == "NOT_APPLICABLE"


def test_rejects_unsupported_extension():
    response = client.post("/api/files/", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert response.status_code == 400
