import sqlite3
from fastapi import APIRouter, HTTPException, Query
from typing import Optional
from app.database import get_connection, transaction
from app.models import AffairCreate, AffairProcess, AffairStatus

router = APIRouter(prefix="/affairs", tags=["事务办理"])


@router.post("", status_code=201)
def create_affair(affair: AffairCreate):
    # IMMEDIATE 写锁把"申请人存在性校验 + 事务插入"合并为一个原子事务，
    # 与居民删除的写事务互斥：申请人在校验后被删除的并发窗口不再存在。
    try:
        with transaction(immediate=True) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM residents WHERE id = ?", (affair.applicant_id,))
            if not cursor.fetchone():
                raise HTTPException(status_code=404, detail="申请人不存在")

            cursor.execute(
                """INSERT INTO affairs (title, category, applicant_id, description)
                   VALUES (?, ?, ?, ?)""",
                (affair.title, affair.category.value, affair.applicant_id, affair.description)
            )
            return {"id": cursor.lastrowid, "message": "事务提交成功"}
    except sqlite3.IntegrityError:
        # 兜底：极端并发下申请人在写锁获取瞬间已被删除，外键约束拒绝插入，
        # 统一返回确定的业务 404，而非把数据库错误泄漏成 500。
        raise HTTPException(status_code=404, detail="申请人不存在")


@router.get("")
def list_affairs(
    status: Optional[AffairStatus] = None,
    category: Optional[str] = None,
    applicant_id: Optional[int] = None,
    department_id: Optional[int] = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100)
):
    conn = get_connection()
    conditions = []
    params = []
    if status:
        conditions.append("a.status = ?")
        params.append(status.value)
    if category:
        conditions.append("a.category = ?")
        params.append(category)
    if applicant_id:
        conditions.append("a.applicant_id = ?")
        params.append(applicant_id)
    if department_id:
        conditions.append("a.department_id = ?")
        params.append(department_id)

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    count_sql = f"SELECT COUNT(*) as total FROM affairs a{where_clause}"
    cursor = conn.cursor()
    cursor.execute(count_sql, params)
    total = cursor.fetchone()["total"]

    offset = (page - 1) * size
    query_sql = f"""SELECT a.*, r.name as applicant_name, d.name as department_name
                    FROM affairs a
                    LEFT JOIN residents r ON a.applicant_id = r.id
                    LEFT JOIN departments d ON a.department_id = d.id
                    {where_clause}
                    ORDER BY a.created_at DESC LIMIT ? OFFSET ?"""
    cursor.execute(query_sql, params + [size, offset])
    rows = cursor.fetchall()

    return {
        "total": total,
        "page": page,
        "size": size,
        "data": [dict(row) for row in rows]
    }


@router.get("/{affair_id}")
def get_affair(affair_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT a.*, r.name as applicant_name, r.phone as applicant_phone,
           d.name as department_name, d.manager as department_manager, d.phone as department_phone
           FROM affairs a
           LEFT JOIN residents r ON a.applicant_id = r.id
           LEFT JOIN departments d ON a.department_id = d.id
           WHERE a.id = ?""",
        (affair_id,)
    )
    row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="事务不存在")
    return dict(row)


@router.put("/{affair_id}/process")
def process_affair(affair_id: int, data: AffairProcess):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM affairs WHERE id = ?", (affair_id,))
    row = cursor.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="事务不存在")

    current_status = row["status"]
    new_status = data.status.value

    valid_transitions = {
        "待受理": ["办理中", "已退回"],
        "办理中": ["已办结", "已退回"],
        "已退回": ["待受理"],
        "已办结": []
    }

    if new_status not in valid_transitions.get(current_status, []):
        raise HTTPException(
            status_code=400,
            detail=f"状态不允许从'{current_status}'转换到'{new_status}'"
        )

    if data.department_id is not None:
        cursor.execute("SELECT id FROM departments WHERE id = ?", (data.department_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="承办部门不存在")

    cursor.execute(
        """UPDATE affairs SET status = ?, department_id = COALESCE(?, department_id),
           handler = ?, result = ?, updated_at = datetime('now') WHERE id = ?""",
        (new_status, data.department_id, data.handler, data.result, affair_id)
    )
    conn.commit()
    return {"message": "事务处理成功", "status": new_status}
