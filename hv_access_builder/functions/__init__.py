from __future__ import annotations

import json
import math
import uuid
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Any

from oag.ontology.registry import FunctionRegistry
from oag.ontology.repository import ObjectRepository
from oag.ontology.schema import Ontology


class RuntimeMemoryAdapter:
    """Writable in-process adapter for generated reasoning artifacts."""

    def __init__(self, ontology: Ontology, object_type: str,
                 source, domain_dir: Path):
        self.ontology = ontology
        self.object_type = object_type
        self.source = source
        self.domain_dir = domain_dir
        self.id_field = source.id_field or ontology.get_id_column(object_type)
        self.rows = self._load_seed()

    @classmethod
    def factory(cls, domain_dir: str | Path):
        base_dir = Path(domain_dir).resolve()

        def build(ontology: Ontology, object_type: str, source, **kwargs):
            return cls(ontology, object_type, source, base_dir)

        return build

    def query(self, object_type: str, filters: dict[str, Any] | None = None,
              limit: int | None = None, order_by: str | None = None,
              offset: int | None = None) -> list[dict]:
        rows = _apply_filters([dict(row) for row in self.rows], filters)
        rows = _apply_order(rows, order_by)
        return _apply_window(rows, limit, offset)

    def count(self, object_type: str,
              filters: dict[str, Any] | None = None) -> int:
        return len(self.query(object_type, filters))

    def query_by_id(self, object_type: str, id_value: Any) -> dict | None:
        if not self.id_field:
            return None
        rows = self.query(object_type, {self.id_field: id_value}, limit=1)
        return rows[0] if rows else None

    def search_text(self, keyword: str, object_types: list[str] | None = None,
                    limit: int = 20) -> list[dict]:
        if not keyword:
            return []
        obj_def = self.ontology.objects[self.object_type]
        text_cols = [name for name, prop in obj_def.properties.items() if prop.type == "str"]
        results = []
        for row in self.rows:
            matched = [
                col for col in text_cols
                if row.get(col) and keyword in str(row[col])
            ]
            if matched:
                record = dict(row)
                record["_object_type"] = self.object_type
                record["_matched_field"] = ", ".join(matched)
                results.append(record)
            if len(results) >= limit:
                break
        return results

    def insert_record(self, object_type: str, data: dict) -> dict:
        self.rows.append(self._project(data))
        return {"inserted": 1}

    def update_record(self, object_type: str, id_value: Any, data: dict) -> dict:
        if not self.id_field:
            raise ValueError(f"{object_type} 没有声明 id 字段，不能 update")
        updated = 0
        patch = self._project(data)
        for row in self.rows:
            if row.get(self.id_field) == id_value:
                row.update({k: v for k, v in patch.items() if k != self.id_field})
                updated += 1
                break
        return {"updated": updated}

    def delete_record(self, object_type: str, id_value: Any) -> dict:
        if not self.id_field:
            raise ValueError(f"{object_type} 没有声明 id 字段，不能 delete")
        before = len(self.rows)
        self.rows = [row for row in self.rows if row.get(self.id_field) != id_value]
        return {"deleted": before - len(self.rows)}

    def table_count(self, object_type: str) -> int:
        return len(self.rows)

    def _load_seed(self) -> list[dict]:
        raw = self.source.config.get("seed_path")
        if not raw:
            return []
        path = Path(raw)
        if not path.is_absolute():
            path = self.domain_dir / path
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("data", data.get("items", []))
        return [self._project(row) for row in data]

    def _project(self, data: dict) -> dict:
        valid = set(self.ontology.objects[self.object_type].properties.keys())
        return {key: value for key, value in data.items() if key in valid}


DOMAIN_DIR = Path(__file__).resolve().parent.parent


def _gen_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8].upper()}"


def _to_float(value: Any, default: float = 0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).lower() in {"1", "true", "yes", "是", "启用"}


def _split_csv(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(v) for v in value if str(v)]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _coordinate(value: Any) -> dict:
    if isinstance(value, dict):
        return {"lng": _to_float(value.get("lng")), "lat": _to_float(value.get("lat"))}
    if isinstance(value, str) and "," in value:
        lng, lat = value.split(",", 1)
        return {"lng": _to_float(lng), "lat": _to_float(lat)}
    return {"lng": 0, "lat": 0}


def _distance_m(a: dict, b: dict) -> float:
    lng1, lat1 = _to_float(a.get("lng")), _to_float(a.get("lat"))
    lng2, lat2 = _to_float(b.get("lng")), _to_float(b.get("lat"))
    radius = 6371000
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    c = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlng / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(c), math.sqrt(1 - c))


def _apply_filters(rows: list[dict], filters: dict[str, Any] | None) -> list[dict]:
    result = list(rows)
    for key, value in (filters or {}).items():
        field, op = key.split("__", 1) if "__" in key else (key, "eq")
        if op == "gt":
            result = [row for row in result if row.get(field) > value]
        elif op == "gte":
            result = [row for row in result if row.get(field) >= value]
        elif op == "lt":
            result = [row for row in result if row.get(field) < value]
        elif op == "lte":
            result = [row for row in result if row.get(field) <= value]
        elif op == "ne":
            result = [row for row in result if row.get(field) != value]
        elif op == "like":
            result = [row for row in result if str(value) in str(row.get(field, ""))]
        else:
            result = [row for row in result if row.get(field) == value]
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


def _latest(store: ObjectRepository, object_type: str, request_id: str) -> dict | None:
    rows = store.query(object_type, filters={"request_id": request_id})
    return rows[-1] if rows else None


def _request(store: ObjectRepository, request_id: str) -> dict:
    req = store.query_by_id("HighVoltageConnectionRequest", request_id)
    if not req:
        raise ValueError(f"未找到高压接入请求 {request_id}")
    return req


def _capacity_kva(req: dict) -> float:
    return _to_float(
        req.get("total_receiving_transformer_capacity_kva")
        or req.get("approved_contract_capacity_kva")
    )


def _single_route_capacity(store: ObjectRepository, request_id: str,
                           explicit: float = 0) -> float:
    if explicit:
        return _to_float(explicit)
    req = _request(store, request_id)
    decision = _latest(store, "SupplyModeDecision", request_id)
    route_count = _to_float((decision or {}).get("minimum_route_count"), 1) or 1
    return round(_capacity_kva(req) / route_count, 2)


def _rule(store: ObjectRepository, object_type: str, rule_id: str = "") -> dict:
    if rule_id:
        row = store.query_by_id(object_type, rule_id)
        if row:
            return row
    rows = store.query(object_type, limit=1)
    return rows[0] if rows else {}


def _config(store: ObjectRepository, request_id: str, config_id: str = "") -> dict:
    if config_id:
        row = store.query_by_id("ConnectionReasoningConfig", config_id)
        if row:
            return row
    rows = store.query("ConnectionReasoningConfig", filters={"request_id": request_id})
    if rows:
        return rows[-1]
    rows = store.query("ConnectionReasoningConfig", filters={"request_id": ""})
    return rows[-1] if rows else {}


def _feeder(store: ObjectRepository, feeder_id: str) -> dict:
    return store.query_by_id("Feeder", feeder_id) or {}


def _transformer(store: ObjectRepository, transformer_id: str) -> dict:
    return store.query_by_id("SubstationTransformer", transformer_id) or {}


def _point(store: ObjectRepository, access_point_id: str) -> dict:
    return store.query_by_id("PowerAccessPoint", access_point_id) or {}


def _load_internal_json(name: str) -> list[dict]:
    path = DOMAIN_DIR / "data" / name
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("items", [])


def _validate_connection_scope(store: ObjectRepository, request_id: str = "",
                               rule_id: str = "", **kw) -> dict:
    req = _request(store, request_id)
    rule = _rule(store, "ConnectionScopeRule", rule_id)
    allowed = set(rule.get("allowed_business_types") or ["新装", "增容", "临时"])
    voltage = _to_float(req.get("supply_voltage_kv"))
    capacity = _capacity_kva(req)
    max_capacity = _to_float(rule.get("max_total_receiving_transformer_capacity_kva"), 20000)

    failed = []
    if req.get("business_type") not in allowed:
        failed.append(f"业务类型{req.get('business_type')}不在允许范围{sorted(allowed)}")
    if voltage != _to_float(rule.get("required_supply_voltage_kv"), 10):
        failed.append(f"供电电压{voltage:g}kV不等于10kV")
    if capacity > max_capacity:
        failed.append(f"容量{capacity:g}kVA超过上限{max_capacity:g}kVA")

    record = {
        "validation_id": _gen_id("SCOPE_"),
        "request_id": request_id,
        "is_in_scope": not failed,
        "failed_reasons": failed,
        "checked_business_type": req.get("business_type", ""),
        "checked_supply_voltage_kv": voltage,
        "checked_total_capacity_kva": capacity,
    }
    store.insert_record("ScopeValidationResult", record)
    return {"validation": record}


def _apply_reasoning_config_constraints(store: ObjectRepository, request_id: str = "",
                                        config_id: str = "", **kw) -> dict:
    req = _request(store, request_id)
    config = _config(store, request_id, config_id)
    allowed = config.get("allowed_source_device_types") or []
    pms_type = config.get("pms_preselected_source_type") or ""
    if pms_type:
        allowed = [pms_type]
    return {
        "request_id": request_id,
        "business_type": req.get("business_type"),
        "allowed_source_device_types": allowed,
        "add_redline_boundary_device": _to_bool(config.get("add_redline_boundary_device")),
        "enable_new_substation_outgoing_line": _to_bool(config.get("enable_new_substation_outgoing_line")),
        "pms_preselected_source_type": pms_type,
    }


def _decide_supply_mode(store: ObjectRepository, request_id: str = "",
                        rule_id: str = "", **kw) -> dict:
    req = _request(store, request_id)
    rules = store.query("PowerSupplyModeRule")
    if rule_id:
        rules = [row for row in rules if row.get("rule_id") == rule_id]
    matched = [
        row for row in rules
        if row.get("user_importance_level") == req.get("user_importance_level")
        and row.get("load_level") == req.get("load_level")
    ]
    rule = matched[0] if matched else {}
    requested = req.get("requested_operation_mode") or req.get("requested_supply_mode") or ""
    required = rule.get("required_supply_mode") or requested or "单电源"
    min_routes = int(rule.get("minimum_route_count") or (1 if required == "单电源" else 2))
    min_sources = int(rule.get("minimum_source_count") or min_routes)
    if _to_float(req.get("requested_power_source_count")) > min_sources:
        min_sources = int(_to_float(req.get("requested_power_source_count")))
        min_routes = max(min_routes, min_sources)
    if requested in {"双回路", "双电源", "多电源(3路含应急)"} and requested != required:
        required = requested

    independence = rule.get("source_independence_requirement") or ""
    record = {
        "decision_id": _gen_id("MODE_"),
        "request_id": request_id,
        "required_supply_mode": required,
        "minimum_route_count": min_routes,
        "minimum_source_count": min_sources,
        "required_different_substations": independence == "different_substation" or required == "双电源",
        "required_different_busbars": independence in {"different_busbar", "different_substation"} or required in {"双回路", "双电源"},
        "decision_reason": f"{req.get('user_importance_level')}/{req.get('load_level')}匹配供电方式{required}",
    }
    store.insert_record("SupplyModeDecision", record)
    return {"decision": record, "rule": rule}


def _search_power_access_points(store: ObjectRepository, request_id: str = "",
                                center_coordinate: str = "",
                                allowed_source_device_types: str = "",
                                radius_sequence_m: str = "200,500,800,1000",
                                required_distinct_feeder_count: int = 0,
                                **kw) -> dict:
    req = _request(store, request_id)
    config = _config(store, request_id)
    decision = _latest(store, "SupplyModeDecision", request_id) or {}
    center = _coordinate(center_coordinate) if center_coordinate else _coordinate(req.get("receiving_point_coordinate"))
    allowed = _split_csv(allowed_source_device_types) or config.get("allowed_source_device_types") or []
    radii = [int(_to_float(r)) for r in _split_csv(radius_sequence_m)] or [200, 500, 800, 1000]
    required = int(required_distinct_feeder_count or decision.get("minimum_route_count") or 1)

    found: list[dict] = []
    found_ids: set[str] = set()
    stopped = radii[-1]
    for radius in radii:
        for point in store.query("PowerAccessPoint"):
            point_id = point.get("access_point_id")
            if point_id in found_ids or not _to_bool(point.get("is_available")):
                continue
            if allowed and point.get("device_type") not in allowed:
                continue
            distance = _distance_m(center, _coordinate(point.get("coordinate")))
            if distance <= radius:
                row = dict(point)
                row["distance_to_receiving_point_m"] = round(distance, 1)
                found.append(row)
                found_ids.add(point_id)
        if len({row.get("feeder_id") for row in found}) >= required:
            stopped = radius
            break

    found.sort(key=lambda row: row.get("distance_to_receiving_point_m", 999999))
    record = {
        "search_batch_id": _gen_id("SEARCH_"),
        "request_id": request_id,
        "center_coordinate": center,
        "radius_sequence_m": radii,
        "stopped_radius_m": stopped,
        "found_access_point_ids": [row.get("access_point_id") for row in found],
        "distinct_feeder_count": len({row.get("feeder_id") for row in found}),
        "satisfies_required_line_count": len({row.get("feeder_id") for row in found}) >= required,
    }
    store.insert_record("PowerSourceSearchBatch", record)
    return {"search_batch": record, "points": found}


def _capacity_check_result(point: dict, feeder: dict, transformer: dict,
                           single_capacity: float, rule: dict) -> tuple[dict, list[str]]:
    failed = []
    feeder_available_capacity_ok = _to_float(feeder.get("available_capacity_kva")) >= single_capacity
    feeder_load_rate_ok = (
        _to_float(feeder.get("historical_max_load_rate")) <= _to_float(rule.get("max_feeder_historical_load_rate"), 0.8)
        and _to_float(feeder.get("current_load_rate")) <= _to_float(rule.get("max_feeder_current_load_rate"), 0.8)
    )
    spare_bay_ok = _to_float(point.get("available_bay_count")) >= _to_float(rule.get("min_spare_bay_count"), 2)
    connected_user_count_ok = _to_float(feeder.get("connected_user_count")) <= _to_float(rule.get("max_connected_user_count"), 50)
    line_loss_rate_ok = _to_float(feeder.get("line_loss_rate_after_connection")) <= _to_float(rule.get("max_line_loss_rate_after_connection"), 0.07)
    transformer_available_capacity_ok = _to_float(transformer.get("available_capacity_kva")) >= single_capacity
    transformer_load_rate_ok = _to_float(transformer.get("current_load_rate")) <= _to_float(rule.get("max_transformer_load_rate"), 0.8)

    checks = {
        "feeder_available_capacity_ok": feeder_available_capacity_ok,
        "feeder_load_rate_ok": feeder_load_rate_ok,
        "spare_bay_ok": spare_bay_ok,
        "connected_user_count_ok": connected_user_count_ok,
        "line_loss_rate_ok": line_loss_rate_ok,
        "transformer_available_capacity_ok": transformer_available_capacity_ok,
        "transformer_load_rate_ok": transformer_load_rate_ok,
    }
    for name, ok in checks.items():
        if not ok:
            failed.append(name)
    return checks, failed


def _check_access_point_capacity(store: ObjectRepository, request_id: str = "",
                                 access_point_ids: str = "",
                                 single_route_capacity_kva: float = 0,
                                 rule_id: str = "", **kw) -> dict:
    rule = _rule(store, "ExistingAccessPointConstraintRule", rule_id)
    batch = _latest(store, "PowerSourceSearchBatch", request_id) or {}
    point_ids = _split_csv(access_point_ids) or batch.get("found_access_point_ids") or []
    single_capacity = _single_route_capacity(store, request_id, single_route_capacity_kva)
    records = []
    for point_id in point_ids:
        point = _point(store, point_id)
        if not point:
            continue
        feeder = _feeder(store, point.get("feeder_id"))
        transformer = _transformer(store, point.get("incoming_source_id"))
        checks, failed = _capacity_check_result(point, feeder, transformer, single_capacity, rule)
        record = {
            "check_id": _gen_id("APCHK_"),
            "request_id": request_id,
            "access_point_id": point_id,
            "single_route_capacity_kva": single_capacity,
            **checks,
            "is_directly_usable": not failed,
            "requires_load_transfer": any(
                name in failed
                for name in ("feeder_available_capacity_ok", "feeder_load_rate_ok",
                             "transformer_available_capacity_ok", "transformer_load_rate_ok")
            ),
            "failed_constraints": failed,
        }
        store.insert_record("AccessPointCapacityCheck", record)
        records.append(record)
    return {
        "checks": records,
        "usable_access_point_ids": [r["access_point_id"] for r in records if r["is_directly_usable"]],
        "requires_load_transfer_ids": [r["access_point_id"] for r in records if r["requires_load_transfer"]],
    }


def _check_original_supply_capacity(store: ObjectRepository, request_id: str = "",
                                    rule_id: str = "", **kw) -> dict:
    rule = _rule(store, "ExistingAccessPointConstraintRule", rule_id)
    infos = store.query("ExpansionOriginalSupplyInfo", filters={"request_id": request_id})
    if not infos:
        return {"request_id": request_id, "skipped": True, "reason": "非增容场景或缺少原电源信息"}
    info = infos[-1]
    req = _request(store, request_id)
    feeder = _feeder(store, info.get("original_feeder_id"))
    transformer = _transformer(store, info.get("original_transformer_id"))
    single_capacity = _to_float(info.get("incremental_capacity_kva")) or _capacity_kva(req)
    fake_point = {"available_bay_count": 999}
    checks, failed = _capacity_check_result(fake_point, feeder, transformer, single_capacity, rule)
    record = {
        "check_id": _gen_id("ORIG_"),
        "request_id": request_id,
        "original_supply_info_id": info.get("original_supply_info_id"),
        "original_feeder_id": info.get("original_feeder_id"),
        "original_transformer_id": info.get("original_transformer_id"),
        "incremental_capacity_kva": single_capacity,
        "feeder_available_capacity_ok": checks["feeder_available_capacity_ok"],
        "feeder_load_rate_ok": checks["feeder_load_rate_ok"],
        "transformer_available_capacity_ok": checks["transformer_available_capacity_ok"],
        "transformer_load_rate_ok": checks["transformer_load_rate_ok"],
        "is_original_supply_usable": not failed,
        "requires_load_transfer": any(
            name in failed
            for name in ("feeder_available_capacity_ok", "feeder_load_rate_ok",
                         "transformer_available_capacity_ok", "transformer_load_rate_ok")
        ),
        "failed_constraints": failed,
    }
    store.insert_record("OriginalSupplyCapacityCheck", record)
    return {"check": record, "original_supply_info": info}


def _generate_load_transfer_plan(store: ObjectRepository, request_id: str = "",
                                 overloaded_object_type: str = "",
                                 overloaded_object_id: str = "",
                                 single_route_capacity_kva: float = 0,
                                 rule_id: str = "", **kw) -> dict:
    capacity = _single_route_capacity(store, request_id, single_route_capacity_kva)
    plans = []
    if not overloaded_object_type or not overloaded_object_id:
        candidates = store.query("AccessPointCapacityCheck", filters={"request_id": request_id})
        if candidates:
            check = next((row for row in candidates if row.get("requires_load_transfer")), candidates[-1])
            point = _point(store, check.get("access_point_id"))
            failed = set(check.get("failed_constraints") or [])
            if failed & {"transformer_available_capacity_ok", "transformer_load_rate_ok"}:
                overloaded_object_type = "SubstationTransformer"
                overloaded_object_id = point.get("incoming_source_id", "")
            else:
                overloaded_object_type = "Feeder"
                overloaded_object_id = point.get("feeder_id", "")
    if overloaded_object_type == "Feeder":
        source = _feeder(store, overloaded_object_id)
        ties = _load_internal_json("_feeder_tie_switch.json")
        receivers = []
        for tie in ties:
            if tie.get("source_feeder_id") == overloaded_object_id:
                receivers.append(tie.get("target_feeder_id"))
            elif tie.get("target_feeder_id") == overloaded_object_id:
                receivers.append(tie.get("source_feeder_id"))
        for receiver_id in receivers:
            receiver = _feeder(store, receiver_id)
            if _to_float(receiver.get("available_capacity_kva")) < capacity:
                continue
            post_rate = min(0.99, _to_float(receiver.get("current_load_rate")) + capacity / 50000)
            feasible = post_rate <= 0.8
            plans.append({
                "transfer_plan_id": _gen_id("TRF_"),
                "request_id": request_id,
                "overloaded_object_type": "Feeder",
                "overloaded_feeder_id": overloaded_object_id,
                "overloaded_transformer_id": source.get("transformer_id", ""),
                "receiver_feeder_id": receiver_id,
                "receiver_transformer_id": receiver.get("transformer_id", ""),
                "switches_used": [tie.get("switch_id") for tie in ties if receiver_id in (tie.get("source_feeder_id"), tie.get("target_feeder_id")) and overloaded_object_id in (tie.get("source_feeder_id"), tie.get("target_feeder_id"))],
                "transfer_capacity_kva": capacity,
                "post_transfer_feeder_load_rate": round(post_rate, 3),
                "post_transfer_transformer_load_rate": 0,
                "is_feasible": feasible,
                "infeasible_reason": "" if feasible else "接收馈线划接后负载率超过80%",
            })
    elif overloaded_object_type == "SubstationTransformer":
        source = _transformer(store, overloaded_object_id)
        for transformer in store.query("SubstationTransformer", filters={"substation_id": source.get("substation_id")}):
            if transformer.get("transformer_id") == overloaded_object_id:
                continue
            post_rate = min(0.99, _to_float(transformer.get("current_load_rate")) + capacity / 50000)
            feasible = _to_float(transformer.get("available_capacity_kva")) >= capacity and post_rate <= 0.8
            plans.append({
                "transfer_plan_id": _gen_id("TRF_"),
                "request_id": request_id,
                "overloaded_object_type": "SubstationTransformer",
                "overloaded_feeder_id": "",
                "overloaded_transformer_id": overloaded_object_id,
                "receiver_feeder_id": "",
                "receiver_transformer_id": transformer.get("transformer_id"),
                "switches_used": [],
                "transfer_capacity_kva": capacity,
                "post_transfer_feeder_load_rate": 0,
                "post_transfer_transformer_load_rate": round(post_rate, 3),
                "is_feasible": feasible,
                "infeasible_reason": "" if feasible else "接收主变容量或负载率不满足要求",
            })
    if not plans:
        plans.append({
            "transfer_plan_id": _gen_id("TRF_"),
            "request_id": request_id,
            "overloaded_object_type": overloaded_object_type,
            "overloaded_feeder_id": overloaded_object_id if overloaded_object_type == "Feeder" else "",
            "overloaded_transformer_id": overloaded_object_id if overloaded_object_type == "SubstationTransformer" else "",
            "receiver_feeder_id": "",
            "receiver_transformer_id": "",
            "switches_used": [],
            "transfer_capacity_kva": capacity,
            "post_transfer_feeder_load_rate": 0,
            "post_transfer_transformer_load_rate": 0,
            "is_feasible": False,
            "infeasible_reason": "未找到可行划接对象",
        })
    for plan in plans:
        store.insert_record("LoadTransferPlan", plan)
    return {"plans": plans, "feasible_plan_ids": [p["transfer_plan_id"] for p in plans if p["is_feasible"]]}


def _evaluate_new_substation_outgoing_line(store: ObjectRepository, request_id: str = "",
                                           single_route_capacity_kva: float = 0,
                                           trigger_rule_id: str = "",
                                           feasibility_rule_id: str = "",
                                           **kw) -> dict:
    req = _request(store, request_id)
    config = _config(store, request_id)
    trigger_rule = _rule(store, "NewSubstationOutgoingLineTriggerRule", trigger_rule_id)
    feasibility_rule = _rule(store, "NewSubstationOutgoingLineFeasibilityRule", feasibility_rule_id)
    capacity = _single_route_capacity(store, request_id, single_route_capacity_kva)
    access_checks = store.query("AccessPointCapacityCheck", filters={"request_id": request_id})
    usable_existing = [row for row in access_checks if row.get("is_directly_usable")]
    transfer_plans = store.query("LoadTransferPlan", filters={"request_id": request_id})
    feasible_transfer = [row for row in transfer_plans if row.get("is_feasible")]

    reasons = []
    if not _to_bool(config.get("enable_new_substation_outgoing_line")):
        reasons.append("配置未启用新出线评估")
    if capacity >= _to_float(trigger_rule.get("single_route_capacity_threshold_kva"), 10000):
        reasons.append("单路容量达到新出线阈值")
    if access_checks and not usable_existing:
        reasons.append("无直接可用现有电源点")
    if transfer_plans and not feasible_transfer:
        reasons.append("负荷划接不可行")
    if req.get("force_new_substation_outgoing_line"):
        reasons.append("请求强制新出线")

    is_triggered = _to_bool(config.get("enable_new_substation_outgoing_line")) and bool(reasons)
    center = _coordinate(req.get("receiving_point_coordinate"))
    search_km = _to_float(feasibility_rule.get("search_radius_km"), 15)
    candidates = []
    selected = {}
    if is_triggered:
        for substation in store.query("Substation"):
            distance_km = _distance_m(center, _coordinate(substation.get("coordinate"))) / 1000
            if distance_km <= search_km:
                row = dict(substation)
                row["distance_km"] = round(distance_km, 2)
                candidates.append(row)
        candidates.sort(key=lambda row: row.get("distance_km", 9999))
        for substation in candidates:
            bays = store.query("SubstationOutgoingBay", filters={"substation_id": substation.get("substation_id")})
            for bay in bays:
                transformer = _transformer(store, bay.get("transformer_id"))
                if (
                    _to_bool(bay.get("is_available"))
                    and _to_float(transformer.get("available_capacity_kva")) >= capacity
                    and _to_float(transformer.get("current_load_rate")) <= 0.8
                ):
                    selected = {
                        "selected_substation_id": substation.get("substation_id"),
                        "selected_transformer_id": transformer.get("transformer_id"),
                        "selected_outgoing_bay_id": bay.get("outgoing_bay_id"),
                    }
                    break
            if selected:
                break

    record = {
        "decision_id": _gen_id("NEWLINE_"),
        "request_id": request_id,
        "is_triggered": is_triggered,
        "trigger_reasons": reasons,
        "search_radius_km": search_km,
        "candidate_substation_ids": [row.get("substation_id") for row in candidates],
        "selected_substation_id": selected.get("selected_substation_id", ""),
        "selected_transformer_id": selected.get("selected_transformer_id", ""),
        "selected_outgoing_bay_id": selected.get("selected_outgoing_bay_id", ""),
        "is_feasible": bool(selected),
        "no_solution_reason": "" if selected or not is_triggered else "15公里范围内未找到满足间隔、容量和负载率要求的变电站出线",
    }
    store.insert_record("NewSubstationOutgoingLineDecision", record)
    return {"decision": record}


def _satisfies_independence(store: ObjectRepository, point_ids: list[str],
                            decision: dict) -> bool:
    if len(point_ids) <= 1:
        return True
    points = [_point(store, point_id) for point_id in point_ids]
    substations = {point.get("substation_id") for point in points}
    busbars = {point.get("busbar_id") for point in points}
    if decision.get("required_different_substations") and len(substations) < len(points):
        return False
    if decision.get("required_different_busbars") and len(busbars) < len(points):
        return False
    return True


def _generate_candidate_connection_schemes(store: ObjectRepository, request_id: str = "",
                                           source_check_ids: str = "",
                                           load_transfer_plan_ids: str = "",
                                           new_outgoing_line_decision_id: str = "",
                                           redline_rule_id: str = "", **kw) -> dict:
    req = _request(store, request_id)
    decision = _latest(store, "SupplyModeDecision", request_id) or {}
    required_count = int(decision.get("minimum_route_count") or 1)
    checks = store.query("AccessPointCapacityCheck", filters={"request_id": request_id})
    if source_check_ids:
        ids = set(_split_csv(source_check_ids))
        selected_checks = [row for row in checks if row.get("check_id") in ids]
        # The model may pass only the newest/successful check id. Keep the
        # function forgiving: a feasible transfer plan can make a previously
        # failed original source usable for combination, so preserve those
        # related checks too.
        transfer_related_points = set()
        for plan in store.query("LoadTransferPlan", filters={"request_id": request_id}):
            if not plan.get("is_feasible"):
                continue
            for check in checks:
                point = _point(store, check.get("access_point_id"))
                if (
                    plan.get("overloaded_feeder_id") == point.get("feeder_id")
                    or plan.get("overloaded_transformer_id") == point.get("incoming_source_id")
                ):
                    transfer_related_points.add(check.get("access_point_id"))
        checks = [
            row for row in checks
            if row in selected_checks or row.get("access_point_id") in transfer_related_points
        ]
    transfer_plans = store.query("LoadTransferPlan", filters={"request_id": request_id})
    if load_transfer_plan_ids:
        plan_ids = set(_split_csv(load_transfer_plan_ids))
        transfer_plans = [row for row in transfer_plans if row.get("transfer_plan_id") in plan_ids]
    feasible_transfers = [row for row in transfer_plans if row.get("is_feasible")]
    original_infos = store.query("ExpansionOriginalSupplyInfo", filters={"request_id": request_id})
    known_check_points = {row.get("access_point_id") for row in checks}
    for info in original_infos:
        original_point_id = info.get("original_access_point_id")
        if not original_point_id or original_point_id in known_check_points:
            continue
        point = _point(store, original_point_id)
        if not point:
            continue
        has_transfer = any(
            plan.get("overloaded_feeder_id") == point.get("feeder_id")
            or plan.get("overloaded_transformer_id") == point.get("incoming_source_id")
            for plan in feasible_transfers
        )
        if has_transfer:
            checks.append({
                "check_id": f"implicit_{original_point_id}",
                "request_id": request_id,
                "access_point_id": original_point_id,
                "single_route_capacity_kva": _single_route_capacity(store, request_id),
                "is_directly_usable": False,
                "requires_load_transfer": True,
                "failed_constraints": ["original_source_requires_transfer"],
            })
            known_check_points.add(original_point_id)
    usable = [row for row in checks if row.get("is_directly_usable")]
    for check in checks:
        if check in usable or not check.get("requires_load_transfer"):
            continue
        point = _point(store, check.get("access_point_id"))
        if any(
            plan.get("overloaded_feeder_id") == point.get("feeder_id")
            or plan.get("overloaded_transformer_id") == point.get("incoming_source_id")
            for plan in feasible_transfers
        ):
            usable.append(check)
    redline = _rule(store, "RedlineBoundaryDeviceRule", redline_rule_id)
    schemes = []

    for combo in combinations(usable, min(required_count, len(usable))):
        point_ids = [row.get("access_point_id") for row in combo]
        if len(point_ids) < required_count:
            continue
        if not _satisfies_independence(store, point_ids, decision):
            continue
        points = [_point(store, point_id) for point_id in point_ids]
        schemes.append({
            "scheme_id": _gen_id("SCHEME_"),
            "request_id": request_id,
            "scheme_source_type": "existing_access_point",
            "source_decision_id": decision.get("decision_id", ""),
            "supply_mode": decision.get("required_supply_mode", ""),
            "operation_mode": decision.get("required_supply_mode", ""),
            "selected_access_point_ids": point_ids,
            "selected_substation_ids": sorted({p.get("substation_id") for p in points}),
            "selected_feeder_ids": sorted({p.get("feeder_id") for p in points}),
            "selected_busbar_ids": sorted({p.get("busbar_id") for p in points}),
            "selected_transformer_ids": sorted({p.get("incoming_source_id") for p in points}),
            "selected_outgoing_bay_ids": [],
            "route_count": len(point_ids),
            "power_source_count": len(point_ids),
            "single_route_capacity_kva": _single_route_capacity(store, request_id),
            "total_supply_distance_m": sum(_to_float(p.get("distance_to_receiving_point_m")) for p in points),
            "new_public_access_point": "",
            "redline_boundary_device_type": redline.get("boundary_device_type", ""),
            "path_description": "、".join(f"接入{p.get('name') or p.get('access_point_id')}" for p in points),
            "load_transfer_plan_ids": [p.get("transfer_plan_id") for p in feasible_transfers],
            "constraint_check_summary": "满足供电方式、容量、负载率和独立性约束；必要电源点已匹配可行负荷划接方案",
        })

    newline = {}
    if new_outgoing_line_decision_id:
        newline = store.query_by_id("NewSubstationOutgoingLineDecision", new_outgoing_line_decision_id) or {}
    else:
        newline = _latest(store, "NewSubstationOutgoingLineDecision", request_id) or {}
    if newline.get("is_triggered") and newline.get("is_feasible"):
        schemes.append({
            "scheme_id": _gen_id("SCHEME_"),
            "request_id": request_id,
            "scheme_source_type": "new_substation_outgoing_line",
            "source_decision_id": newline.get("decision_id", ""),
            "supply_mode": decision.get("required_supply_mode", ""),
            "operation_mode": "新出线",
            "selected_access_point_ids": [],
            "selected_substation_ids": [newline.get("selected_substation_id")],
            "selected_feeder_ids": [],
            "selected_busbar_ids": [],
            "selected_transformer_ids": [newline.get("selected_transformer_id")],
            "selected_outgoing_bay_ids": [newline.get("selected_outgoing_bay_id")],
            "route_count": required_count,
            "power_source_count": required_count,
            "single_route_capacity_kva": _single_route_capacity(store, request_id),
            "total_supply_distance_m": 0,
            "new_public_access_point": "新建变电站出线",
            "redline_boundary_device_type": redline.get("boundary_device_type", "开关站"),
            "path_description": f"由{newline.get('selected_substation_id')}新出线至用户红线边界",
            "load_transfer_plan_ids": [],
            "constraint_check_summary": "新出线分支满足间隔、容量和负载率约束",
        })

    for scheme in schemes:
        store.insert_record("CandidateConnectionScheme", scheme)
    if not schemes:
        return {
            "schemes": [],
            "reason": f"{req.get('request_id')} 未形成满足约束的候选方案",
        }
    return {"schemes": schemes, "scheme_ids": [row["scheme_id"] for row in schemes]}


def _score_and_rank_schemes(store: ObjectRepository, request_id: str = "",
                            scheme_ids: str = "", rule_id: str = "", **kw) -> dict:
    rule = _rule(store, "SchemeScoringRule", rule_id)
    schemes = store.query("CandidateConnectionScheme", filters={"request_id": request_id})
    if scheme_ids:
        ids = set(_split_csv(scheme_ids))
        schemes = [row for row in schemes if row.get("scheme_id") in ids]
    if not schemes:
        record = {
            "result_id": _gen_id("RANK_"),
            "request_id": request_id,
            "scheme_ids": [],
            "max_scheme_count": int(rule.get("max_output_count") or 3),
            "sorted_by_score": True,
            "best_scheme_per_operation_mode": {},
            "generation_status": "失败",
            "no_solution_reason": "没有候选方案可评分",
        }
        store.insert_record("RankedConnectionSchemeSet", record)
        return {"ranked_set": record, "scores": []}

    scores = []
    for scheme in schemes:
        distance = _to_float(scheme.get("total_supply_distance_m"))
        distance_score = max(0, 100 - distance / 20)
        capacity_score = 80 if scheme.get("scheme_source_type") == "new_substation_outgoing_line" else 70
        source_count_scores = rule.get("source_count_reliability_scores") or {}
        operation_scores = rule.get("operation_mode_reliability_scores") or {}
        source_count_score = _to_float(source_count_scores.get(str(scheme.get("power_source_count")), 60))
        operation_score = _to_float(operation_scores.get(scheme.get("operation_mode"), 75))
        reliability_score = (source_count_score + operation_score) / 2
        total = (
            distance_score * _to_float(rule.get("distance_weight"), 0.35)
            + capacity_score * _to_float(rule.get("available_capacity_weight"), 0.35)
            + reliability_score * _to_float(rule.get("reliability_weight"), 0.30)
        )
        scores.append({
            "score_id": _gen_id("SCORE_"),
            "request_id": request_id,
            "scheme_id": scheme.get("scheme_id"),
            "distance_score": round(distance_score, 2),
            "available_capacity_score": round(capacity_score, 2),
            "reliability_score": round(reliability_score, 2),
            "source_count_reliability_score": round(source_count_score, 2),
            "operation_mode_reliability_score": round(operation_score, 2),
            "total_score": round(total, 2),
            "rank": 0,
            "retained_reason": "",
        })
    scores.sort(key=lambda row: row["total_score"], reverse=True)
    for idx, score in enumerate(scores, start=1):
        score["rank"] = idx
        score["retained_reason"] = "综合得分排序保留"
        store.insert_record("SchemeScore", score)

    max_count = int(rule.get("max_output_count") or 3)
    kept = scores[:max_count]
    record = {
        "result_id": _gen_id("RANK_"),
        "request_id": request_id,
        "scheme_ids": [row["scheme_id"] for row in kept],
        "max_scheme_count": max_count,
        "sorted_by_score": True,
        "best_scheme_per_operation_mode": {
            (store.query_by_id("CandidateConnectionScheme", row["scheme_id"]) or {}).get("operation_mode", ""): row["scheme_id"]
            for row in kept
        },
        "generation_status": "成功",
        "no_solution_reason": "",
    }
    store.insert_record("RankedConnectionSchemeSet", record)
    return {"ranked_set": record, "scores": kept}


def _generate_connection_scheme_pdf_preview(store: ObjectRepository, request_id: str = "",
                                            result_id: str = "",
                                            output_format: str = "pdf", **kw) -> dict:
    req = _request(store, request_id)
    ranked = store.query_by_id("RankedConnectionSchemeSet", result_id) if result_id else _latest(store, "RankedConnectionSchemeSet", request_id)
    if not ranked or ranked.get("generation_status") != "成功":
        return {"error": "没有成功的推荐方案集合，不能生成PDF预览"}
    schemes = [
        store.query_by_id("CandidateConnectionScheme", scheme_id)
        for scheme_id in ranked.get("scheme_ids", [])
    ]
    schemes = [row for row in schemes if row]
    record = {
        "pdf_id": _gen_id("PDF_"),
        "request_id": request_id,
        "result_id": ranked.get("result_id"),
        "user_demand_summary": f"{req.get('customer_name', request_id)} {req.get('business_type')} {req.get('approved_contract_capacity_kva')}kVA",
        "reasoning_process_summary": "完成范围校验、供电方式判定、电源点搜索、容量校核、候选方案生成与评分排序。",
        "power_access_point_selection": [s.get("selected_access_point_ids") for s in schemes],
        "load_transfer_description": [
            row for row in store.query("LoadTransferPlan", filters={"request_id": request_id})
            if row.get("is_feasible")
        ],
        "path_direction_description": [s.get("path_description") for s in schemes],
        "new_public_access_point_description": [
            s.get("new_public_access_point") for s in schemes if s.get("new_public_access_point")
        ],
        "recommended_scheme_summary": [
            {"scheme_id": s.get("scheme_id"), "mode": s.get("operation_mode"), "path": s.get("path_description")}
            for s in schemes
        ],
        "pdf_uri": f"memory://{request_id}/connection_scheme_preview.{output_format}",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    store.insert_record("ConnectionSchemePdfPreview", record)
    return {"pdf_preview": record}


def _generate_no_available_scheme_conclusion(store: ObjectRepository,
                                             request_id: str = "",
                                             failed_stage: str = "",
                                             searched_radius_m: int = 1000,
                                             searched_substation_radius_km: float = 15,
                                             **kw) -> dict:
    constraints = []
    for object_type in ("AccessPointCapacityCheck", "OriginalSupplyCapacityCheck"):
        for row in store.query(object_type, filters={"request_id": request_id}):
            constraints.extend(row.get("failed_constraints") or [])
    newline = _latest(store, "NewSubstationOutgoingLineDecision", request_id) or {}
    if newline.get("no_solution_reason"):
        constraints.append(newline.get("no_solution_reason"))
    if not failed_stage:
        failed_stage = "方案组合/评分排序"
    explanation = "；".join(str(item) for item in constraints if item) or "未形成满足全部约束的候选接入方案"
    record = {
        "conclusion_id": _gen_id("NOSOL_"),
        "request_id": request_id,
        "failed_stage": failed_stage,
        "failed_constraints": constraints,
        "searched_radius_m": searched_radius_m,
        "searched_substation_radius_km": searched_substation_radius_km,
        "explanation": explanation,
        "recommended_manual_review_actions": [
            "复核用户受电点坐标和容量资料",
            "人工校核周边线路可开放容量",
            "评估新建线路、主变扩容或新增间隔",
        ],
    }
    store.insert_record("NoAvailableConnectionSchemeConclusion", record)
    return {"conclusion": record}


def register(registry: FunctionRegistry, store: ObjectRepository, ontology: Ontology):
    registry.register_adapter("runtime_memory", RuntimeMemoryAdapter.factory(DOMAIN_DIR))

    fn_map = {
        "validate_connection_scope": lambda **kw: _validate_connection_scope(store, **kw),
        "apply_reasoning_config_constraints": lambda **kw: _apply_reasoning_config_constraints(store, **kw),
        "decide_supply_mode": lambda **kw: _decide_supply_mode(store, **kw),
        "search_power_access_points": lambda **kw: _search_power_access_points(store, **kw),
        "check_access_point_capacity": lambda **kw: _check_access_point_capacity(store, **kw),
        "check_original_supply_capacity": lambda **kw: _check_original_supply_capacity(store, **kw),
        "generate_load_transfer_plan": lambda **kw: _generate_load_transfer_plan(store, **kw),
        "evaluate_new_substation_outgoing_line": lambda **kw: _evaluate_new_substation_outgoing_line(store, **kw),
        "generate_candidate_connection_schemes": lambda **kw: _generate_candidate_connection_schemes(store, **kw),
        "score_and_rank_schemes": lambda **kw: _score_and_rank_schemes(store, **kw),
        "generate_connection_scheme_pdf_preview": lambda **kw: _generate_connection_scheme_pdf_preview(store, **kw),
        "generate_no_available_scheme_conclusion": lambda **kw: _generate_no_available_scheme_conclusion(store, **kw),
    }
    for name, fn in fn_map.items():
        func_def = ontology.functions.get(name)
        if func_def:
            registry.register(name, fn, func_def)
