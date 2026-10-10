"""FastAPI application for uploading and measuring KML and zipped Shapefiles."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import geopandas as gpd
from fastapi import FastAPI, File, HTTPException, UploadFile, status
from fastapi.responses import JSONResponse
from pyproj import CRS, Transformer
from shapely.geometry import shape, mapping
from shapely.ops import transform as transform_geometry
from shapely.validation import explain_validity

APP_NAME = "Geospatial File Measurement API"
DATA_DIR = Path(os.getenv("GEO_API_DATA_DIR", "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "geospatial.sqlite3"
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
ALLOWED_EXTENSIONS = {".kml", ".zip"}

app = FastAPI(
    title=APP_NAME,
    description=(
        "Upload a KML file or a ZIP archive containing a Shapefile, inspect its "
        "features, and retrieve polygon-area and line-length measurements."
    ),
    version="1.0.0",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_database() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with get_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS uploaded_files (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                crs TEXT NOT NULL,
                feature_count INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS features (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id TEXT NOT NULL,
                feature_index INTEGER NOT NULL,
                geometry_type TEXT NOT NULL,
                geometry_json TEXT NOT NULL,
                properties_json TEXT NOT NULL,
                measurement_json TEXT NOT NULL,
                FOREIGN KEY(file_id) REFERENCES uploaded_files(id),
                UNIQUE(file_id, feature_index)
            );
            CREATE INDEX IF NOT EXISTS idx_features_file_id ON features(file_id);
            """
        )


@app.on_event("startup")
def on_startup() -> None:
    initialize_database()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    return next((node for node in element.iter() if _local_name(node.tag) == name), None)


def _parse_coordinates(text: str | None) -> list[tuple[float, float]]:
    if not text:
        return []
    coordinates: list[tuple[float, float]] = []
    for token in text.strip().split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        coordinates.append((float(parts[0]), float(parts[1])))
    return coordinates


def _parse_kml_geometry(element: ET.Element) -> Any | None:
    """Parse common KML geometry types without requiring a GDAL KML driver."""
    from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPoint, MultiPolygon, Point, Polygon

    for node in element.iter():
        kind = _local_name(node.tag)
        if kind == "Point":
            coords = _parse_coordinates((_child(node, "coordinates").text if _child(node, "coordinates") is not None else None))
            if coords:
                return Point(coords[0])
        elif kind == "LineString":
            coords_node = _child(node, "coordinates")
            coords = _parse_coordinates(coords_node.text if coords_node is not None else None)
            if len(coords) >= 2:
                return LineString(coords)
        elif kind == "Polygon":
            rings: list[list[tuple[float, float]]] = []
            for boundary in node.iter():
                if _local_name(boundary.tag) == "LinearRing":
                    coords_node = _child(boundary, "coordinates")
                    coords = _parse_coordinates(coords_node.text if coords_node is not None else None)
                    if len(coords) >= 4:
                        rings.append(coords)
            if rings:
                return Polygon(rings[0], rings[1:])
        elif kind == "MultiGeometry":
            geometries = []
            for child in list(node):
                parsed = _parse_kml_geometry(child)
                if parsed is not None:
                    geometries.append(parsed)
            if geometries:
                types = {geometry.geom_type for geometry in geometries}
                try:
                    if types == {"Point"}:
                        return MultiPoint(geometries)
                    if types == {"LineString"}:
                        return MultiLineString(geometries)
                    if types == {"Polygon"}:
                        return MultiPolygon(geometries)
                    return GeometryCollection(geometries)
                except Exception:
                    return GeometryCollection(geometries)
    return None


def read_kml(path: Path) -> gpd.GeoDataFrame:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise ValueError(f"The KML file could not be parsed: {exc}") from exc

    rows: list[dict[str, Any]] = []
    for placemark in (node for node in root.iter() if _local_name(node.tag) == "Placemark"):
        properties: dict[str, Any] = {}
        name_node = _child(placemark, "name")
        description_node = _child(placemark, "description")
        if name_node is not None and name_node.text:
            properties["name"] = name_node.text.strip()
        if description_node is not None and description_node.text:
            properties["description"] = description_node.text.strip()
        for data_node in placemark.iter():
            if _local_name(data_node.tag) == "Data":
                key = data_node.attrib.get("name")
                value_node = _child(data_node, "value")
                if key:
                    properties[key] = value_node.text if value_node is not None else None
            elif _local_name(data_node.tag) == "SimpleData":
                key = data_node.attrib.get("name")
                if key:
                    properties[key] = data_node.text
        geometry = _parse_kml_geometry(placemark)
        if geometry is not None:
            rows.append({"geometry": geometry, **properties})

    if not rows:
        raise ValueError("No supported Point, LineString, or Polygon features were found in the KML file.")
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")


def read_shapefile_zip(path: Path) -> gpd.GeoDataFrame:
    if not zipfile.is_zipfile(path):
        raise ValueError("The .zip upload is not a valid ZIP archive.")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        shapefiles = [name for name in names if name.lower().endswith(".shp") and not name.startswith("__MACOSX/")]
        if not shapefiles:
            raise ValueError("The ZIP archive must contain a .shp file and its associated Shapefile components.")
        # Reject path traversal entries before extraction.
        for name in names:
            target = (UPLOAD_DIR / "extract-check" / name).resolve()
            base = (UPLOAD_DIR / "extract-check").resolve()
            if target != base and base not in target.parents:
                raise ValueError("The ZIP archive contains an unsafe file path.")
        with tempfile.TemporaryDirectory(prefix="shapefile-") as temporary_dir:
            archive.extractall(temporary_dir)
            shp_path = Path(temporary_dir) / shapefiles[0]
            try:
                frame = gpd.read_file(shp_path)
            except Exception as exc:
                raise ValueError(f"Could not read the Shapefile: {exc}") from exc
    if frame.crs is None:
        raise ValueError("The Shapefile has no CRS metadata (.prj). Please provide a Shapefile with a defined CRS.")
    if frame.empty:
        raise ValueError("The uploaded Shapefile contains no features.")
    return frame


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    try:
        # pandas/numpy scalar types expose item().
        if hasattr(value, "item"):
            value = value.item()
    except (ValueError, TypeError):
        pass
    if isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            return None
        return value
    if isinstance(value, (datetime,)):
        return value.isoformat()
    return str(value)


def _measurement(geometry: Any, source_crs: CRS) -> dict[str, Any]:
    """Measure in metres/m² after projecting into a local UTM CRS when possible."""
    result: dict[str, Any] = {"value": None, "unit": None, "status": "NOT_APPLICABLE"}
    if geometry is None or geometry.is_empty:
        result.update(status="SKIPPED", reason="Geometry is empty or missing.")
        return result
    geometry_type = geometry.geom_type
    if geometry_type in {"Point", "MultiPoint"}:
        return result
    if geometry_type not in {"Polygon", "MultiPolygon", "LineString", "MultiLineString", "LinearRing"}:
        result.update(status="UNSUPPORTED", reason=f"Measurement is not defined for {geometry_type} geometries.")
        return result
    if not geometry.is_valid:
        result.update(status="SKIPPED", reason=f"Invalid geometry: {explain_validity(geometry)}")
        return result
    try:
        geographic = geometry if source_crs.is_geographic else transform_geometry(
            Transformer.from_crs(source_crs, "EPSG:4326", always_xy=True).transform, geometry
        )
        centroid = geographic.representative_point()
        longitude, latitude = centroid.x, centroid.y
        if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
            raise ValueError("Geometry coordinates are outside valid longitude/latitude bounds.")
        # UTM is suitable for local/regional data. Clamp the zone at the valid range.
        zone = max(1, min(60, int((longitude + 180) // 6) + 1))
        epsg = (32600 if latitude >= 0 else 32700) + zone
        transformer = Transformer.from_crs(source_crs, CRS.from_epsg(epsg), always_xy=True)
        projected = transform_geometry(transformer.transform, geometry)
        if geometry_type in {"Polygon", "MultiPolygon"}:
            result.update(value=float(projected.area), unit="m²", status="COMPLETED", method=f"UTM (EPSG:{epsg})")
        else:
            result.update(value=float(projected.length), unit="m", status="COMPLETED", method=f"UTM (EPSG:{epsg})")
    except Exception as exc:
        result.update(status="SKIPPED", reason=f"Measurement could not be calculated: {exc}")
    return result


def process_geospatial_file(path: Path, extension: str) -> tuple[gpd.GeoDataFrame, CRS]:
    if extension == ".kml":
        frame = read_kml(path)
    else:
        frame = read_shapefile_zip(path)
    if frame.crs is None:
        # KML is explicitly WGS84; Shapefiles without CRS are rejected above.
        frame = frame.set_crs("EPSG:4326")
    try:
        source_crs = CRS.from_user_input(frame.crs)
    except Exception as exc:
        raise ValueError(f"Could not interpret the file's coordinate reference system: {exc}") from exc
    return frame, source_crs


def save_processed_file(file_id: str, filename: str, frame: gpd.GeoDataFrame, source_crs: CRS) -> dict[str, Any]:
    prepared: list[tuple[int, str, str, str, str]] = []
    for index, row in frame.iterrows():
        geometry = row.geometry
        if geometry is None:
            geometry_type = "Unknown"
            geometry_json = "null"
            measure = {"value": None, "unit": None, "status": "SKIPPED", "reason": "Geometry is missing."}
        else:
            geometry_type = geometry.geom_type
            geometry_json = json.dumps(mapping(geometry), separators=(",", ":"))
            measure = _measurement(geometry, source_crs)
        properties = {str(key): _json_safe(value) for key, value in row.items() if key != frame.geometry.name}
        prepared.append((int(index), geometry_type, geometry_json, json.dumps(properties), json.dumps(measure)))

    with get_connection() as connection:
        connection.execute(
            "INSERT INTO uploaded_files (id, filename, crs, feature_count, status, created_at, error) VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (file_id, filename, source_crs.to_string(), len(prepared), "COMPLETED", utc_now()),
        )
        connection.executemany(
            "INSERT INTO features (file_id, feature_index, geometry_type, geometry_json, properties_json, measurement_json) VALUES (?, ?, ?, ?, ?, ?)",
            [(file_id, *feature) for feature in prepared],
        )
    return {
        "id": file_id,
        "filename": filename,
        "feature_count": len(prepared),
        "crs": source_crs.to_string(),
        "status": "COMPLETED",
    }


@app.get("/", tags=["Health"])
def root() -> dict[str, str]:
    return {"name": APP_NAME, "status": "ok", "docs": "/docs"}


@app.get("/health", tags=["Health"])
def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.post("/api/files/", status_code=status.HTTP_201_CREATED, tags=["Files"])
async def upload_file(file: UploadFile = File(...)) -> dict[str, Any]:
    filename = Path(file.filename or "upload").name
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported file type. Upload a .kml file or a .zip containing a Shapefile.")

    file_id = uuid.uuid4().hex[:12]
    saved_path = UPLOAD_DIR / f"{file_id}{extension}"
    size = 0
    try:
        with saved_path.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail=f"File exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit.")
                output.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="The uploaded file is empty.")
        frame, source_crs = process_geospatial_file(saved_path, extension)
        return save_processed_file(file_id, filename, frame, source_crs)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        # Avoid exposing server internals to clients; details are logged by the ASGI server if configured.
        raise HTTPException(status_code=422, detail=f"Unable to process this geospatial file: {exc}") from exc
    finally:
        await file.close()
        # Retain uploaded source files for debugging/reproducibility only when processing succeeded.
        with get_connection() as connection:
            exists = connection.execute("SELECT 1 FROM uploaded_files WHERE id = ?", (file_id,)).fetchone()
        if not exists:
            saved_path.unlink(missing_ok=True)


def _get_file_or_404(file_id: str) -> sqlite3.Row:
    with get_connection() as connection:
        row = connection.execute("SELECT * FROM uploaded_files WHERE id = ?", (file_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="File not found.")
    return row


@app.get("/api/files/{file_id}/", tags=["Files"])
def get_file_info(file_id: str) -> dict[str, Any]:
    row = _get_file_or_404(file_id)
    return {
        "id": row["id"],
        "filename": row["filename"],
        "feature_count": row["feature_count"],
        "crs": row["crs"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


@app.get("/api/files/{file_id}/features/", tags=["Features"])
def get_features(file_id: str, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    _get_file_or_404(file_id)
    if limit < 1 or limit > 1000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 1000.")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be zero or greater.")
    with get_connection() as connection:
        total = connection.execute("SELECT COUNT(*) FROM features WHERE file_id = ?", (file_id,)).fetchone()[0]
        rows = connection.execute(
            "SELECT feature_index, geometry_type, geometry_json, properties_json, measurement_json FROM features WHERE file_id = ? ORDER BY feature_index LIMIT ? OFFSET ?",
            (file_id, limit, offset),
        ).fetchall()
    return {
        "file_id": file_id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "features": [
            {
                "feature_id": row["feature_index"],
                "geometry_type": row["geometry_type"],
                "geometry": json.loads(row["geometry_json"]),
                "crs": _get_file_or_404(file_id)["crs"],
                "properties": json.loads(row["properties_json"]),
                "measurement": json.loads(row["measurement_json"]),
            }
            for row in rows
        ],
    }


@app.get("/api/files/{file_id}/measurements/", tags=["Measurements"])
def get_measurements(file_id: str) -> dict[str, Any]:
    file_row = _get_file_or_404(file_id)
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT feature_index, geometry_type, measurement_json FROM features WHERE file_id = ? ORDER BY feature_index",
            (file_id,),
        ).fetchall()
    return {
        "file_id": file_id,
        "filename": file_row["filename"],
        "crs": file_row["crs"],
        "feature_count": file_row["feature_count"],
        "measurements": [
            {
                "feature_id": row["feature_index"],
                "geometry_type": row["geometry_type"],
                **json.loads(row["measurement_json"]),
            }
            for row in rows
        ],
    }


@app.exception_handler(sqlite3.Error)
async def database_error_handler(_request: Any, _exc: sqlite3.Error) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": "A database error occurred."})
