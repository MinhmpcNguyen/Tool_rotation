from __future__ import annotations

import argparse
import html
import json
import math
import mimetypes
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, unquote

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8790
DEFAULT_GLB_PATH = Path(__file__).resolve().parent / "demo_inventory"
VENDOR_DIR = Path(__file__).resolve().parent / "frontend" / "src" / "vendor" / "three"
BASIS_DIR = (
    Path(__file__).resolve().parent
    / "frontend"
    / "public"
    / "assets"
    / "three"
    / "basis"
)
DRACO_DIR = (
    Path(__file__).resolve().parent
    / "frontend"
    / "public"
    / "assets"
    / "three"
    / "draco"
)
MODEL_FRONT_AXIS_BY_OFFSET = {
    0: "-z",
    90: "+x",
    180: "+z",
    270: "-x",
}
OFFSET_LABELS = {
    0: "Front is -Z",
    90: "Front is +X",
    180: "Front is +Z",
    270: "Front is -X",
}

CATALOG_API_BASE = "https://auto-furniture-api2.a-star.group"
STORAGE_BASE = "https://storage.mazig.io"
CATALOG_DOWNLOADS_DIR = Path(__file__).resolve().parent / "catalog_downloads"
CATALOG_SESSION_FILE = Path(__file__).resolve().parent / "catalog_session.json"
CATALOG_TOOL_USER_AGENT = "GLB-Orientation-Tool/1.0"
NO_GLB_LOADED_MESSAGE = "No GLB files are loaded. In catalog mode, search and download a catalog item first."


# ── math helpers ─────────────────────────────────────────────────────────────


def deg_to_quaternion_y(deg: float) -> dict[str, float]:
    """Convert a Y-axis CCW rotation (degrees) to a Three.js quaternion [x,y,z,w]."""
    rad = math.radians(deg)
    return {
        "x": 0.0,
        "y": round(math.sin(rad / 2), 10),
        "z": 0.0,
        "w": round(math.cos(rad / 2), 10),
    }


def multiply_quaternions(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    """Quaternion multiply a × b (Three.js convention: apply b first, then a)."""
    ax, ay, az, aw = a["x"], a["y"], a["z"], a["w"]
    bx, by, bz, bw = b["x"], b["y"], b["z"], b["w"]
    return {
        "x": aw * bx + ax * bw + ay * bz - az * by,
        "y": aw * by - ax * bz + ay * bw + az * bx,
        "z": aw * bz + ax * by - ay * bx + az * bw,
        "w": aw * bw - ax * bx - ay * by - az * bz,
    }


def normalize_quaternion(q: dict[str, float]) -> dict[str, float]:
    norm = math.sqrt(q["x"] ** 2 + q["y"] ** 2 + q["z"] ** 2 + q["w"] ** 2)
    if norm < 1e-9:
        return {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}
    return {k: round(v / norm, 10) for k, v in q.items()}


def quaternion_yaw_deg(q: dict[str, float]) -> float:
    """Extract the Y-axis rotation angle in degrees from a quaternion."""
    yaw_rad = 2 * math.atan2(q["y"], q["w"])
    return math.degrees(yaw_rad) % 360


# ── normalisation helpers ─────────────────────────────────────────────────────


def escape(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def normalize_cardinal_offset(value: float) -> int:
    normalized = int(round(value / 90.0) * 90) % 360
    if normalized not in MODEL_FRONT_AXIS_BY_OFFSET:
        raise ValueError("Rotation offset must be one of 0, 90, 180, or 270 degrees.")
    return normalized


def normalize_quarter_turn_tilt(value: float) -> int:
    normalized = int(round(value / 90.0) * 90) % 360
    if normalized == 270:
        return -90
    if normalized in {0, 90, 180}:
        return normalized
    raise ValueError("Tilt correction must be one of -90, 0, 90, or 180 degrees.")


def quat_to_list(q: dict[str, float]) -> list[float]:
    """Convert quaternion dict {x,y,z,w} to list [x,y,z,w] for API output."""
    return [round(q["x"], 10), round(q["y"], 10), round(q["z"], 10), round(q["w"], 10)]


def quaternion_display(value: object) -> str:
    if isinstance(value, dict):
        components = [value.get("x"), value.get("y"), value.get("z"), value.get("w")]
    elif isinstance(value, (list, tuple)):
        components = list(value[:4])
    else:
        components = []

    numbers: list[float] = []
    for component in components:
        if isinstance(component, bool):
            break
        if isinstance(component, (int, float)):
            numbers.append(round(float(component), 10))
            continue
        if isinstance(component, str) and component.strip():
            try:
                numbers.append(round(float(component), 10))
            except ValueError:
                break

    if len(numbers) != 4:
        return "x=?, y=?, z=?, w=?"
    return f"x={numbers[0]}, y={numbers[1]}, z={numbers[2]}, w={numbers[3]}"


def selected_offset(raw_offset: str | None) -> int:
    if raw_offset:
        try:
            return normalize_cardinal_offset(float(raw_offset))
        except ValueError:
            return 0
    return 0


def selected_tilt(raw_tilt: str | None) -> int:
    if raw_tilt:
        try:
            return normalize_quarter_turn_tilt(float(raw_tilt))
        except ValueError:
            return 0
    return 0


def adjusted_tilt(value: int, delta: int) -> int:
    return normalize_quarter_turn_tilt(float(value + delta))


# ── file helpers ──────────────────────────────────────────────────────────────


def orientation_sidecar_path(glb_path: Path) -> Path:
    return Path(f"{glb_path}.orientation.json")


def discover_glb_paths(input_path: Path) -> list[Path]:
    if input_path.is_file():
        if input_path.suffix.lower() != ".glb":
            raise ValueError(f"Expected a .glb file, got: {input_path}")
        return [input_path.resolve()]
    if input_path.is_dir():
        paths = sorted(path.resolve() for path in input_path.glob("*.glb"))
        if paths:
            return paths
        raise ValueError(f"No .glb files found in directory: {input_path}")
    raise ValueError(f"GLB file or directory not found: {input_path}")


# ── catalog session helpers ───────────────────────────────────────────────────


def load_catalog_session() -> dict[str, dict]:
    """Returns {catalog_item_id: {id, nameVn, modelUrl, glb_filename}} from session file."""
    if not CATALOG_SESSION_FILE.is_file():
        return {}
    try:
        data = json.loads(CATALOG_SESSION_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_catalog_session(session: dict) -> None:
    temp = CATALOG_SESSION_FILE.with_suffix(".json.tmp")
    temp.write_text(
        json.dumps(session, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temp.replace(CATALOG_SESSION_FILE)


def catalog_glb_path(catalog_item_id: str) -> Path:
    return CATALOG_DOWNLOADS_DIR / f"{catalog_item_id}.glb"


def catalog_orientation_path(catalog_item_id: str) -> Path:
    return Path(f"{catalog_glb_path(catalog_item_id)}.orientation.json")


def selected_glb_path(
    *,
    params: dict[str, list[str]],
    glb_paths: list[Path],
) -> Path:
    if not glb_paths:
        raise ValueError(NO_GLB_LOADED_MESSAGE)
    raw_file = params.get("file", [""])[0]
    if raw_file:
        requested_name = Path(raw_file).name
        for glb_path in glb_paths:
            if raw_file == str(glb_path) or requested_name == glb_path.name:
                return glb_path
    return glb_paths[0]


def selected_glb_index(*, glb_path: Path, glb_paths: list[Path]) -> int:
    for index, candidate in enumerate(glb_paths):
        if candidate == glb_path:
            return index
    return 0


def read_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number == number else None
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def read_object(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {str(key): item for key, item in value.items()}


def load_saved_orientation(orientation_path: Path) -> dict[str, object] | None:
    if not orientation_path.is_file():
        return None
    try:
        payload = json.loads(orientation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return read_object(payload)


def saved_offset(payload: dict[str, object] | None) -> int:
    if payload is None:
        return 0
    override = read_object(payload.get("preview_override"))
    raw_value = (
        read_number((override or {}).get("rotation_deg_offset"))
        or read_number(payload.get("rotation_deg_offset"))
        or 0.0
    )
    return normalize_cardinal_offset(raw_value)


def saved_tilt(payload: dict[str, object] | None, *, axis: str) -> int:
    if payload is None:
        return 0
    override = read_object(payload.get("preview_override"))
    key = f"rotation_deg_{axis}"
    raw_value = (
        read_number((override or {}).get(key)) or read_number(payload.get(key)) or 0.0
    )
    return normalize_quarter_turn_tilt(raw_value)


def resolve_orientation(
    *,
    params: dict[str, list[str]],
    saved_payload: dict[str, object] | None,
) -> tuple[int, int, int]:
    raw_offset = params.get("offset", [None])[0]
    raw_tilt_x = params.get("tilt_x", [None])[0]
    raw_tilt_z = params.get("tilt_z", [None])[0]
    offset = (
        selected_offset(raw_offset)
        if raw_offset is not None
        else saved_offset(saved_payload)
    )
    tilt_x = (
        selected_tilt(raw_tilt_x)
        if raw_tilt_x is not None
        else saved_tilt(saved_payload, axis="x")
    )
    tilt_z = (
        selected_tilt(raw_tilt_z)
        if raw_tilt_z is not None
        else saved_tilt(saved_payload, axis="z")
    )
    return offset, tilt_x, tilt_z


# ── result.json helpers ────────────────────────────────────────────────────────


def load_result_json(result_path: Path) -> list[dict]:
    """Load objects list from a normalize-run result.json."""
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        objects = data.get("objects", [])
        if not isinstance(objects, list):
            return []
        return [o for o in objects if isinstance(o, dict) and o.get("modelUrl")]
    return []


def result_object_id(index: int, obj: dict) -> str:
    name = obj.get("name") or obj.get("catalogItemId") or f"obj_{index}"
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(name))
    return f"{index}_{safe[:40]}"


def parse_quat_params(params: dict[str, list[str]]) -> dict[str, float] | None:
    """Parse qx/qy/qz/qw from query params; return None if missing or identity."""
    try:
        qx = float(params.get("qx", ["0"])[0])
        qy = float(params.get("qy", ["0"])[0])
        qz = float(params.get("qz", ["0"])[0])
        qw = float(params.get("qw", ["1"])[0])
    except (ValueError, IndexError):
        return None
    # identity quaternion → no base rotation
    if abs(qx) < 1e-9 and abs(qy) < 1e-9 and abs(qz) < 1e-9 and abs(qw - 1) < 1e-9:
        return None
    return {"x": qx, "y": qy, "z": qz, "w": qw}


# ── payload builders ──────────────────────────────────────────────────────────


def build_preview_payload(
    *,
    glb_path: Path,
    offset: int,
    tilt_x: int,
    tilt_z: int,
) -> dict[str, object]:
    preview_override = {
        "enabled": True,
        "contexts": ["panel"],
        "rotation_deg_offset": float(offset),
        "rotation_deg_x": float(tilt_x),
        "rotation_deg_z": float(tilt_z),
        "notes": "Manual local GLB orientation review.",
    }
    quat = deg_to_quaternion_y(float(offset))
    quat_list = quat_to_list(quat)
    orientation_review = {
        "version": 1,
        "status": "reviewed",
        "front_axis": MODEL_FRONT_AXIS_BY_OFFSET[offset],
        "rotation_deg_offset": float(offset),
        "rotation_deg_x": float(tilt_x),
        "rotation_deg_z": float(tilt_z),
        "quaternion": quat_list,
        "reference_front": "2D plan +Y / 3D scene -Z",
        "contexts": ["panel"],
        "notes": "Manual local GLB orientation review.",
    }
    return {
        "source_file": str(glb_path),
        "file_name": glb_path.name,
        "front_axis": MODEL_FRONT_AXIS_BY_OFFSET[offset],
        "rotation_deg_offset": float(offset),
        "rotation_deg_x": float(tilt_x),
        "rotation_deg_z": float(tilt_z),
        "quaternion": quat_list,
        "preview_override": preview_override,
        "orientation_review": orientation_review,
    }


def build_result_payload(
    *,
    obj: dict,
    obj_id: str,
    base_quat: dict[str, float],
    offset: int,
    tilt_x: int,
    tilt_z: int,
) -> dict[str, object]:
    """Build a review payload for a result.json object."""
    offset_quat = deg_to_quaternion_y(float(offset))
    # corrected = base × offset  (apply offset correction on top of result rotation)
    corrected_quat = normalize_quaternion(multiply_quaternions(base_quat, offset_quat))
    # delta shows the correction only — what to right-multiply into catalog defaultRotation
    correction_quat = offset_quat  # R_default_new = R_default_old × correction
    return {
        "object_id": obj_id,
        "name": obj.get("name"),
        "catalog_item_id": obj.get("catalogItemId"),
        "model_url": obj.get("modelUrl"),
        "base_rotation": quat_to_list(base_quat),
        "correction_offset_deg": float(offset),
        "correction_quaternion": quat_to_list(correction_quat),
        "corrected_rotation": quat_to_list(corrected_quat),
        "tilt_x_deg": float(tilt_x),
        "tilt_z_deg": float(tilt_z),
        "note": (
            "corrected_rotation is the final quaternion to use for this object. "
            "correction_quaternion is what to multiply into catalog defaultRotation."
        ),
    }


# ── catalog search panel HTML (injected into local index in catalog mode) ─────


def _build_catalog_search_panel() -> str:
    return """
<strong style="font-size:13px;">🔍 Tìm đồ trong catalog</strong>
<div style="margin-top:8px;display:flex;gap:6px;align-items:center;">
  <input id="cat-search" class="search-input" placeholder="Tên đồ vật..." style="flex:1;">
  <label style="font-size:12px;white-space:nowrap;">
    <input type="checkbox" id="cat-null-only" checked> Null only
  </label>
  <button onclick="doCatalogSearch()" style="padding:7px 10px;border:0;border-radius:6px;background:#176b32;color:#fff;cursor:pointer;font-size:12px;">Tìm</button>
</div>
<div id="cat-results" class="search-results" style="display:none;"></div>
<div id="cat-download-bar" style="display:none;margin-top:8px;">
  <button onclick="downloadSelected()" style="width:100%;padding:9px;border:0;border-radius:6px;background:#0369a1;color:#fff;font-weight:700;cursor:pointer;font-size:13px;">
    Tải GLB đã chọn (<span id="cat-sel-count">0</span>)
  </button>
</div>
<script>
  let _catalogItems = [];
  let _selectedIds = new Set();

  async function doCatalogSearch() {
    const search = document.getElementById('cat-search').value.trim();
    const nullOnly = document.getElementById('cat-null-only').checked;
    let url = '/catalog-api?limit=50&offset=0';
    if (search) url += '&search=' + encodeURIComponent(search);
    if (nullOnly) url += '&defaultRotationPresence=null';
    const box = document.getElementById('cat-results');
    box.style.display = 'block';
    box.innerHTML = '<div style="padding:10px;color:#666;">Đang tìm...</div>';
    try {
      const resp = await fetch(url);
      const contentType = resp.headers.get('content-type') || '';
      const data = contentType.includes('application/json')
        ? await resp.json()
        : { error: await resp.text() };
      if (!resp.ok || data.error) {
        throw new Error(data.error || `Catalog API returned ${resp.status}`);
      }
      _catalogItems = Array.isArray(data.items) ? data.items : [];
      renderCatalogResults(_catalogItems);
    } catch(e) {
      box.innerHTML = '<div style="padding:10px;color:#b42318;">Lỗi: ' + e.message + '</div>';
    }
  }

  function renderCatalogResults(items) {
    const box = document.getElementById('cat-results');
    if (!items.length) {
      box.innerHTML = '<div style="padding:10px;color:#666;">Không tìm thấy kết quả.</div>';
      return;
    }
    box.innerHTML = items.map(it => {
      const checked = _selectedIds.has(it.id) ? 'checked' : '';
      const dr = it.defaultRotation ? '✅ có rotation' : '⬜ null';
      return `<div class="search-item">
        <input type="checkbox" id="chk-${it.id}" ${checked} onchange="toggleSelect('${it.id}')">
        <label for="chk-${it.id}"><strong>${escHtml(it.nameVn || it.name || it.id)}</strong><br>
        <span style="color:#888;">${dr} · ${escHtml(it.id.slice(0,8))}...</span></label>
      </div>`;
    }).join('');
    updateDownloadBar();
  }

  function toggleSelect(id) {
    if (_selectedIds.has(id)) _selectedIds.delete(id);
    else _selectedIds.add(id);
    updateDownloadBar();
  }

  function updateDownloadBar() {
    const n = _selectedIds.size;
    document.getElementById('cat-sel-count').textContent = n;
    document.getElementById('cat-download-bar').style.display = n > 0 ? 'block' : 'none';
  }

  function downloadSelected() {
    const selected = _catalogItems.filter(it => _selectedIds.has(it.id));
    const form = document.createElement('form');
    form.method = 'post';
    form.action = '/catalog-download';
    const inp = document.createElement('input');
    inp.type = 'hidden';
    inp.name = 'items_json';
    inp.value = JSON.stringify(selected);
    form.appendChild(inp);
    document.body.appendChild(form);
    form.submit();
  }

  function escHtml(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  document.getElementById('cat-search').addEventListener('keydown', e => {
    if (e.key === 'Enter') doCatalogSearch();
  });
</script>"""


# ── HTTP handler ──────────────────────────────────────────────────────────────


class LocalGlbOrientationHandler(BaseHTTPRequestHandler):
    glb_paths: list[Path]
    basis_dir: Path
    draco_dir: Path
    result_objects: list[
        dict
    ]  # empty → local file mode; non-empty → result review mode
    catalog_mode: bool  # True → catalog review mode

    @property
    def _result_mode(self) -> bool:
        return bool(self.result_objects)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path in ("", "/"):
                self._send_html(self._build_index(parsed.query))
                return
            if parsed.path == "/viewer":
                params = parse_qs(parsed.query)
                if (
                    not self._result_mode
                    and not params.get("model_url")
                    and not self.glb_paths
                ):
                    self._send_plain(NO_GLB_LOADED_MESSAGE, status=HTTPStatus.NOT_FOUND)
                    return
                self._send_html(self._build_viewer(parsed.query))
                return
            if parsed.path == "/model":
                if not self.glb_paths:
                    self._send_plain(NO_GLB_LOADED_MESSAGE, status=HTTPStatus.NOT_FOUND)
                    return
                params = parse_qs(parsed.query)
                glb_path = selected_glb_path(params=params, glb_paths=self.glb_paths)
                self._send_file(glb_path, default_content_type="model/gltf-binary")
                return
            if parsed.path == "/proxy-glb":
                params = parse_qs(parsed.query)
                raw_url = params.get("url", [""])[0]
                if not raw_url:
                    self._send_plain(
                        "Missing url param.", status=HTTPStatus.BAD_REQUEST
                    )
                    return
                self._proxy_glb(unquote(raw_url))
                return
            if parsed.path == "/catalog-api":
                self._handle_catalog_api_search(parsed.query)
                return
            if parsed.path == "/catalog-status":
                self._handle_catalog_status()
                return
            if parsed.path == "/export.json":
                self._send_json(self._build_export_payload(parsed.query))
                return
            if parsed.path.startswith("/vendor/"):
                self._send_vendor_file(parsed.path.removeprefix("/vendor/"))
                return
            if parsed.path.startswith("/basis/"):
                self._send_basis_file(parsed.path.removeprefix("/basis/"))
                return
            if parsed.path.startswith("/draco/"):
                self._send_draco_file(parsed.path.removeprefix("/draco/"))
                return
            self._send_plain("Not found.", status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_plain(str(exc), status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/save":
                length_raw = self.headers.get("Content-Length")
                length = int(length_raw) if length_raw and length_raw.isdigit() else 0
                body = self.rfile.read(length).decode("utf-8")
                params = parse_qs(body)

                if self._result_mode:
                    self._handle_result_save(params)
                else:
                    if not self.glb_paths:
                        self._send_plain(
                            NO_GLB_LOADED_MESSAGE, status=HTTPStatus.BAD_REQUEST
                        )
                        return
                    glb_path = selected_glb_path(
                        params=params, glb_paths=self.glb_paths
                    )
                    orientation_path = orientation_sidecar_path(glb_path)
                    offset, tilt_x, tilt_z = resolve_orientation(
                        params=params,
                        saved_payload=load_saved_orientation(orientation_path),
                    )
                    payload = build_preview_payload(
                        glb_path=glb_path,
                        offset=offset,
                        tilt_x=tilt_x,
                        tilt_z=tilt_z,
                    )
                    payload["saved_at_utc"] = datetime.now(timezone.utc).isoformat()
                    orientation_path.write_text(
                        json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
                        encoding="utf-8",
                    )
                    query = urlencode(
                        {
                            "file": glb_path.name,
                            "offset": str(offset),
                            "tilt_x": str(tilt_x),
                            "tilt_z": str(tilt_z),
                            "saved": "1",
                        }
                    )
                    self.send_response(int(HTTPStatus.SEE_OTHER))
                    self.send_header("Location", f"/?{query}")
                    self.end_headers()
                return

            if parsed.path == "/reset-saved":
                length_raw = self.headers.get("Content-Length")
                length = int(length_raw) if length_raw and length_raw.isdigit() else 0
                body = self.rfile.read(length).decode("utf-8")
                params = parse_qs(body)
                if not self.glb_paths:
                    self._send_plain(
                        NO_GLB_LOADED_MESSAGE, status=HTTPStatus.BAD_REQUEST
                    )
                    return
                glb_path = selected_glb_path(params=params, glb_paths=self.glb_paths)
                orientation_path = orientation_sidecar_path(glb_path)
                if orientation_path.exists():
                    orientation_path.unlink()
                self.send_response(int(HTTPStatus.SEE_OTHER))
                reset_query = urlencode({"file": glb_path.name, "reset": "1"})
                self.send_header("Location", f"/?{reset_query}")
                self.end_headers()
                return

            if parsed.path == "/catalog-download":
                length_raw = self.headers.get("Content-Length")
                length = int(length_raw) if length_raw and length_raw.isdigit() else 0
                body = self.rfile.read(length).decode("utf-8")
                params = parse_qs(body)
                self._handle_catalog_download(params)
                return

            if parsed.path == "/catalog-send":
                self._handle_catalog_send()
                return

            self._send_plain("Not found.", status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_plain(str(exc), status=HTTPStatus.BAD_REQUEST)

    def _handle_result_save(self, params: dict[str, list[str]]) -> None:
        obj_id = params.get("obj_id", [""])[0]
        offset, tilt_x, tilt_z = resolve_orientation(params=params, saved_payload=None)
        base_quat = self._base_quat_from_params(params)
        # find object
        obj = self._find_result_obj(obj_id)
        if obj is None:
            obj = self.result_objects[0] if self.result_objects else {}

        payload = build_result_payload(
            obj=obj,
            obj_id=obj_id,
            base_quat=base_quat,
            offset=offset,
            tilt_x=tilt_x,
            tilt_z=tilt_z,
        )
        payload["saved_at_utc"] = datetime.now(timezone.utc).isoformat()

        # Save sidecar next to the result file OR in CWD
        sidecar_name = f"orientation_correction_{obj_id}.json"
        sidecar_path = Path(sidecar_name)
        sidecar_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"[tool] Saved correction → {sidecar_path.resolve()}")
        query = urlencode(
            {
                "obj_id": obj_id,
                "offset": str(offset),
                "tilt_x": str(tilt_x),
                "tilt_z": str(tilt_z),
                "qx": str(base_quat["x"]),
                "qy": str(base_quat["y"]),
                "qz": str(base_quat["z"]),
                "qw": str(base_quat["w"]),
                "saved": "1",
            }
        )
        self.send_response(int(HTTPStatus.SEE_OTHER))
        self.send_header("Location", f"/?{query}")
        self.end_headers()

    def _find_result_obj(self, obj_id: str) -> dict | None:
        for i, obj in enumerate(self.result_objects):
            if result_object_id(i, obj) == obj_id:
                return obj
        return None

    @staticmethod
    def _base_quat_from_params(params: dict[str, list[str]]) -> dict[str, float]:
        try:
            return {
                "x": float(params.get("qx", ["0"])[0]),
                "y": float(params.get("qy", ["0"])[0]),
                "z": float(params.get("qz", ["0"])[0]),
                "w": float(params.get("qw", ["1"])[0]),
            }
        except (ValueError, IndexError):
            return {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}

    def log_message(self, format: str, *args: object) -> None:
        return

    # ── page builders ──────────────────────────────────────────────────────────

    def _build_index(self, query: str) -> bytes:
        if self._result_mode:
            return self._build_result_index(query)
        return self._build_local_index(query)

    def _build_local_index(self, query: str) -> bytes:
        if not self.glb_paths:
            return (
                self._build_empty_catalog_index(query)
                if self.catalog_mode
                else self._build_no_glb_index()
            )

        params = parse_qs(query)
        glb_path = selected_glb_path(params=params, glb_paths=self.glb_paths)
        orientation_path = orientation_sidecar_path(glb_path)
        current_index = selected_glb_index(glb_path=glb_path, glb_paths=self.glb_paths)
        saved_payload = load_saved_orientation(orientation_path)
        offset, tilt_x, tilt_z = resolve_orientation(
            params=params,
            saved_payload=saved_payload,
        )
        payload = build_preview_payload(
            glb_path=glb_path,
            offset=offset,
            tilt_x=tilt_x,
            tilt_z=tilt_z,
        )
        payload["orientation_file"] = str(orientation_path)
        pretty_payload = json.dumps(payload, indent=2, ensure_ascii=True)
        quat_display = quaternion_display(payload.get("quaternion"))
        saved_now = params.get("saved", [""])[0] == "1"
        reset_now = params.get("reset", [""])[0] == "1"
        status_text = (
            "Saved local orientation."
            if saved_now
            else "Cleared saved orientation."
            if reset_now
            else "Loaded saved orientation."
            if saved_payload is not None
            else "No saved orientation yet."
        )
        viewer_src = "/viewer?" + urlencode(
            {
                "file": glb_path.name,
                "offset": str(offset),
                "tilt_x": str(tilt_x),
                "tilt_z": str(tilt_z),
            }
        )
        offset_links = " ".join(
            self._offset_link(
                offset=value,
                file_name=glb_path.name,
                tilt_x=tilt_x,
                tilt_z=tilt_z,
                selected=offset == value,
            )
            for value in OFFSET_LABELS
        )
        tilt_x_links = " ".join(
            (
                self._tilt_link(
                    offset=offset,
                    file_name=glb_path.name,
                    tilt_x=adjusted_tilt(tilt_x, -90),
                    tilt_z=tilt_z,
                    label="X -90",
                ),
                self._tilt_link(
                    offset=offset,
                    file_name=glb_path.name,
                    tilt_x=adjusted_tilt(tilt_x, 90),
                    tilt_z=tilt_z,
                    label="X +90",
                ),
            )
        )
        tilt_z_links = " ".join(
            (
                self._tilt_link(
                    offset=offset,
                    file_name=glb_path.name,
                    tilt_x=tilt_x,
                    tilt_z=adjusted_tilt(tilt_z, -90),
                    label="Z -90",
                ),
                self._tilt_link(
                    offset=offset,
                    file_name=glb_path.name,
                    tilt_x=tilt_x,
                    tilt_z=adjusted_tilt(tilt_z, 90),
                    label="Z +90",
                ),
            )
        )
        tilt_reset_link = self._tilt_link(
            offset=offset,
            file_name=glb_path.name,
            tilt_x=0,
            tilt_z=0,
            label="Reset tilt",
        )
        file_links = "\n".join(
            self._file_link(
                glb_path=candidate,
                selected=candidate == glb_path,
                index=index,
                total=len(self.glb_paths),
                display_name=self._catalog_name(candidate)
                if self.catalog_mode
                else None,
            )
            for index, candidate in enumerate(self.glb_paths)
        )
        document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Local GLB Orientation Review</title>
  <style>
    body {{ margin: 0; font: 14px system-ui, sans-serif; color: #111; background: #f6f6f4; }}
    .layout {{ display: grid; grid-template-columns: 420px 1fr; height: 100vh; }}
    aside {{ padding: 14px; border-right: 1px solid #ccc; overflow: auto; background: #fff; }}
    main {{ display: grid; grid-template-rows: auto 1fr; min-width: 0; }}
    h1 {{ font-size: 18px; margin: 0 0 8px; }}
    h2 {{ font-size: 14px; margin: 16px 0 8px; }}
    .muted {{ color: #666; font-size: 12px; }}
    .topbar {{ padding: 10px 14px; border-bottom: 1px solid #ccc; background: #fff; }}
    .name {{ font-weight: 800; }}
    .offsets {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 8px 0 12px; }}
    .offsets a {{ display: block; padding: 10px; border: 1px solid #bbb; border-radius: 6px; text-align: center; color: #111; text-decoration: none; background: #fafafa; }}
    .offsets a.selected {{ border-color: #176b32; background: #dcfce7; font-weight: 800; }}
    .tilts {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 8px 0; }}
    .tilts a {{ display: block; padding: 10px; border: 1px solid #bbb; border-radius: 6px; text-align: center; color: #111; text-decoration: none; background: #fafafa; }}
    .tilts.single {{ grid-template-columns: 1fr; }}
    .files {{ display: grid; gap: 6px; margin: 8px 0 12px; }}
    .files a {{ display: block; padding: 8px 10px; border: 1px solid #ddd; border-radius: 6px; color: #111; text-decoration: none; background: #fafafa; }}
    .files a.selected {{ border-color: #176b32; background: #dcfce7; font-weight: 800; }}
    .files .reviewed {{ color: #176b32; font-weight: 800; }}
    .btn-save {{ display: inline-block; padding: 10px 12px; border-radius: 6px; background: #176b32; color: #fff; font-weight: 700; text-decoration: none; border: 0; cursor: pointer; width: 100%; }}
    .btn-danger {{ width: 100%; padding: 10px 12px; border: 0; border-radius: 6px; background: #b42318; color: #fff; font-weight: 700; cursor: pointer; }}
    pre {{ margin: 8px 0 0; padding: 12px; border-radius: 6px; background: #111; color: #d8f3dc; overflow: auto; font-size: 12px; line-height: 1.45; }}
    iframe {{ width: 100%; height: 100%; border: 0; background: #202020; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    .quat-box {{ margin: 4px 0 12px; padding: 10px 12px; background: #f0f7f1; border: 1px solid #9fd3a8; border-radius: 6px; font-size: 12px; }}
    .search-input {{ width: 100%; box-sizing: border-box; padding: 7px 9px; border: 1px solid #ccc; border-radius: 6px; font-size: 13px; }}
    .search-results {{ margin-top:8px; max-height:260px; overflow-y:auto; border:1px solid #e5e7eb; border-radius:6px; }}
    .search-item {{ display:flex; align-items:center; gap:8px; padding:7px 10px; border-bottom:1px solid #f3f4f6; font-size:12px; }}
    .search-item:last-child {{ border-bottom:0; }}
    .search-item label {{ cursor:pointer; flex:1; }}
    @media (max-width: 980px) {{ .layout {{ grid-template-columns: 1fr; grid-template-rows: auto 60vh; }} }}
  </style>
  <script>
    async function sendAll() {{
      const btn = event.target;
      btn.disabled = true;
      btn.textContent = 'Đang gửi...';
      const div = document.getElementById('send-result');
      try {{
        const resp = await fetch('/catalog-send', {{method: 'POST'}});
        const data = await resp.json();
        const lines = (data.results || []).map(r => {{
          const icon = r.status === 'ok' ? '✅' : r.status === 'skipped' ? '⏭' : '❌';
          return `${{icon}} ${{r.nameVn || r.id}}: ${{r.status}}${{r.reason ? ' — ' + r.reason : ''}}`;
        }});
        div.innerHTML = lines.map(l => `<div>${{l}}</div>`).join('');
      }} catch(e) {{
        div.textContent = 'Lỗi: ' + e.message;
      }} finally {{
        btn.disabled = false;
        btn.textContent = 'Gửi lên catalog';
      }}
    }}
  </script>
</head>
<body>
  <div class="layout">
    <aside>
      <h1>Local GLB Orientation</h1>
      {"<div style='margin-bottom:10px;padding:10px;background:#f0f7f1;border:1px solid #9fd3a8;border-radius:6px;'>" + _build_catalog_search_panel() + "</div>" if self.catalog_mode else ""}
      <div><strong>File:</strong> {escape(self._catalog_name(glb_path) if self.catalog_mode else glb_path.name)}</div>
      {"" if self.catalog_mode else f"<div class='muted'><code>{escape(str(glb_path))}</code></div>"}
      <div class="muted"><strong>Item:</strong> {current_index + 1}/{len(self.glb_paths)}</div>
      <div class="muted" style="margin-top:6px;"><strong>Status:</strong> {escape(status_text)}</div>

      <h2>0. Pick a model</h2>
      <div class="files">{file_links}</div>
      {"<div style='margin-top:8px;'><button class='btn-save' style='background:#0369a1;' onclick='sendAll()'>Gửi lên catalog</button><div id='send-result' style='margin-top:8px;font-size:12px;'></div></div>" if self.catalog_mode else ""}

      <h2>1. Pick the real front</h2>
      <div class="offsets">{offset_links}</div>
      <div class="quat-box">
        <strong>defaultRotation (Three.js [x,y,z,w]):</strong><br>
        <code style="font-size:13px; color:#176b32;">{escape(quat_display)}</code>
      </div>

      <h2>2. Stand the object upright if needed</h2>
      <div class="muted">Current tilt: X={tilt_x}deg, Z={tilt_z}deg</div>
      <div class="tilts">{tilt_x_links}</div>
      <div class="tilts">{tilt_z_links}</div>
      <div class="tilts single">{tilt_reset_link}</div>

      <form method="post" action="/save" style="margin-top:12px;">
        <input type="hidden" name="file" value="{escape(glb_path.name)}">
        <input type="hidden" name="offset" value="{offset}">
        <input type="hidden" name="tilt_x" value="{tilt_x}">
        <input type="hidden" name="tilt_z" value="{tilt_z}">
        <button class="btn-save" type="submit">Save orientation</button>
      </form>
      <form method="post" action="/reset-saved" onsubmit="return confirm('Delete saved orientation?')" style="margin-top:8px;">
        <input type="hidden" name="file" value="{escape(glb_path.name)}">
        <button class="btn-danger" type="submit">Clear saved orientation</button>
      </form>

      <h2>3. Output JSON</h2>
      <pre>{escape(pretty_payload)}</pre>
    </aside>
    <main>
      <div class="topbar">
        <div class="name">{escape(glb_path.name)}</div>
        <div class="muted">offset={offset}deg | {escape(OFFSET_LABELS[offset])} | tiltX={tilt_x}deg | tiltZ={tilt_z}deg | quat=[{escape(quat_display)}]</div>
      </div>
      <iframe src="{escape(viewer_src)}" title="GLB viewer"></iframe>
    </main>
  </div>
</body>
</html>"""
        return document.encode("utf-8")

    def _build_empty_catalog_index(self, query: str) -> bytes:
        params = parse_qs(query)
        downloaded = params.get("downloaded", [""])[0] == "1"
        error_count = params.get("errors", ["0"])[0]
        status_text = (
            f"Download finished, but no GLB files were added. Errors: {error_count}."
            if downloaded
            else "No catalog GLBs downloaded yet."
        )
        document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Catalog GLB Orientation</title>
  <style>
    body {{ margin: 0; font: 14px system-ui, sans-serif; color: #111; background: #f6f6f4; }}
    .layout {{ display: grid; grid-template-columns: 420px 1fr; min-height: 100vh; }}
    aside {{ padding: 14px; border-right: 1px solid #ccc; overflow: auto; background: #fff; }}
    main {{ display: grid; place-items: center; min-width: 0; padding: 24px; }}
    h1 {{ font-size: 18px; margin: 0 0 8px; }}
    h2 {{ font-size: 14px; margin: 16px 0 8px; }}
    .muted {{ color: #666; font-size: 12px; }}
    .empty-state {{ max-width: 520px; padding: 18px; border: 1px solid #d1d5db; border-radius: 8px; background: #fff; }}
    .search-input {{ width: 100%; box-sizing: border-box; padding: 7px 9px; border: 1px solid #ccc; border-radius: 6px; font-size: 13px; }}
    .search-results {{ margin-top:8px; max-height:260px; overflow-y:auto; border:1px solid #e5e7eb; border-radius:6px; }}
    .search-item {{ display:flex; align-items:center; gap:8px; padding:7px 10px; border-bottom:1px solid #f3f4f6; font-size:12px; }}
    .search-item:last-child {{ border-bottom:0; }}
    .search-item label {{ cursor:pointer; flex:1; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    @media (max-width: 980px) {{ .layout {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
  <div class="layout">
    <aside>
      <h1>Catalog GLB Orientation</h1>
      <div style="margin-bottom:10px;padding:10px;background:#f0f7f1;border:1px solid #9fd3a8;border-radius:6px;">{_build_catalog_search_panel()}</div>
      <h2>Status</h2>
      <div class="muted">{escape(status_text)}</div>
      <div class="muted" style="margin-top:6px;">Downloaded GLBs will be stored in <code>{escape(CATALOG_DOWNLOADS_DIR)}</code>.</div>
    </aside>
    <main>
      <div class="empty-state">
        <strong>No GLB selected yet.</strong>
        <p class="muted">Use the catalog search panel to find items, select one or more results, then download them. The orientation viewer will appear after at least one GLB is available.</p>
      </div>
    </main>
  </div>
</body>
</html>"""
        return document.encode("utf-8")

    def _build_no_glb_index(self) -> bytes:
        document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Local GLB Orientation</title>
  <style>
    body {{ margin: 0; display: grid; place-items: center; min-height: 100vh; font: 14px system-ui, sans-serif; color: #111; background: #f6f6f4; }}
    .box {{ max-width: 520px; padding: 18px; border: 1px solid #d1d5db; border-radius: 8px; background: #fff; }}
    .muted {{ color: #666; font-size: 12px; }}
  </style>
</head>
<body>
  <div class="box">
    <strong>No GLB files are loaded.</strong>
    <p class="muted">{escape(NO_GLB_LOADED_MESSAGE)}</p>
  </div>
</body>
</html>"""
        return document.encode("utf-8")

    def _build_result_index(self, query: str) -> bytes:
        params = parse_qs(query)
        # Select object
        obj_id = params.get("obj_id", [""])[0]
        obj_index = 0
        selected_obj: dict = self.result_objects[0] if self.result_objects else {}
        for i, obj in enumerate(self.result_objects):
            if result_object_id(i, obj) == obj_id:
                obj_index = i
                selected_obj = obj
                break
        if not obj_id:
            obj_id = result_object_id(0, selected_obj)

        # Base quaternion from result.json (or from params if user navigated)
        base_quat_from_result = _quat_from_obj(selected_obj)
        base_quat = (
            self._base_quat_from_params(params)
            if params.get("qx")
            else base_quat_from_result
        )

        offset, tilt_x, tilt_z = resolve_orientation(params=params, saved_payload=None)
        offset_quat = deg_to_quaternion_y(float(offset))
        corrected_quat = normalize_quaternion(
            multiply_quaternions(base_quat, offset_quat)
        )

        model_url = selected_obj.get("modelUrl", "")
        proxy_url = "/proxy-glb?" + urlencode({"url": model_url}) if model_url else ""

        viewer_src = "/viewer?" + urlencode(
            {
                "model_url": proxy_url,
                "offset": str(offset),
                "tilt_x": str(tilt_x),
                "tilt_z": str(tilt_z),
                "qx": str(base_quat["x"]),
                "qy": str(base_quat["y"]),
                "qz": str(base_quat["z"]),
                "qw": str(base_quat["w"]),
            }
        )

        export_url = "/export.json?" + urlencode(
            {
                "obj_id": obj_id,
                "offset": str(offset),
                "tilt_x": str(tilt_x),
                "tilt_z": str(tilt_z),
                "qx": str(base_quat["x"]),
                "qy": str(base_quat["y"]),
                "qz": str(base_quat["z"]),
                "qw": str(base_quat["w"]),
            }
        )

        saved_now = params.get("saved", [""])[0] == "1"
        status_text = (
            "✅ Correction saved to file."
            if saved_now
            else f"Reviewing {obj_index + 1}/{len(self.result_objects)} objects."
        )

        # Object list
        obj_links = "\n".join(
            self._result_obj_link(
                index=i,
                obj=o,
                selected=(result_object_id(i, o) == obj_id),
            )
            for i, o in enumerate(self.result_objects)
        )

        # Offset buttons (with base quat threaded through)
        offset_links = " ".join(
            self._result_offset_link(
                value=value,
                obj_id=obj_id,
                base_quat=base_quat,
                tilt_x=tilt_x,
                tilt_z=tilt_z,
                selected=offset == value,
            )
            for value in OFFSET_LABELS
        )
        tilt_x_links = " ".join(
            (
                self._result_tilt_link(
                    obj_id=obj_id,
                    base_quat=base_quat,
                    offset=offset,
                    tilt_x=adjusted_tilt(tilt_x, -90),
                    tilt_z=tilt_z,
                    label="X -90",
                ),
                self._result_tilt_link(
                    obj_id=obj_id,
                    base_quat=base_quat,
                    offset=offset,
                    tilt_x=adjusted_tilt(tilt_x, 90),
                    tilt_z=tilt_z,
                    label="X +90",
                ),
            )
        )
        tilt_z_links = " ".join(
            (
                self._result_tilt_link(
                    obj_id=obj_id,
                    base_quat=base_quat,
                    offset=offset,
                    tilt_x=tilt_x,
                    tilt_z=adjusted_tilt(tilt_z, -90),
                    label="Z -90",
                ),
                self._result_tilt_link(
                    obj_id=obj_id,
                    base_quat=base_quat,
                    offset=offset,
                    tilt_x=tilt_x,
                    tilt_z=adjusted_tilt(tilt_z, 90),
                    label="Z +90",
                ),
            )
        )
        tilt_reset_link = self._result_tilt_link(
            obj_id=obj_id,
            base_quat=base_quat,
            offset=offset,
            tilt_x=0,
            tilt_z=0,
            label="Reset tilt",
        )

        # Reset to result.json base rotation
        reset_base_url = "/?" + urlencode(
            {
                "obj_id": obj_id,
                "offset": "0",
                "tilt_x": "0",
                "tilt_z": "0",
                "qx": str(base_quat_from_result["x"]),
                "qy": str(base_quat_from_result["y"]),
                "qz": str(base_quat_from_result["z"]),
                "qw": str(base_quat_from_result["w"]),
            }
        )

        payload_preview = json.dumps(
            {
                "name": selected_obj.get("name"),
                "catalog_item_id": selected_obj.get("catalogItemId"),
                "base_rotation_from_result": base_quat_from_result,
                "correction_offset_deg": offset,
                "correction_quaternion": offset_quat,
                "corrected_rotation": corrected_quat,
                "note": "Put corrected_rotation back into result.json rotation field, or multiply correction_quaternion into catalog defaultRotation.",
            },
            indent=2,
            ensure_ascii=False,
        )

        base_disp = f"x={base_quat['x']:.4f}, y={base_quat['y']:.4f}, z={base_quat['z']:.4f}, w={base_quat['w']:.4f}"
        corr_disp = f"x={corrected_quat['x']:.4f}, y={corrected_quat['y']:.4f}, z={corrected_quat['z']:.4f}, w={corrected_quat['w']:.4f}"

        document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Result Review — {escape(selected_obj.get("name", "?"))}</title>
  <style>
    body {{ margin: 0; font: 14px system-ui, sans-serif; color: #111; background: #f6f6f4; }}
    .layout {{ display: grid; grid-template-columns: 440px 1fr; height: 100vh; }}
    aside {{ padding: 14px; border-right: 1px solid #ccc; overflow: auto; background: #fff; }}
    main {{ display: grid; grid-template-rows: auto 1fr; min-width: 0; }}
    h1 {{ font-size: 16px; margin: 0 0 6px; }}
    h2 {{ font-size: 13px; margin: 14px 0 6px; text-transform: uppercase; color: #555; letter-spacing:.04em; }}
    .muted {{ color: #666; font-size: 12px; }}
    .badge {{ display:inline-block; padding:2px 7px; border-radius:4px; font-size:11px; font-weight:700; background:#e5e7eb; }}
    .badge.ok {{ background:#dcfce7; color:#166534; }}
    .topbar {{ padding: 10px 14px; border-bottom: 1px solid #ccc; background: #fff; }}
    .name {{ font-weight: 800; }}
    .offsets {{ display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin: 6px 0 10px; }}
    .offsets a {{ display: block; padding: 9px; border: 1px solid #bbb; border-radius: 6px; text-align: center; color: #111; text-decoration: none; background: #fafafa; font-size: 13px; }}
    .offsets a.selected {{ border-color: #176b32; background: #dcfce7; font-weight: 800; }}
    .tilts {{ display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin: 6px 0; }}
    .tilts a {{ display: block; padding: 9px; border: 1px solid #bbb; border-radius: 6px; text-align: center; color: #111; text-decoration: none; background: #fafafa; font-size: 13px; }}
    .tilts.single {{ grid-template-columns: 1fr; }}
    .obj-list {{ display: grid; gap: 4px; margin: 6px 0 12px; max-height: 200px; overflow-y: auto; border: 1px solid #e5e7eb; border-radius: 6px; padding: 4px; }}
    .obj-list a {{ display: block; padding: 6px 8px; border-radius: 4px; color: #111; text-decoration: none; font-size: 12px; }}
    .obj-list a:hover {{ background: #f3f4f6; }}
    .obj-list a.selected {{ background: #dcfce7; font-weight: 700; }}
    .quat-box {{ margin: 6px 0 10px; padding: 9px 11px; border-radius: 6px; font-size: 12px; }}
    .quat-box.base {{ background: #fef9c3; border: 1px solid #fde68a; }}
    .quat-box.corrected {{ background: #f0f7f1; border: 1px solid #9fd3a8; }}
    .btn-save {{ display: block; width: 100%; padding: 10px; border-radius: 6px; background: #176b32; color: #fff; font-weight: 700; border: 0; cursor: pointer; font-size: 14px; }}
    .btn-secondary {{ display: block; width: 100%; padding: 9px; border-radius: 6px; background: #f3f4f6; color: #111; border: 1px solid #d1d5db; cursor: pointer; font-size: 13px; text-align:center; text-decoration:none; margin-top:6px; }}
    pre {{ margin: 6px 0 0; padding: 10px; border-radius: 6px; background: #111; color: #d8f3dc; overflow: auto; font-size: 11px; line-height: 1.45; }}
    iframe {{ width: 100%; height: 100%; border: 0; background: #202020; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  </style>
</head>
<body>
  <div class="layout">
    <aside>
      <h1>🪑 Result Review Mode</h1>
      <div class="muted">{escape(status_text)}</div>

      <h2>Select object</h2>
      <div class="obj-list">{obj_links}</div>

      <h2>Base rotation (from result.json)</h2>
      <div class="quat-box base">
        <strong>{escape(selected_obj.get("name", "?"))}</strong><br>
        <code style="font-size:11px;">{escape(base_disp)}</code><br>
        <a href="{escape(reset_base_url)}" class="btn-secondary" style="display:inline-block;padding:4px 10px;font-size:12px;margin-top:4px;">↩ Reset to result rotation</a>
      </div>

      <h2>1. Adjust yaw (front direction)</h2>
      <div class="offsets">{offset_links}</div>

      <h2>2. Adjust tilt if needed</h2>
      <div class="muted">Current tilt: X={tilt_x}deg, Z={tilt_z}deg</div>
      <div class="tilts">{tilt_x_links}</div>
      <div class="tilts">{tilt_z_links}</div>
      <div class="tilts single">{tilt_reset_link}</div>

      <h2>Corrected rotation</h2>
      <div class="quat-box corrected">
        <strong>Final quaternion [x,y,z,w]:</strong><br>
        <code style="font-size:11px; color:#176b32;">{escape(corr_disp)}</code>
      </div>

      <form method="post" action="/save" style="margin-top:10px;">
        <input type="hidden" name="obj_id" value="{escape(obj_id)}">
        <input type="hidden" name="offset" value="{offset}">
        <input type="hidden" name="tilt_x" value="{tilt_x}">
        <input type="hidden" name="tilt_z" value="{tilt_z}">
        <input type="hidden" name="qx" value="{base_quat["x"]}">
        <input type="hidden" name="qy" value="{base_quat["y"]}">
        <input type="hidden" name="qz" value="{base_quat["z"]}">
        <input type="hidden" name="qw" value="{base_quat["w"]}">
        <button class="btn-save" type="submit">💾 Save correction JSON</button>
      </form>
      <a href="{escape(export_url)}" class="btn-secondary" target="_blank">📋 Export JSON only</a>

      <h2>Output preview</h2>
      <pre>{escape(payload_preview)}</pre>
    </aside>
    <main>
      <div class="topbar">
        <div class="name">{escape(selected_obj.get("name", "?"))} <span class="badge">result.json mode</span></div>
        <div class="muted">base=[{escape(base_disp)}] | correction_offset={offset}deg | corrected=[{escape(corr_disp)}]</div>
      </div>
      <iframe src="{escape(viewer_src)}" title="GLB viewer"></iframe>
    </main>
  </div>
</body>
</html>"""
        return document.encode("utf-8")

    def _build_viewer(self, query: str) -> bytes:
        params = parse_qs(query)

        # In result mode, model comes from proxy; in local mode from /model
        if self._result_mode or params.get("model_url"):
            proxy_url = params.get("model_url", [""])[0]
            model_url_json = json.dumps(proxy_url)
        else:
            glb_path = selected_glb_path(params=params, glb_paths=self.glb_paths)
            model_url_json = json.dumps(f"/model?{urlencode({'file': glb_path.name})}")

        offset, tilt_x, tilt_z = resolve_orientation(params=params, saved_payload=None)
        base_quat = self._base_quat_from_params(params)
        has_base_quat = not (
            abs(base_quat["x"]) < 1e-9
            and abs(base_quat["y"]) < 1e-9
            and abs(base_quat["z"]) < 1e-9
            and abs(base_quat["w"] - 1) < 1e-9
        )
        base_quat_json = json.dumps(base_quat)

        status_text = (
            f"Loaded — base_quat from result + offset={offset}deg tiltX={tilt_x}deg tiltZ={tilt_z}deg"
            if has_base_quat
            else f"Loaded — offset={offset}deg tiltX={tilt_x}deg tiltZ={tilt_z}deg"
        )
        status_text_json = json.dumps(status_text)

        document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{ margin: 0; overflow: hidden; background: #202020; color: white; font: 13px system-ui, sans-serif; }}
    #status {{ position: fixed; left: 12px; top: 12px; padding: 8px 10px; background: rgba(0,0,0,.7); border-radius: 6px; z-index: 5; max-width: 70vw; }}
    #hint {{ position: fixed; left: 12px; bottom: 12px; padding: 8px 10px; background: rgba(0,0,0,.7); border-radius: 6px; z-index: 5; }}
  </style>
</head>
<body>
  <div id="status">Loading model...</div>
  <div id="hint">White arrow = 2D plan FRONT (-Z). Drag to orbit, scroll to zoom.</div>
  <script>
    window.addEventListener('error', (event) => {{
      document.getElementById('status').textContent = 'Error: ' + event.message;
    }});
    window.addEventListener('unhandledrejection', (event) => {{
      const r = event.reason;
      document.getElementById('status').textContent = 'Error: ' + (r && r.message ? r.message : String(r));
    }});
  </script>
  <script type="module">
    import * as THREE from '/vendor/three.module.js';
    import {{ OrbitControls }} from '/vendor/OrbitControls.js';
    import {{ GLTFLoader }} from '/vendor/loaders/GLTFLoader.js';
    import {{ DRACOLoader }} from '/vendor/loaders/DRACOLoader.js';
    import {{ KTX2Loader }} from '/vendor/loaders/KTX2Loader.js';

    const status = document.getElementById('status');
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x202020);
    const renderer = new THREE.WebGLRenderer({{ antialias: true }});
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(window.innerWidth, window.innerHeight);
    renderer.shadowMap.enabled = true;
    document.body.appendChild(renderer.domElement);

    const ktx2Loader = new KTX2Loader()
      .setTranscoderPath('/basis/')
      .setWorkerLimit(2)
      .detectSupport(renderer);
    const dracoLoader = new DRACOLoader()
      .setDecoderPath('/draco/')
      .setWorkerLimit(2);

    const camera = new THREE.PerspectiveCamera(45, window.innerWidth / window.innerHeight, 0.01, 100);
    camera.position.set(2.6, 2.0, 3.0);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.target.set(0, 0.55, 0);

    const world = new THREE.Group();
    scene.add(world);
    scene.add(new THREE.HemisphereLight(0xffffff, 0x444444, 1.7));
    const key = new THREE.DirectionalLight(0xffffff, 2.2);
    key.position.set(4, 6, 5);
    key.castShadow = true;
    scene.add(key);

    const grid = new THREE.GridHelper(4, 8, 0x999999, 0x444444);
    world.add(grid);

    // White arrow = 2D plan FRONT direction (-Z in Three.js)
    const arrow = new THREE.ArrowHelper(
      new THREE.Vector3(0, 0, -1),
      new THREE.Vector3(0, 0.08, 1.0),
      1.5, 0xffffff, 0.25, 0.12
    );
    world.add(arrow);

    function makeLabel(text, x, y, z, color = '#ffffff') {{
      const c = document.createElement('canvas');
      c.width = 256; c.height = 64;
      const ctx = c.getContext('2d');
      ctx.font = '700 28px system-ui';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillStyle = color;
      ctx.fillText(text, 128, 32);
      const sprite = new THREE.Sprite(new THREE.SpriteMaterial({{ map: new THREE.CanvasTexture(c), transparent: true }}));
      sprite.position.set(x, y, z);
      sprite.scale.set(0.75, 0.18, 1);
      world.add(sprite);
    }}
    makeLabel('PLAN FRONT', 0, 0.1, -0.85);
    makeLabel('BACK',  0, 0.1,  1.0, '#bdbdbd');
    makeLabel('LEFT', -1.25, 0.1, 0, '#bdbdbd');
    makeLabel('RIGHT', 1.25, 0.1, 0, '#bdbdbd');

    window.addEventListener('resize', resize);
    resize();

    function tick() {{
      requestAnimationFrame(tick);
      controls.update();
      renderer.render(scene, camera);
    }}
    tick();

    main().catch((err) => {{
      status.textContent = 'Viewer error: ' + (err instanceof Error ? err.message : String(err));
      console.error(err);
    }});

    async function main() {{
      const modelUrl = {model_url_json};
      const baseQuat = {base_quat_json};
      const hasBaseQuat = {json.dumps(has_base_quat)};

      status.textContent = 'Fetching GLB...';
      const response = await fetch(modelUrl, {{ cache: 'no-store' }});
      if (!response.ok) throw new Error(`GLB fetch failed: ${{response.status}} ${{response.statusText}}`);
      const bytes = await response.arrayBuffer();
      status.textContent = `Parsing GLB (${{formatBytes(bytes.byteLength)}})...`;

      const loader = new GLTFLoader();
      if (typeof loader.setKTX2Loader === 'function') loader.setKTX2Loader(ktx2Loader);
      if (typeof loader.setDRACOLoader === 'function') loader.setDRACOLoader(dracoLoader);
      const gltf = await new Promise((resolve, reject) => loader.parse(bytes, '', resolve, reject));
      const sceneSource = gltf.scene || (Array.isArray(gltf.scenes) ? gltf.scenes[0] : null);
      if (!sceneSource || typeof sceneSource.clone !== 'function')
        throw new Error('GLB loaded, but it does not contain a usable scene.');

      const root = new THREE.Group();

      if (hasBaseQuat) {{
        // Result mode: apply full result quaternion as base, then yaw offset on top
        const baseGroup = new THREE.Group();
        baseGroup.quaternion.set(baseQuat.x, baseQuat.y, baseQuat.z, baseQuat.w);

        const yawGroup = new THREE.Group();
        yawGroup.rotation.y = THREE.MathUtils.degToRad({offset});

        const tiltGroup = new THREE.Group();
        tiltGroup.rotation.x = THREE.MathUtils.degToRad({tilt_x});
        tiltGroup.rotation.z = THREE.MathUtils.degToRad({tilt_z});

        const model = sceneSource.clone(true);
        // Mirror normalizeGltfRoot: reset root transform
        model.position.set(0, 0, 0);
        model.rotation.set(0, 0, 0);
        model.scale.set(1, 1, 1);

        tiltGroup.add(model);
        yawGroup.add(tiltGroup);
        baseGroup.add(yawGroup);
        root.add(baseGroup);
      }} else {{
        // Local file mode: yaw + tilt only
        const yawRoot = new THREE.Group();
        const tiltRoot = new THREE.Group();
        const model = sceneSource.clone(true);
        model.position.set(0, 0, 0);
        model.rotation.set(0, 0, 0);
        model.scale.set(1, 1, 1);
        yawRoot.rotation.y = THREE.MathUtils.degToRad({offset});
        tiltRoot.rotation.x = THREE.MathUtils.degToRad({tilt_x});
        tiltRoot.rotation.z = THREE.MathUtils.degToRad({tilt_z});
        tiltRoot.add(model);
        yawRoot.add(tiltRoot);
        root.add(yawRoot);
      }}

      world.add(root);
      root.updateMatrixWorld(true);
      const box = new THREE.Box3().setFromObject(root);
      const size = box.getSize(new THREE.Vector3());
      const maxDim = Math.max(size.x, size.y, size.z);
      if (!Number.isFinite(maxDim) || maxDim <= 0) throw new Error('GLB bounds are empty.');

      const scale = Math.min(
        1.8 / Math.max(size.x, size.z, 0.001),
        1.6 / Math.max(size.x, size.y, size.z, 0.001)
      );
      root.scale.setScalar(scale);
      root.updateMatrixWorld(true);
      const fitted = new THREE.Box3().setFromObject(root);
      const center = fitted.getCenter(new THREE.Vector3());
      root.position.x -= center.x;
      root.position.z -= center.z;
      root.position.y -= fitted.min.y;

      root.traverse((node) => {{
        if (node.isMesh) {{
          node.castShadow = true;
          node.receiveShadow = true;
          const mats = Array.isArray(node.material) ? node.material : [node.material];
          for (const m of mats) {{ m.side = THREE.DoubleSide; m.needsUpdate = true; }}
        }}
      }});
      status.textContent = {status_text_json};
    }}

    function resize() {{
      renderer.setSize(window.innerWidth, window.innerHeight);
      camera.aspect = window.innerWidth / window.innerHeight;
      camera.updateProjectionMatrix();
    }}

    function formatBytes(n) {{
      if (!Number.isFinite(n)) return 'unknown size';
      return n < 1048576 ? `${{Math.round(n/1024)}} KB` : `${{(n/1048576).toFixed(1)}} MB`;
    }}
  </script>
</body>
</html>"""
        return document.encode("utf-8")

    def _build_export_payload(self, query: str) -> dict[str, object]:
        params = parse_qs(query)
        if self._result_mode:
            obj_id = params.get("obj_id", [""])[0]
            offset, tilt_x, tilt_z = resolve_orientation(
                params=params, saved_payload=None
            )
            base_quat = self._base_quat_from_params(params)
            obj = self._find_result_obj(obj_id) or (
                self.result_objects[0] if self.result_objects else {}
            )
            return build_result_payload(
                obj=obj,
                obj_id=obj_id,
                base_quat=base_quat,
                offset=offset,
                tilt_x=tilt_x,
                tilt_z=tilt_z,
            )

        if not self.glb_paths:
            return {
                "error": NO_GLB_LOADED_MESSAGE,
                "catalog_mode": self.catalog_mode,
                "catalog_downloads_dir": str(CATALOG_DOWNLOADS_DIR)
                if self.catalog_mode
                else None,
            }

        glb_path = selected_glb_path(params=params, glb_paths=self.glb_paths)
        orientation_path = orientation_sidecar_path(glb_path)
        offset, tilt_x, tilt_z = resolve_orientation(
            params=params,
            saved_payload=load_saved_orientation(orientation_path),
        )
        payload = build_preview_payload(
            glb_path=glb_path, offset=offset, tilt_x=tilt_x, tilt_z=tilt_z
        )
        payload["orientation_file"] = str(orientation_path)
        return payload

    # ── catalog mode handlers ─────────────────────────────────────────────────

    def _handle_catalog_api_search(self, query: str) -> None:
        params = parse_qs(query)
        api_params: dict[str, str] = {
            "limit": params.get("limit", ["50"])[0],
            "offset": params.get("offset", ["0"])[0],
        }
        search = params.get("search", [""])[0]
        if search:
            api_params["search"] = search
        drp = params.get("defaultRotationPresence", [""])[0]
        if drp:
            api_params["defaultRotationPresence"] = drp
        url = f"{CATALOG_API_BASE}/api/catalog/items?{urlencode(api_params)}"
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": CATALOG_TOOL_USER_AGENT,
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = resp.read()
            self.send_response(int(HTTPStatus.OK))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            self._send_json(
                {
                    "error": f"Catalog API HTTP {exc.code}: {exc.reason}",
                    "details": error_body[:500],
                    "url": url,
                },
                status=HTTPStatus.BAD_GATEWAY,
            )
        except Exception as exc:
            self._send_json(
                {"error": f"Catalog API error: {exc}", "url": url},
                status=HTTPStatus.BAD_GATEWAY,
            )

    def _handle_catalog_status(self) -> None:
        session = load_catalog_session()
        result = []
        for item_id, meta in session.items():
            op = catalog_orientation_path(item_id)
            orientation = None
            if op.is_file():
                try:
                    orientation = json.loads(op.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    pass
            result.append(
                {
                    "id": item_id,
                    "nameVn": meta.get("nameVn"),
                    "downloaded": catalog_glb_path(item_id).is_file(),
                    "orientation_saved": op.is_file(),
                    "quaternion": orientation.get("quaternion")
                    if orientation
                    else None,
                }
            )
        self._send_json({"items": result})

    def _handle_catalog_download(self, params: dict[str, list[str]]) -> None:
        items_json = params.get("items_json", ["[]"])[0]
        try:
            items = json.loads(items_json)
        except (json.JSONDecodeError, ValueError):
            self._send_plain("Invalid items_json.", status=HTTPStatus.BAD_REQUEST)
            return

        CATALOG_DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        session = load_catalog_session()
        errors: list[str] = []

        for item in items:
            item_id = item.get("id", "")
            model_url = item.get("modelUrl", "")
            if not item_id or not model_url:
                continue
            glb_url = f"{STORAGE_BASE}{model_url}"
            dest = catalog_glb_path(item_id)
            try:
                req = urllib.request.Request(
                    glb_url, headers={"User-Agent": CATALOG_TOOL_USER_AGENT}
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    dest.write_bytes(resp.read())
                session[item_id] = {
                    "id": item_id,
                    "nameVn": item.get("nameVn") or item.get("name") or item_id,
                    "modelUrl": model_url,
                    "glb_filename": dest.name,
                }
            except Exception as exc:
                errors.append(f"{item_id}: {exc}")

        save_catalog_session(session)

        # Rebuild glb_paths from updated session
        self.__class__.glb_paths = [
            catalog_glb_path(iid) for iid in session if catalog_glb_path(iid).is_file()
        ]

        query = urlencode({"downloaded": "1", "errors": len(errors)})
        self.send_response(int(HTTPStatus.SEE_OTHER))
        self.send_header("Location", f"/?{query}")
        self.end_headers()

    def _handle_catalog_send(self) -> None:
        session = load_catalog_session()
        results = []
        for item_id, meta in session.items():
            op = catalog_orientation_path(item_id)
            if not op.is_file():
                results.append(
                    {
                        "id": item_id,
                        "nameVn": meta.get("nameVn"),
                        "status": "skipped",
                        "reason": "no orientation saved",
                    }
                )
                continue
            try:
                orientation = json.loads(op.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                results.append(
                    {
                        "id": item_id,
                        "nameVn": meta.get("nameVn"),
                        "status": "error",
                        "reason": "could not read orientation file",
                    }
                )
                continue

            quaternion = orientation.get("quaternion")
            if not isinstance(quaternion, list) or len(quaternion) != 4:
                results.append(
                    {
                        "id": item_id,
                        "nameVn": meta.get("nameVn"),
                        "status": "error",
                        "reason": "invalid quaternion in orientation file",
                    }
                )
                continue

            url = f"{CATALOG_API_BASE}/api/catalog/items/{item_id}"
            body = json.dumps({"defaultRotation": quaternion}).encode("utf-8")
            try:
                req = urllib.request.Request(
                    url,
                    data=body,
                    method="PUT",
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "User-Agent": CATALOG_TOOL_USER_AGENT,
                    },
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    resp_body = resp.read().decode("utf-8", errors="replace")
                results.append(
                    {
                        "id": item_id,
                        "nameVn": meta.get("nameVn"),
                        "status": "ok",
                        "quaternion": quaternion,
                        "response": resp_body[:200],
                    }
                )
            except Exception as exc:
                results.append(
                    {
                        "id": item_id,
                        "nameVn": meta.get("nameVn"),
                        "status": "error",
                        "reason": str(exc),
                    }
                )

        self._send_json({"results": results})

    def _catalog_name(self, glb_path: Path) -> str:
        """Return nameVn from session for a catalog GLB, or fall back to filename."""
        if not self.catalog_mode:
            return glb_path.name
        item_id = glb_path.stem  # filename without .glb
        session = load_catalog_session()
        return session.get(item_id, {}).get("nameVn") or glb_path.name

    def _proxy_glb(self, url: str) -> None:
        """Fetch a remote GLB and pipe it back to the client."""
        if not url.startswith(("http://", "https://")):
            self._send_plain(
                "Only http/https URLs are allowed.", status=HTTPStatus.BAD_REQUEST
            )
            return
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": CATALOG_TOOL_USER_AGENT}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            self.send_response(int(HTTPStatus.OK))
            self.send_header("Content-Type", "model/gltf-binary")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except Exception as exc:
            self._send_plain(f"Proxy error: {exc}", status=HTTPStatus.BAD_GATEWAY)

    # ── link builders ─────────────────────────────────────────────────────────

    def _offset_link(
        self, *, offset: int, file_name: str, tilt_x: int, tilt_z: int, selected: bool
    ) -> str:
        query = urlencode(
            {
                "file": file_name,
                "offset": str(offset),
                "tilt_x": str(tilt_x),
                "tilt_z": str(tilt_z),
            }
        )
        cls = "selected" if selected else ""
        return f'<a class="{cls}" href="/?{query}">{escape(OFFSET_LABELS[offset])}<br>{offset}°</a>'

    def _tilt_link(
        self, *, offset: int, file_name: str, tilt_x: int, tilt_z: int, label: str
    ) -> str:
        query = urlencode(
            {
                "file": file_name,
                "offset": str(offset),
                "tilt_x": str(tilt_x),
                "tilt_z": str(tilt_z),
            }
        )
        return f'<a href="/?{query}">{escape(label)}</a>'

    def _result_offset_link(
        self,
        *,
        value: int,
        obj_id: str,
        base_quat: dict,
        tilt_x: int,
        tilt_z: int,
        selected: bool,
    ) -> str:
        query = urlencode(
            {
                **_quat_params(base_quat),
                "obj_id": obj_id,
                "offset": str(value),
                "tilt_x": str(tilt_x),
                "tilt_z": str(tilt_z),
            }
        )
        cls = "selected" if selected else ""
        return f'<a class="{cls}" href="/?{query}">{escape(OFFSET_LABELS[value])}<br>{value}°</a>'

    def _result_tilt_link(
        self,
        *,
        obj_id: str,
        base_quat: dict,
        offset: int,
        tilt_x: int,
        tilt_z: int,
        label: str,
    ) -> str:
        query = urlencode(
            {
                **_quat_params(base_quat),
                "obj_id": obj_id,
                "offset": str(offset),
                "tilt_x": str(tilt_x),
                "tilt_z": str(tilt_z),
            }
        )
        return f'<a href="/?{query}">{escape(label)}</a>'

    def _result_obj_link(self, *, index: int, obj: dict, selected: bool) -> str:
        oid = result_object_id(index, obj)
        base_quat = _quat_from_obj(obj)
        query = urlencode(
            {
                **_quat_params(base_quat),
                "obj_id": oid,
                "offset": "0",
                "tilt_x": "0",
                "tilt_z": "0",
            }
        )
        cls = "selected" if selected else ""
        name = obj.get("name") or "?"
        cat = obj.get("catalogItemId") or ""
        return (
            f'<a class="{cls}" href="/?{query}">'
            f"{index + 1}. {escape(name)}<br>"
            f'<span style="color:#888;font-size:11px;">{escape(cat[:30])}</span></a>'
        )

    def _file_link(
        self,
        *,
        glb_path: Path,
        selected: bool,
        index: int,
        total: int,
        display_name: str | None = None,
    ) -> str:
        query = urlencode({"file": glb_path.name})
        cls = "selected" if selected else ""
        review_label = (
            '<span class="reviewed">reviewed</span>'
            if orientation_sidecar_path(glb_path).is_file()
            else "unreviewed"
        )
        label = escape(display_name or glb_path.name)
        sub = escape(glb_path.name) if display_name else review_label
        return (
            (
                f'<a class="{cls}" href="/?{query}">'
                f"{index + 1}/{total} - {label}<br>"
                f'<span class="muted">{sub}</span>'
                f'<span class="muted"> · {review_label}</span></a>'
            )
            if display_name
            else (
                f'<a class="{cls}" href="/?{query}">'
                f"{index + 1}/{total} - {label}<br>"
                f'<span class="muted">{review_label}</span></a>'
            )
        )

    # ── send helpers ──────────────────────────────────────────────────────────

    def _send_vendor_file(self, relative_path: str) -> None:
        relative = Path(relative_path)
        candidate = (VENDOR_DIR / relative).resolve()
        if not candidate.is_file():
            raise FileNotFoundError(f"Vendor file not found: {candidate}")
        if (
            VENDOR_DIR.resolve() not in candidate.parents
            and candidate != VENDOR_DIR.resolve()
        ):
            raise FileNotFoundError(
                f"Vendor file is outside allowed directory: {candidate}"
            )
        self._send_file(candidate)

    def _send_basis_file(self, relative_path: str) -> None:
        relative = Path(relative_path)
        candidate = (self.basis_dir / relative).resolve()
        if not candidate.is_file():
            raise FileNotFoundError(f"Basis transcoder file not found: {candidate}")
        resolved_basis_dir = self.basis_dir.resolve()
        if (
            resolved_basis_dir not in candidate.parents
            and candidate != resolved_basis_dir
        ):
            raise FileNotFoundError(
                f"Basis file is outside allowed directory: {candidate}"
            )
        self._send_file(candidate)

    def _send_draco_file(self, relative_path: str) -> None:
        relative = Path(relative_path)
        candidate = (self.draco_dir / relative).resolve()
        if not candidate.is_file():
            raise FileNotFoundError(f"Draco decoder file not found: {candidate}")
        resolved_draco_dir = self.draco_dir.resolve()
        if (
            resolved_draco_dir not in candidate.parents
            and candidate != resolved_draco_dir
        ):
            raise FileNotFoundError(
                f"Draco file is outside allowed directory: {candidate}"
            )
        content_type = "application/wasm" if candidate.suffix == ".wasm" else None
        self._send_file(candidate, default_content_type=content_type)

    def _send_file(
        self, path: Path, *, default_content_type: str | None = None
    ) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        content_type = (
            default_content_type
            or mimetypes.guess_type(path.name)[0]
            or "application/octet-stream"
        )
        data = path.read_bytes()
        self.send_response(int(HTTPStatus.OK))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, document: bytes) -> None:
        self.send_response(int(HTTPStatus.OK))
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(document)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(document)

    def _send_json(
        self, payload: dict[str, object], *, status: HTTPStatus = HTTPStatus.OK
    ) -> None:
        data = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_plain(self, message: str, *, status: HTTPStatus) -> None:
        data = message.encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ── small helpers ─────────────────────────────────────────────────────────────


def _quat_from_obj(obj: dict) -> dict[str, float]:
    rot = obj.get("rotation")
    if isinstance(rot, dict):
        try:
            return {
                "x": float(rot.get("x", 0)),
                "y": float(rot.get("y", 0)),
                "z": float(rot.get("z", 0)),
                "w": float(rot.get("w", 1)),
            }
        except (TypeError, ValueError):
            pass
    return {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}


def _quat_params(q: dict[str, float]) -> dict[str, str]:
    return {"qx": str(q["x"]), "qy": str(q["y"]), "qz": str(q["z"]), "qw": str(q["w"])}


# ── CLI ───────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "GLB orientation review tool.\n"
            "Mode 1 (local files):  python tool.py /path/to/glb_dir\n"
            "Mode 2 (result.json):  python tool.py --result /path/to/result.json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "glb_path",
        nargs="?",
        default=None,
        help="Path to a local .glb file or directory (local file mode).",
    )
    group.add_argument(
        "--result",
        metavar="RESULT_JSON",
        help="Path to a normalize-run result.json to review its objects.",
    )
    group.add_argument(
        "--catalog",
        action="store_true",
        default=False,
        help="Catalog review mode: search, download, and set defaultRotation via the catalog API.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="HTTP host to bind.")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="HTTP port to bind."
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    result_objs: list[dict] = []
    glb_paths: list[Path] = []
    catalog_mode = False

    if args.result:
        result_path = Path(args.result).expanduser().resolve()
        if not result_path.is_file():
            raise SystemExit(f"result.json not found: {result_path}")
        result_objs = load_result_json(result_path)
        if not result_objs:
            raise SystemExit(f"No objects with modelUrl found in: {result_path}")
        print(f"Result review mode: {len(result_objs)} objects from {result_path.name}")
        glb_paths = []
    elif args.catalog:
        CATALOG_DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        session = load_catalog_session()
        glb_paths = [
            catalog_glb_path(iid) for iid in session if catalog_glb_path(iid).is_file()
        ]
        catalog_mode = True
        print(
            f"Catalog mode: {len(glb_paths)} GLB(s) already downloaded in {CATALOG_DOWNLOADS_DIR}"
        )
        print(f"Catalog API: {CATALOG_API_BASE}")
        print(f"Storage:     {STORAGE_BASE}")
    else:
        raw_path = args.glb_path or str(DEFAULT_GLB_PATH)
        input_path = Path(raw_path).expanduser().resolve()
        try:
            glb_paths = discover_glb_paths(input_path)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"Local file mode: {len(glb_paths)} GLB model(s) from {input_path}")

    handler = type(
        "BoundHandler",
        (LocalGlbOrientationHandler,),
        {
            "glb_paths": glb_paths,
            "basis_dir": BASIS_DIR.resolve(),
            "draco_dir": DRACO_DIR.resolve(),
            "result_objects": result_objs,
            "catalog_mode": catalog_mode,
        },
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    mode = (
        "result-review"
        if result_objs
        else ("catalog" if catalog_mode else "local-files")
    )
    print(f"Mode: {mode}")
    print(f"Open http://{args.host}:{args.port}")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
