from __future__ import annotations

import sqlite3
import threading

from tests.test_legacy_business import create_department, create_resident


def _create_affair(client, resident_id: int, title: str = "社保材料补录") -> int:
    response = client.post(
        "/affairs",
        json={"title": title, "category": "社保", "applicant_id": resident_id, "description": "材料"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_delete_blocked_when_referenced_returns_stable_conflict(client):
    resident_id = create_resident(client)
    affair_id = _create_affair(client, resident_id)

    first = client.delete(f"/residents/{resident_id}")
    assert first.status_code == 409
    body = first.json()["error"]
    assert body["code"] == "conflict"
    assert "政务事务" in body["message"]
    assert body["context"]["reason"] == "resident_referenced_by_affairs"
    assert body["context"]["affair_count"] == 1
    assert body["context"]["affair_ids"] == [affair_id]

    # 连续重试得到确定且一致的响应，而不是间歇性 500。
    second = client.delete(f"/residents/{resident_id}")
    assert second.status_code == 409
    assert second.json() == first.json()

    # 居民档案与关联事务都保持完整。
    assert client.get(f"/residents/{resident_id}").status_code == 200
    assert client.get(f"/affairs/{affair_id}").status_code == 200


def test_delete_missing_resident_is_deterministic_404(client):
    response = client.delete("/residents/999999")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    # 重复删除不存在的居民仍是同一个确定响应。
    assert client.delete("/residents/999999").status_code == 404


def test_delete_unreferenced_resident_succeeds(client):
    resident_id = create_resident(client)
    response = client.delete(f"/residents/{resident_id}")
    assert response.status_code == 200
    assert client.get(f"/residents/{resident_id}").status_code == 404
    # 已删除后再次删除按不存在处理。
    assert client.delete(f"/residents/{resident_id}").status_code == 404


def test_delete_does_not_affect_other_residents_and_affairs(client):
    keep_id = create_resident(client)
    keep_affair = _create_affair(client, keep_id)
    other_id = client.post(
        "/residents",
        json={"name": "李四", "id_card": "110101199002022345", "gender": "女",
              "birth_date": "1991-02-02", "address": "平安路二号", "village": "平安村"},
    ).json()["id"]

    assert client.delete(f"/residents/{other_id}").status_code == 200

    listed = client.get("/residents").json()
    assert listed["total"] == 1
    assert [row["id"] for row in listed["data"]] == [keep_id]
    assert client.get(f"/affairs/{keep_affair}").status_code == 200
    # 既有事务办理流程不受影响。
    department_id = create_department(client)
    processed = client.put(
        f"/affairs/{keep_affair}/process",
        json={"status": "办理中", "department_id": department_id, "handler": "王经办"},
    )
    assert processed.status_code == 200


def test_affair_creation_for_missing_applicant_is_404(client):
    response = client.post(
        "/affairs",
        json={"title": "无人申请", "category": "社保", "applicant_id": 999999, "description": "x"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "申请人不存在"


def test_delete_audit_matches_outcome(client, admin):
    referenced_id = create_resident(client)
    affair_id = _create_affair(client, referenced_id)
    client.delete(f"/residents/{referenced_id}")

    blocked = client.get(
        "/api/audit",
        params={"resource_type": "resident", "action": "resident.delete.blocked"},
        headers=admin["headers"],
    ).json()
    assert blocked["total"] == 1
    blocked_event = blocked["data"][0]
    assert blocked_event["outcome"] == "denied"
    import json
    metadata = json.loads(blocked_event["metadata_json"])
    assert metadata["affair_count"] == 1
    assert metadata["affair_ids"] == [affair_id]

    # 阻断事件存在，且成功删除事件不应出现。
    assert client.get(
        "/api/audit", params={"action": "resident.delete"}, headers=admin["headers"]
    ).json()["total"] == 0

    free_id = client.post(
        "/residents",
        json={"name": "赵五", "id_card": "110101199003033456", "gender": "男",
              "birth_date": "1992-03-03", "address": "康乐路三号", "village": "康乐村"},
    ).json()["id"]
    assert client.delete(f"/residents/{free_id}").status_code == 200
    success = client.get(
        "/api/audit", params={"action": "resident.delete"}, headers=admin["headers"]
    ).json()
    assert success["total"] == 1
    assert success["data"][0]["outcome"] == "success"


def test_concurrent_delete_and_affair_insert_is_deterministic(client):
    """删除与新增事务并发竞争：只能二选一，绝不出现 500 或孤儿事务。"""
    from app.database import get_connection, transaction
    from app.core.errors import ConflictError, NotFoundError
    from app.routers.residents import delete_resident

    def make_resident(seed: int) -> int:
        conn = get_connection()
        cursor = conn.execute(
            "INSERT INTO residents(name,id_card,gender,birth_date,address,village) "
            "VALUES(?,?,?,?,?,?)",
            (f"竞态{seed}", f"999999{seed:06d}", "男", "1990-01-01", "址", "村"),
        )
        conn.commit()
        return int(cursor.lastrowid)

    def insert_affair(resident_id: int, outcome: dict) -> None:
        conn = get_connection()
        try:
            with transaction(immediate=True):
                if not conn.execute(
                    "SELECT id FROM residents WHERE id=?", (resident_id,)
                ).fetchone():
                    raise NotFoundError("申请人不存在")
                conn.execute(
                    "INSERT INTO affairs(title,category,applicant_id,description) "
                    "VALUES(?,?,?,?)",
                    ("并发事务", "社保", resident_id, "x"),
                )
            outcome["affair"] = 201
        except NotFoundError:
            outcome["affair"] = 404
        except sqlite3.IntegrityError:
            outcome["affair"] = 404

    def remove(resident_id: int, outcome: dict) -> None:
        try:
            delete_resident(resident_id)
            outcome["delete"] = 200
        except ConflictError:
            outcome["delete"] = 409
        except NotFoundError:
            outcome["delete"] = 404

    observed = set()
    for seed in range(20):
        resident_id = make_resident(seed)
        outcome: dict = {}
        barrier = threading.Barrier(2)

        def run_inserter() -> None:
            barrier.wait()
            insert_affair(resident_id, outcome)

        def run_deleter() -> None:
            barrier.wait()
            remove(resident_id, outcome)

        t1 = threading.Thread(target=run_inserter)
        t2 = threading.Thread(target=run_deleter)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        conn = get_connection()
        resident = conn.execute(
            "SELECT id FROM residents WHERE id=?", (resident_id,)
        ).fetchone()
        affair_count = conn.execute(
            "SELECT COUNT(*) FROM affairs WHERE applicant_id=?", (resident_id,)
        ).fetchone()[0]
        orphan_count = conn.execute(
            "SELECT COUNT(*) FROM affairs a LEFT JOIN residents r ON r.id=a.applicant_id "
            "WHERE r.id IS NULL"
        ).fetchone()[0]

        assert orphan_count == 0
        assert outcome["delete"] in (200, 409)
        assert outcome["affair"] in (201, 404)
        if outcome["delete"] == 200:
            # 删除胜出：居民已删、无关联事务，并发新增必然被拒。
            assert resident is None and affair_count == 0 and outcome["affair"] == 404
        else:
            # 新增胜出：居民保留、事务存在，删除必然冲突。
            assert resident is not None and affair_count == 1 and outcome["affair"] == 201
        observed.add((outcome["delete"], outcome["affair"]))

    # 两种时序都应在足够多轮中出现，证明两条路径都被并发覆盖。
    assert observed == {(200, 404), (409, 201)}
