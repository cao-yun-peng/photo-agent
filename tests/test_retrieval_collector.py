"""真实检索采集器的输入映射回归测试。"""

from __future__ import annotations

import json

from scripts.collect_retrieval_results import _load_uuid_map


def test_collector_accepts_import_map(tmp_path) -> None:
    path = tmp_path / "import-map.json"
    path.write_text(
        json.dumps(
            {
                "records": [
                    {"dataset_id": "p-004", "photo_id": "photo-uuid-1"},
                    {"dataset_id": "p-006", "photo_id": "photo-uuid-2"},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert _load_uuid_map(str(path)) == {
        "photo-uuid-1": "p-004",
        "photo-uuid-2": "p-006",
    }


def test_collector_still_accepts_flat_uuid_map(tmp_path) -> None:
    path = tmp_path / "uuid-map.json"
    path.write_text(json.dumps({"photo-uuid-1": "p-004"}), encoding="utf-8")
    assert _load_uuid_map(str(path)) == {"photo-uuid-1": "p-004"}
