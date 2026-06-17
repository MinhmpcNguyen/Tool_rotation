# Manual GLB Orientation Tool

Tool này mở một web viewer local để kiểm tra hướng mặt trước của file `.glb`, chỉnh yaw/tilt, lưu JSON sidecar, và trong catalog mode có thể gửi `defaultRotation` ngược lên Catalog API.

## Yêu cầu

- Python 3.10 trở lên.
- Browser có WebGL.
- Không cần package Python ngoài standard library. `requirements.txt` để trống có chủ ý.

## Cấu trúc thư mục

- `manual_local_glb_orientation.py`: server + web viewer.
- `demo_inventory/`: GLB mẫu để test local mode.
- `catalog_downloads/`: GLB tải từ catalog trong catalog mode.
- `catalog_session.json`: metadata các item catalog đã tải.
- `frontend/src/vendor/three/`: Three.js, `GLTFLoader`, `KTX2Loader`, `DRACOLoader`.
- `frontend/public/assets/three/basis/`: Basis/KTX transcoder.
- `frontend/public/assets/three/draco/`: Draco decoder cho GLB nén Draco.
- `orientation_correction_*.json`: output correction khi review `result.json`.

## Chạy nhanh

Từ thư mục này:

```bash
cd tool
python manual_local_glb_orientation.py
```

Mở browser:

```text
http://127.0.0.1:8790
```

Nếu port `8790` đang bận:

```bash
python manual_local_glb_orientation.py --port 8791
```

Mỗi lần sửa code tool, hãy dừng server cũ rồi chạy lại để browser nhận code mới.

<!-- ## Mode 1: Review GLB local

Chạy với folder hoặc một file `.glb`:

```bash
python manual_local_glb_orientation.py demo_inventory
python manual_local_glb_orientation.py demo_inventory/bed.glb
```

Trong UI:

1. Chọn model ở mục `0. Pick a model`.
2. Chọn mặt trước thật ở mục `1. Pick the real front`.
3. Nếu object bị nằm ngang/ngửa, dùng `X -90`, `X +90`, `Z -90`, `Z +90`.
4. Bấm `Save orientation`.

Output được lưu cạnh file GLB:

```text
<model>.glb.orientation.json
```

File này có `preview_override`, `orientation_review`, `quaternion`, và các giá trị rotation để dùng lại trong pipeline.

## Mode 2: Review object từ result.json

Mode này dùng khi bạn có output layout/normalizer chứa objects có `modelUrl` và `rotation`.

```bash
python manual_local_glb_orientation.py --result ../backend/cases/.../result.json
```

Trong UI:

1. Chọn object ở danh sách bên trái.
2. Viewer sẽ load GLB qua proxy từ `modelUrl`.
3. Chỉnh yaw/tilt trên base rotation đang có trong `result.json`.
4. Bấm `Save correction JSON`.

Output được ghi ở current working directory:

```text
orientation_correction_<object_id>.json
```

Trong file output:

- `base_rotation`: rotation gốc từ `result.json`.
- `correction_quaternion`: quaternion correction để nhân vào catalog `defaultRotation`.
- `corrected_rotation`: quaternion cuối cùng nếu muốn ghi trực tiếp lại vào object trong `result.json`. -->

## Mode 3: Catalog mode

Mode này cho phép tìm item catalog, tải GLB về local, chỉnh hướng, rồi gửi `defaultRotation` lên Catalog API.

```bash
python manual_local_glb_orientation.py --catalog
```

Luồng dùng:

1. Nhập tên đồ vào ô `Tìm đồ trong catalog`.
2. Giữ `Null only` nếu chỉ muốn tìm item chưa có `defaultRotation`.
3. Bấm `Tìm`.
4. Tick các item cần review.
5. Bấm `Tải GLB đã chọn`.
6. Chọn từng model, chỉnh `Front`, `Tilt`, rồi bấm `Save orientation`.
7. Sau khi review xong các item cần thiết, bấm `Gửi lên catalog`.

Catalog mode tạo/cập nhật:

```text
catalog_downloads/<catalog_item_id>.glb
catalog_downloads/<catalog_item_id>.glb.orientation.json
catalog_session.json
```

`Gửi lên catalog` sẽ đọc các file `.orientation.json` đã lưu và gửi:

```json
{
  "defaultRotation": [x, y, z, w]
}
```

vào endpoint:

```text
PUT https://auto-furniture-api2.a-star.group/api/catalog/items/<catalog_item_id>
```

Chỉ bấm `Gửi lên catalog` khi đã kiểm tra kỹ hướng của từng model.

## Ý nghĩa các nút chỉnh hướng

- `Front is -Z / 0°`: mặt trước model đã trùng hướng chuẩn viewer.
- `Front is +X / 90°`: xoay model 90° quanh trục Y.
- `Front is +Z / 180°`: xoay model 180° quanh trục Y.
- `Front is -X / 270°`: xoay model 270° quanh trục Y.
- `X/Z ±90`: chỉnh object bị nghiêng hoặc nằm sai trục.
- `Reset tilt`: đưa tilt X/Z về `0`.

Chuẩn viewer: mũi tên trắng `PLAN FRONT` là hướng front chuẩn của plan trong scene Three.js.

## Troubleshooting

**Trang báo `list index out of range`**

Server đang chạy code cũ hoặc catalog chưa có GLB mà code chưa được restart. Dừng server và chạy lại tool.

**Search catalog báo JSON parse error hoặc `Unexpected token`**

Upstream Catalog API đang trả lỗi không phải JSON, hoặc server đang chạy code cũ. Restart tool. Code hiện tại luôn trả JSON lỗi rõ ràng cho `/catalog-api`.

**Sau download thấy `list indices must be integers or slices, not str`**

Server đang chạy code cũ đọc quaternion sai shape. Restart tool.

**Viewer báo `THREE.GLTFLoader: No DRACOLoader instance provided`**

GLB dùng Draco compression. Tool hiện đã bundle `DRACOLoader` và decoder trong `frontend/public/assets/three/draco/`; restart tool để nhận asset mới.

**Viewer trắng hoặc model không hiện**

Kiểm tra status text ở góc trái viewer. Nếu browser cache code cũ, reload hard refresh hoặc đổi port khi chạy tool. Nếu vẫn lỗi, mở `/model?file=<file.glb>` để kiểm tra GLB local có tải được không.

**Port đang bận**

Chạy port khác:

```bash
python manual_local_glb_orientation.py --catalog --port 8791
```

## Ghi chú

- Tool phục vụ review thủ công, không tự sửa database trừ khi bạn bấm `Gửi lên catalog`.
- Giữ nguyên thư mục `frontend/` cạnh `manual_local_glb_orientation.py`; viewer phụ thuộc các asset local trong đó.
- `catalog_downloads/` có thể lớn dần theo số GLB tải về.
