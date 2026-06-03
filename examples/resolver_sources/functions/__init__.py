from __future__ import annotations

import sqlite3
from typing import Any

from oag_ontology.registry import FunctionRegistry
from oag_ontology.repository import ObjectRepository
from oag_ontology.schema import Ontology


class MemoryAdapter:
    def __init__(self, id_field: str):
        self.id_field = id_field
        self.rows: list[dict] = []

    def query(self, object_type: str, filters: dict[str, Any] | None = None,
              limit: int | None = None, order_by: str | None = None,
              offset: int | None = None) -> list[dict]:
        rows = _apply_filters(self.rows, filters)
        rows = _apply_order(rows, order_by)
        return _apply_window([dict(row) for row in rows], limit, offset)

    def count(self, object_type: str,
              filters: dict[str, Any] | None = None) -> int:
        return len(_apply_filters(self.rows, filters))

    def query_by_id(self, object_type: str, id_value: Any) -> dict | None:
        rows = self.query(object_type, {self.id_field: id_value}, limit=1)
        return rows[0] if rows else None

    def search_text(self, keyword: str, object_types: list[str] | None = None,
                    limit: int = 20) -> list[dict]:
        return []

    def insert_record(self, object_type: str, data: dict) -> dict:
        self.rows.append(dict(data))
        return {"inserted": 1}

    def update_record(self, object_type: str, id_value: Any, data: dict) -> dict:
        updated = 0
        for row in self.rows:
            if row.get(self.id_field) == id_value:
                row.update(dict(data))
                updated += 1
                break
        return {"updated": updated}

    def delete_record(self, object_type: str, id_value: Any) -> dict:
        before = len(self.rows)
        self.rows = [row for row in self.rows if row.get(self.id_field) != id_value]
        return {"deleted": before - len(self.rows)}

    def table_count(self, object_type: str) -> int:
        return len(self.rows)


class AccountBalanceSqlViewAdapter:
    """Read-only ObjectAdapter that looks like MySQL, using SQLite here."""

    def __init__(self):
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._seed()

    def query(self, object_type: str, filters: dict[str, Any] | None = None,
              limit: int | None = None, order_by: str | None = None,
              offset: int | None = None) -> list[dict]:
        sql = """
            SELECT
                a.account_id,
                a.customer_id,
                a.status,
                COALESCE(SUM(t.amount), 0) AS balance
            FROM accounts a
            LEFT JOIN account_transactions t ON t.account_id = a.account_id
        """
        where, params = _sql_where(filters, {
            "account_id": "a.account_id",
            "customer_id": "a.customer_id",
            "status": "a.status",
        })
        if where:
            sql += f" WHERE {where}"
        sql += " GROUP BY a.account_id, a.customer_id, a.status"
        if order_by:
            col = order_by.lstrip("-")
            allowed = {
                "account_id": "a.account_id",
                "customer_id": "a.customer_id",
                "status": "a.status",
                "balance": "balance",
            }
            if col in allowed:
                direction = "DESC" if order_by.startswith("-") else "ASC"
                sql += f" ORDER BY {allowed[col]} {direction}"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        if offset:
            sql += " OFFSET ?"
            params.append(offset)
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def count(self, object_type: str,
              filters: dict[str, Any] | None = None) -> int:
        return len(self.query(object_type, filters))

    def query_by_id(self, object_type: str, id_value: Any) -> dict | None:
        rows = self.query(object_type, {"account_id": id_value}, limit=1)
        return rows[0] if rows else None

    def search_text(self, keyword: str, object_types: list[str] | None = None,
                    limit: int = 20) -> list[dict]:
        return []

    def insert_record(self, object_type: str, data: dict) -> dict:
        raise ValueError(f"{object_type} 是只读 SQL 视图对象")

    def update_record(self, object_type: str, id_value: Any, data: dict) -> dict:
        raise ValueError(f"{object_type} 是只读 SQL 视图对象")

    def delete_record(self, object_type: str, id_value: Any) -> dict:
        raise ValueError(f"{object_type} 是只读 SQL 视图对象")

    def table_count(self, object_type: str) -> int:
        return self.count(object_type)

    def _seed(self):
        self.conn.executescript("""
            CREATE TABLE accounts (
                account_id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE account_transactions (
                transaction_id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                amount REAL NOT NULL
            );
        """)
        self.conn.executemany(
            "INSERT INTO accounts VALUES (?, ?, ?)",
            [
                ("A100", "C001", "active"),
                ("A101", "C001", "active"),
                ("A200", "C002", "frozen"),
                ("A300", "C003", "active"),
            ],
        )
        self.conn.executemany(
            "INSERT INTO account_transactions VALUES (?, ?, ?)",
            [
                ("T1", "A100", 1200.0),
                ("T2", "A100", -200.0),
                ("T3", "A101", 300.0),
                ("T4", "A200", -50.0),
                ("T5", "A300", 8000.0),
            ],
        )
        self.conn.commit()


class CustomerRiskResolver:
    """Resolver composed from two other resolvers."""

    def __init__(self, repository: ObjectRepository):
        self.repository = repository

    def query(self, filters: dict[str, Any] | None = None,
              limit: int | None = None, order_by: str | None = None,
              offset: int | None = None) -> list[dict]:
        rows = []
        for profile in self.repository.query("CustomerProfile"):
            accounts = self.repository.query(
                "AccountBalance",
                {"customer_id": profile["customer_id"]},
            )
            total = sum(float(account["balance"]) for account in accounts)
            rows.append({
                "customer_id": profile["customer_id"],
                "name": profile["name"],
                "tier": profile["tier"],
                "total_balance": total,
                "risk_level": _risk_level(total, accounts),
            })
        rows = _apply_filters(rows, filters)
        rows = _apply_order(rows, order_by)
        return _apply_window(rows, limit, offset)

    def count(self, filters: dict[str, Any] | None = None) -> int:
        return len(self.query(filters))

    def query_by_id(self, id_value: Any) -> dict | None:
        rows = self.query({"customer_id": id_value}, limit=1)
        return rows[0] if rows else None


def register(registry: FunctionRegistry, repository: ObjectRepository, ontology: Ontology):
    sql_view_adapter = AccountBalanceSqlViewAdapter()

    def sql_view_adapter_factory(**kw):
        return sql_view_adapter

    def memory_adapter_factory(source, **kw):
        return MemoryAdapter(source.id_field or "id")

    registry.register_adapter("sql_view", sql_view_adapter_factory)
    registry.register_adapter("memory", memory_adapter_factory)

    risks = CustomerRiskResolver(repository)
    registry.register_resolver("customer_risk_view", risks)


def _apply_filters(rows: list[dict], filters: dict[str, Any] | None) -> list[dict]:
    result = list(rows)
    for key, value in (filters or {}).items():
        if "__" in key:
            field, op = key.split("__", 1)
            if op == "gte":
                result = [row for row in result if row.get(field) >= value]
            elif op == "lte":
                result = [row for row in result if row.get(field) <= value]
            elif op == "like":
                result = [row for row in result if value in str(row.get(field, ""))]
            else:
                result = [row for row in result if row.get(field) == value]
        else:
            result = [row for row in result if row.get(key) == value]
    return result


def _apply_order(rows: list[dict], order_by: str | None) -> list[dict]:
    if not order_by:
        return rows
    reverse = order_by.startswith("-")
    field = order_by.lstrip("-")
    return sorted(rows, key=lambda row: row.get(field), reverse=reverse)


def _apply_window(rows: list[dict], limit: int | None,
                  offset: int | None) -> list[dict]:
    if offset:
        rows = rows[offset:]
    if limit:
        rows = rows[:limit]
    return rows


def _sql_where(filters: dict[str, Any] | None,
               columns: dict[str, str]) -> tuple[str, list[Any]]:
    clauses = []
    params: list[Any] = []
    for key, value in (filters or {}).items():
        field, op = key.split("__", 1) if "__" in key else (key, "eq")
        column = columns.get(field)
        if not column:
            continue
        if op == "like":
            clauses.append(f"{column} LIKE ?")
            params.append(f"%{value}%")
        elif op == "ne":
            clauses.append(f"{column} != ?")
            params.append(value)
        else:
            clauses.append(f"{column} = ?")
            params.append(value)
    return " AND ".join(clauses), params


def _risk_level(total_balance: float, accounts: list[dict]) -> str:
    if any(account["status"] == "frozen" for account in accounts):
        return "review"
    if total_balance < 0:
        return "high"
    if total_balance > 5000:
        return "low"
    return "normal"
