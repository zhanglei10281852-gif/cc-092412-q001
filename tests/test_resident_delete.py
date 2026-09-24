from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from test_legacy_business import create_department, create_resident


def create_affair(client, resident_id: int, title: str = "社保材料补录"):
    return client.post(
        "/affairs",
        json={"title": title, "category": "社保", "applicant_id": resident_id, "description": "补录材料"},
    )


def test_delete_resident_with_affairs_returns_stable_conflict(client):
    resident_id = create_resident(client)
    affair = create_affair(client, resident_id)
    assert affair.status_code == 201
    affair_id = affair.json()["id"]

    first = client.delete(f"/residents/{resident_id}")
    assert first.status_code == 409
    assert first.json()["detail"] == "该居民存在1条关联政务事务，无法删除"

    # 连续重试得到完全一致的确定响应
    for _ in range(3):
        retry = client.delete(f"/residents/{resident_id}")
        assert retry.status_code == 409
        assert retry.json() == first.json()

    # 居民与关联事务保持完整，关联查询结果不变
    assert client.get(f"/residents/{resident_id}").status_code == 200
    affairs = client.get("/affairs", params={"applicant_id": resident_id}).json()
    assert affairs["total"] == 1
    assert affairs["data"][0]["id"] == affair_id
    detail = client.get(f"/affairs/{affair_id}")
    assert detail.status_code == 200
    assert detail.json()["applicant_id"] == resident_id


def test_delete_resident_without_references_succeeds(client):
    resident_id = create_resident(client)

    deleted = client.delete(f"/residents/{resident_id}")
    assert deleted.status_code == 200
    assert deleted.json() == {"message": "删除成功"}

    assert client.get(f"/residents/{resident_id}").status_code == 404
    again = client.delete(f"/residents/{resident_id}")
    assert again.status_code == 404
    assert again.json()["detail"] == "居民不存在"


def test_delete_missing_resident_returns_404(client):
    for _ in range(2):
        response = client.delete("/residents/999999")
        assert response.status_code == 404
        assert response.json()["detail"] == "居民不存在"


def test_delete_resident_does_not_touch_others(client):
    referenced = create_resident(client)
    removable = client.post(
        "/residents",
        json={"name": "李四", "id_card": "110101199202022345", "gender": "女", "birth_date": "1992-02-02",
              "address": "幸福路二号", "village": "幸福村"},
    )
    assert removable.status_code == 201
    removable_id = removable.json()["id"]
    assert create_affair(client, referenced).status_code == 201

    assert client.delete(f"/residents/{removable_id}").status_code == 200
    assert client.delete(f"/residents/{referenced}").status_code == 409

    remaining = client.get("/residents").json()
    assert remaining["total"] == 1
    assert remaining["data"][0]["id"] == referenced
    assert client.get(f"/residents/{referenced}").status_code == 200


def test_delete_resident_race_with_new_affair_still_conflict(client, monkeypatch):
    resident_id = create_resident(client)
    assert create_affair(client, resident_id).status_code == 201

    # 模拟并发新增关联：引用检查返回 0，但删除时外键约束仍然生效
    from app.repositories.business import ResidentRepository

    monkeypatch.setattr(
        ResidentRepository,
        "dependency_counts",
        lambda self, resident_id: {"affairs": 0},
    )

    response = client.delete(f"/residents/{resident_id}")
    assert response.status_code == 409
    assert response.json()["detail"] == "该居民存在关联政务事务，无法删除"

    # 兜底分支同样不破坏任何数据
    assert client.get(f"/residents/{resident_id}").status_code == 200
    assert client.get("/affairs", params={"applicant_id": resident_id}).json()["total"] == 1


def test_concurrent_deletes_and_affair_creation_stay_deterministic(client):
    resident_id = create_resident(client)
    department_id = create_department(client)

    def delete_once():
        return client.delete(f"/residents/{resident_id}").status_code

    def create_affair_once(index: int):
        return client.post(
            "/affairs",
            json={"title": f"并发事务{index}", "category": "社保", "applicant_id": resident_id},
        ).status_code

    with ThreadPoolExecutor(max_workers=12) as pool:
        delete_futures = [pool.submit(delete_once) for _ in range(6)]
        affair_futures = [pool.submit(create_affair_once, index) for index in range(6)]
        delete_results = [future.result() for future in delete_futures]
        affair_results = [future.result() for future in affair_futures]

    # 删除只允许成功一次、稳定冲突或居民已不存在，新增关联只允许受理或申请人不存在
    assert set(delete_results) <= {200, 404, 409}
    assert delete_results.count(200) <= 1
    assert set(affair_results) <= {201, 404}

    resident = client.get(f"/residents/{resident_id}")
    affairs = client.get("/affairs", params={"applicant_id": resident_id}).json()
    if 200 in delete_results:
        # 删除成功后居民消失，且不会有事务关联到已删除居民
        assert resident.status_code == 404
        assert affairs["total"] == 0
    else:
        # 全部冲突则居民与事务完整保留
        assert resident.status_code == 200
        assert affairs["total"] == affair_results.count(201)

    # 事务办理流程不受删除链路影响
    other = create_resident(client)
    affair = create_affair(client, other)
    assert affair.status_code == 201
    processing = client.put(
        f"/affairs/{affair.json()['id']}/process",
        json={"status": "办理中", "department_id": department_id, "handler": "王经办"},
    )
    assert processing.status_code == 200
