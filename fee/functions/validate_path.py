from __future__ import annotations

import math
from typing import Any

from oag.ontology.repository import ObjectRepository


def _int(v: Any) -> int:
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    try:
        return int(str(v))
    except (ValueError, TypeError):
        return 0


def _str(v: Any) -> str | None:
    return str(v) if v is not None else None


def validate_path(store: ObjectRepository, path_id: int = 0) -> dict:
    if not path_id:
        return {"error": "需要参数: path_id"}

    path_id = int(path_id)
    path = store.query_by_id("MinimumFeePath", path_id)
    if not path:
        return {"error": f"路径 {path_id} 不存在"}

    intervals = [s.strip() for s in (path.get("toll_intervals") or "").split(",") if s.strip()]
    mfees = [_int(s.strip()) for s in (path.get("chargefee_group") or "").split(",") if s.strip()]
    efees = [_int(s.strip()) for s in (path.get("chargefee95_group") or "").split(",") if s.strip()]

    errors: list[dict] = []

    def add_error(rule: str, msg: str):
        errors.append({
            "error_id": f"{path_id}:{rule}:{len(errors) + 1}",
            "path_id": path_id,
            "rule_id": rule,
            "message": msg,
        })

    _check_contiguity(store, intervals, add_error)
    _check_formula(path, mfees, efees, add_error)
    _check_lengths(intervals, mfees, efees, add_error)
    _check_gantry_continuity(store, intervals, add_error)

    _replace_path_errors(store, path_id, errors)
    return {"path_id": path_id, "errors": errors, "passed": len(errors) == 0}


def _check_contiguity(store: ObjectRepository, intervals: list[str], add_error):
    for i in range(len(intervals) - 1):
        a, b = intervals[i], intervals[i + 1]
        connected = store.query("Contiguity", filters={
            "en_road_node_id": a,
            "en_road_node_type": 0,
            "ex_road_node_id": b,
            "ex_road_node_type": 0,
            "invalid": 0,
        }, limit=1)
        if not connected:
            add_error("V1", f"收费单元 {a} -> {b} 在路网中不相邻")


def _check_formula(path: dict, mfees: list[int], efees: list[int], add_error):
    total_fee = _int(path.get("total_fee"))
    total_fee95 = _int(path.get("total_fee95"))

    sum_mfee = sum(mfees)
    expected_fee = int(math.floor(sum_mfee / 100 + 0.5) * 100)
    if total_fee != expected_fee:
        add_error("V2", f"MTC总费额不一致: 记录={total_fee}, 计算={expected_fee}")

    sum_efee = sum(efees)
    expected_95 = int(min(sum_efee, math.floor(sum_mfee / 100) * 100 * 0.95))
    expected_95 = int(math.floor(expected_95 / 100 + 0.5) * 100)
    if total_fee95 != expected_95:
        add_error("V3", f"ETC总费额不一致: 记录={total_fee95}, 计算={expected_95}")


def _check_lengths(intervals: list[str], mfees: list[int], efees: list[int], add_error):
    if len(intervals) != len(mfees) or len(intervals) != len(efees):
        add_error(
            "V4",
            f"序列长度不一致: intervals={len(intervals)}, "
            f"mfees={len(mfees)}, efees={len(efees)}",
        )


def _check_gantry_continuity(store: ObjectRepository, intervals: list[str], add_error):
    unit_rows = {}
    for uid in intervals:
        unit = store.query_by_id("TollUnit", uid)
        if unit:
            unit_rows[uid] = _str(unit.get("gantry_id"))

    gantry_positions: dict[str, list[int]] = {}
    for i, uid in enumerate(intervals):
        gid = unit_rows.get(uid)
        if gid:
            gantry_positions.setdefault(gid, []).append(i)

    for gid, positions in gantry_positions.items():
        if len(positions) <= 1:
            continue
        for j in range(1, len(positions)):
            if positions[j] - positions[j - 1] != 1:
                add_error("V5", f"门架 {gid} 的收费单元在路径中不连续 (位置: {positions})")
                break


def _replace_path_errors(store: ObjectRepository, path_id: int, errors: list[dict]):
    adapter = store.adapter_for("ValidationError")
    delete_where = getattr(adapter, "delete_where", None)
    if callable(delete_where):
        delete_where({"path_id": path_id})
    else:
        for row in store.query("ValidationError", filters={"path_id": path_id}):
            store.delete_record("ValidationError", row["error_id"])
    for error in errors:
        store.insert_record("ValidationError", error)
