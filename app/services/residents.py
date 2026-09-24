from __future__ import annotations

import sqlite3

from app.core.errors import ConflictError, NotFoundError
from app.repositories.business import ResidentRepository
from app.services.audit import AuditContext, AuditService

# 居民档案维护目前走窗口免登链路，没有登录主体，审计以固定窗口身份记录。
WINDOW_ACTOR = AuditContext(actor_user_id=None, actor_name="窗口人员")

BLOCK_REASON = "resident_referenced_by_affairs"


class ResidentService:
    """居民档案业务规则，核心职责是删除前的引用保护与审计留痕。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.residents = ResidentRepository(connection)
        self.audit = AuditService(connection)

    def delete(self, actor: AuditContext, resident_id: int) -> dict:
        # 调用方在 BEGIN IMMEDIATE 事务内调用：该写锁与"新增事务"的写事务互斥，
        # 因此"存在性 -> 关联检查 -> 删除"在并发新增关联时不会出现检查后又被插入引用的空窗。
        resident = self.residents.get(resident_id)
        if resident is None:
            raise NotFoundError("居民不存在")

        references = self.residents.affair_references(resident_id)
        if references:
            raise ConflictError(
                f"该居民已关联 {len(references)} 条政务事务，不能删除；请先办结或迁移关联事务后再操作。",
                context={
                    "reason": BLOCK_REASON,
                    "resident_id": resident_id,
                    "resident_name": resident["name"],
                    "affair_count": len(references),
                    "affair_ids": [item["id"] for item in references],
                },
            )

        if not self.residents.delete(resident_id):
            # 写锁内理论上不会到达，仍兜底以保证响应确定。
            raise NotFoundError("居民不存在")

        self.audit.record(
            actor,
            action="resident.delete",
            resource_type="resident",
            resource_id=resident_id,
            before={key: resident[key] for key in ("id", "name", "id_card", "village")},
        )
        return {"message": "删除成功"}

    def record_blocked_delete(self, actor: AuditContext, resident_id: int, context: dict) -> None:
        """业务事务回滚后再独立落库的阻断审计，确保审计结果与本次冲突响应一致且持久可见。

        复用已回滚到自动提交状态的同一连接写入，metadata 直接取自冲突上下文，
        避免回滚与写入之间关联发生变化导致审计与响应不一致。
        """
        self.audit.record(
            actor,
            action="resident.delete.blocked",
            resource_type="resident",
            resource_id=resident_id,
            outcome="denied",
            metadata={
                "reason": context.get("reason", BLOCK_REASON),
                "resident_name": context.get("resident_name"),
                "affair_count": context.get("affair_count", 0),
                "affair_ids": context.get("affair_ids", []),
            },
        )
