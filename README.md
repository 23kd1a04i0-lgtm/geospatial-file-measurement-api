# Geospatial File Measurement API

A small, documented FastAPI service that accepts KML files or ZIP archives containing Shapefiles, stores extracted features, and calculates polygon area and line length in metric units.

## Features

- `POST /api/files/` uploads and processes a `.kml` file or a `.zip` containing a Shapefile (`.shp`, `.shx`, `.dbf`, and ideally `.prj`).
- `GET /api/files/{id}/` returns file metadata and processing status.
- `GET /api/files/{id}/features/` returns feature geometry, source CRS, attributes, and measurement results. Supports pagination with `limit` and `offset`.
- `GET /api/files/{id}/measurements/` returns per-feature measurements.
- Polygon/MultiPolygon area is returned in square metres (`m²`); LineString/MultiLineString length is returned in metres (`m`). Point features do not require a measurement.
- Unsupported or invalid geometries are represented with a status and reason instead of terminating processing for the whole file.
- SQLite persistence means processed file metadata and feature results remain available after a server restart.
- Interactive OpenAPI docs are available at `/docs`.

## Requirements

- Python 3.10 or newer
- pip

GeoPandas installs geospatial dependencies such as Shapely, pyproj, and a GDAL-backed I/O engine through its package dependencies. If your platform cannot install binary wheels, consult the GeoPandas installation documentation and install compatible geospatial libraries for that platform.

## Run locally

### 1. Create and activate a virtual environment

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Windows Command Prompt:

```bat
py -m venv .venv
.venv\Scripts\activate.bat
```

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Start the API

```bash
uvicorn app.main:app --reload
```

The API listens at `http://127.0.0.1:8000`. Visit `http://127.0.0.1:8000/docs` to explore and run requests in Swagger UI. The database and retained successful source uploads are created under `data/` by default. Set `GEO_API_DATA_DIR` to use a different location.

## API usage

### Upload a KML file

```bash
curl -X POST "http://127.0.0.1:8000/api/files/" \
  -F "file=@examples/sample.kml"
```

Windows PowerShell can use:

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/files/" -F "file=@examples/sample.kml"
```

Example response (`id` and timestamp are generated per upload):

```json
{
  "id": "a1b2c3d4e5f6",
  "filename": "sample.kml",
  "feature_count": 3,
  "crs": "EPSG:4326",
  "status": "COMPLETED"
}
```

The ZIP upload uses the same endpoint:

```bash
curl -X POST "http://127.0.0.1:8000/api/files/" \
  -F "file=@path/to/shapefile_bundle.zip"
```

The archive should contain the Shapefile components together. A `.prj` file is strongly recommended because it defines the source coordinate reference system; uploads without CRS metadata are rejected rather than silently assigning a potentially incorrect CRS.

### Get file information

```http
GET /api/files/{id}/
```

Example response:

```json
{
  "id": "a1b2c3d4e5f6",
  "filename": "sample.kml",
  "feature_count": 3,
  "crs": "EPSG:4326",
  "status": "COMPLETED",
  "created_at": "2026-01-01T12:00:00+00:00"
}
```

### Get measurements

```http
GET /api/files/{id}/measurements/
```

Example response (numeric measurements depend on the input geometry):

```json
{
  "file_id": "a1b2c3d4e5f6",
  "filename": "sample.kml",
  "crs": "EPSG:4326",
  "feature_count": 3,
  "measurements": [
    {
      "feature_id": 0,
      "geometry_type": "Polygon",
      "value": 12000.0,
      "unit": "m²",
      "status": "COMPLETED",
      "method": "UTM (EPSG:32643)"
    },
    {
      "feature_id": 1,
      "geometry_type": "LineString",
      "value": 155.0,
      "unit": "m",
      "status": "COMPLETED",
      "method": "UTM (EPSG:32643)"
    },
    {
      "feature_id": 2,
      "geometry_type": "Point",
      "value": null,
      "unit": null,
      "status": "NOT_APPLICABLE"
    }
  ]
}
```

The values above are illustrative; the API returns measurements calculated from the uploaded geometries.

### Get features

```http
GET /api/files/{id}/features/?limit=100&offset=0
```

The response includes each feature's index, geometry type, GeoJSON geometry, source CRS, properties, and measurement status/value. `limit` must be between 1 and 1000.

### Other endpoints

- `GET /health` — basic health check.
- `GET /docs` — interactive Swagger UI.
- `GET /openapi.json` — generated OpenAPI specification.

## Architecture

```text
Client
  |
  | multipart/form-data upload
  v
FastAPI routes (app/main.py)
  |
  +--> Validate extension and upload size
  |
  +--> KML XML parser OR GeoPandas Shapefile reader
  |
  +--> Identify source CRS and iterate over features
  |
  +--> Project measurable geometry into local UTM
  |       +--> Polygon: area in m²
  |       +--> Line: length in m
  |       +--> Point/unsupported/invalid: explicit status
  |
  +--> Persist file metadata and feature JSON in SQLite
  |
  v
JSON API responses
```

- **Application structure:** `app/main.py` contains API routes, input parsing, CRS-aware measurement logic, and persistence. `examples/` contains a small KML fixture. `tests/` contains API tests.
- **File processing:** uploads are streamed to disk with a configurable size limit. KML is parsed as XML for common geometry types; ZIP archives are checked for a Shapefile and extracted to a temporary directory for GeoPandas reading.
- **Measurement flow:** each feature is handled independently. Valid polygon and line geometries are transformed before measuring; invalid, empty, and unsupported geometries receive a status and explanatory reason.
- **CRS handling:** KML coordinates are interpreted as EPSG:4326. Shapefiles must include CRS metadata. The geometry is transformed to geographic coordinates if necessary to identify a local UTM zone, then projected to that UTM CRS before calculating area or length. This avoids measuring geographic coordinates directly in degrees. UTM is a practical choice for local/regional datasets; very large, polar, or cross-zone geometries may need a dataset-specific equal-area or geodesic strategy.

## Design decisions and trade-offs

1. **FastAPI:** chosen for typed, concise endpoints and automatic OpenAPI documentation. Django REST Framework would also be suitable for a larger application with richer admin and user-management needs.
2. **GeoPandas + Shapely + pyproj:** GeoPandas handles Shapefile reading, Shapely represents and validates geometries, and pyproj performs coordinate transformations.
3. **KML XML parsing:** the included parser supports common Point, LineString, Polygon, and MultiGeometry placemarks without depending on whether a platform's GDAL build enables the KML driver. Complex KML features outside these common structures may require a dedicated KML library.
4. **Local UTM projection:** UTM provides metre-based measurements for local/regional features. A production geospatial platform could choose a suitable CRS per dataset, use geodesic calculations, or split geometries spanning multiple zones.
5. **SQLite:** keeps setup lightweight and persistent without requiring a separate database service. PostgreSQL/PostGIS is a natural next step for larger datasets, spatial queries, and concurrent workloads.
6. **Feature-level status:** one invalid or unsupported geometry should not prevent other features from being returned. Input-level failures, such as an unreadable archive or missing Shapefile CRS, return clear HTTP errors.
7. **Upload safety:** filenames are normalized, uploads are size-limited, ZIP paths are checked against traversal, and ZIP contents are extracted only into a temporary directory. For production deployment, add authentication, rate limiting, malware scanning, stricter archive expansion limits, structured logging, and background processing for large files.

## Tests

Run:

```bash
pytest -q
```

The tests cover the health endpoint, KML upload and measurement of polygon/line/point features, and rejection of unsupported extensions.

## Learning and future scope

This project brings together REST API design, file validation, geospatial formats, geometry processing, coordinate reference systems, persistence, and automated tests. Key learning points include why measurements should not be calculated directly from longitude/latitude degrees and how a feature-level status can make a data-processing API more resilient.

Possible improvements:

- Add authentication, authorization, rate limits, and per-user file ownership.
- Move large uploads and calculations to a background worker and expose processing states.
- Support GeoJSON and GeoPackage, more KML variants, and richer validation reports.
- Add configurable measurement CRS strategies and geodesic measurements for global or cross-zone geometries.
- Use PostGIS for spatial indexing, filtering, and larger-scale datasets.
- Add structured logs, metrics, CI checks, and deployment configuration (Docker and a cloud hosting target).
- Add retention policies and controls for deleting uploaded files and associated features.

## Repository submission checklist

- [ ] Create a public GitHub repository.
- [ ] Upload the project files, including this README and `requirements.txt`.
- [ ] Do not commit `.env`, uploaded private datasets, or the generated `data/` database.
- [ ] Run the service and `pytest -q` locally before submitting.
- [ ] Update the repository description and verify that the README setup steps work on a clean environment.
