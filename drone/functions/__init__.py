from __future__ import annotations

import json
import math
from typing import Any

from oag.registry import FunctionRegistry
from oag.schema import Ontology
from oag.store import Store

DATA_FILES = {
    "RoadSegment": "road_segment.json",
    "Bridge": "bridge.json",
    "Tunnel": "tunnel.json",
    "EmergencyDepot": "emergency_depot.json",
    "RescueTeam": "rescue_team.json",
    "Drone": "drone.json",
    "DroneOperator": "drone_operator.json",
    "DroneBase": "drone_base.json",
    "AirspaceZone": "airspace_zone.json",
    "DamageGradeStandard": "damage_grade_standard.json",
    "EventLevelStandard": "event_level_standard.json",
    "ClearanceTechniqueRule": "clearance_technique_rule.json",
    "TrafficControlRule": "traffic_control_rule.json",
    "ResponseLevelRule": "response_level_rule.json",
    "DroneClassRule": "drone_class_rule.json",
    "OperatorLicenseRule": "operator_license_rule.json",
    "AirspaceRule": "airspace_rule.json",
    "FlightApprovalRule": "flight_approval_rule.json",
    "PayloadTypeRule": "payload_type_rule.json",
    "WeatherWarning": "weather_warning.json",
    "DisasterEvent": "disaster_event.json",
    "AccidentEvent": "accident_event.json",
    "FacilityInspection": "facility_inspection.json",
}


def _haversine(lng1, lat1, lng2, lat2):
    lng1, lat1, lng2, lat2 = float(lng1), float(lat1), float(lng2), float(lat2)
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _get_event(store: Store, event_id: str = "", **kw) -> dict:
    for etype in ("DisasterEvent", "AccidentEvent"):
        row = store.query_by_id(etype, event_id)
        if row:
            row["event_type"] = etype
            return row
    return {"error": f"未找到事件 {event_id}"}


def _get_affected_facilities(store: Store, event_id: str = "", radius_km: float = 5, **kw) -> dict:
    event = _get_event(store, event_id)
    if "error" in event:
        return event
    lng, lat = event.get("lng", 0), event.get("lat", 0)
    if not lng or not lat:
        return {"error": "事件缺少坐标信息"}

    results = {"event_id": event_id, "radius_km": radius_km, "facilities": []}
    for ftype, id_field in [("RoadSegment", "segment_id"), ("Bridge", "bridge_id"), ("Tunnel", "tunnel_id")]:
        for row in store.query(ftype):
            dist = _haversine(lng, lat, row.get("lng", 0), row.get("lat", 0))
            if dist <= radius_km:
                results["facilities"].append({
                    "facility_type": ftype, "facility_id": row[id_field],
                    "name": row.get("name", row.get("road_name", "")),
                    "distance_km": round(dist, 2),
                })
    return results


def _get_depots_in_range(store: Store, lng: float = 0, lat: float = 0, radius_km: float = 100, **kw) -> list:
    results = []
    for row in store.query("EmergencyDepot"):
        dist = _haversine(lng, lat, row.get("lng", 0), row.get("lat", 0))
        if dist <= radius_km:
            row["distance_km"] = round(dist, 2)
            results.append(row)
    return sorted(results, key=lambda x: x["distance_km"])


def _get_rescue_teams_in_range(store: Store, lng: float = 0, lat: float = 0, radius_km: float = 100, **kw) -> list:
    results = []
    for row in store.query("RescueTeam"):
        if not row.get("available"):
            continue
        dist = _haversine(lng, lat, row.get("lng", 0), row.get("lat", 0))
        if dist <= radius_km:
            row["distance_km"] = round(dist, 2)
            results.append(row)
    return sorted(results, key=lambda x: x["distance_km"])


def _get_drones_in_range(store: Store, lng: float = 0, lat: float = 0, radius_km: float = 50, **kw) -> list:
    results = []
    bases = {b["base_id"]: b for b in store.query("DroneBase")}
    for drone in store.query("Drone"):
        if drone.get("status") != "可用":
            continue
        base = bases.get(drone.get("base_id", ""))
        if not base:
            continue
        dist = _haversine(lng, lat, base.get("lng", 0), base.get("lat", 0))
        if dist <= radius_km:
            drone["distance_km"] = round(dist, 2)
            drone["base_name"] = base.get("name", "")
            results.append(drone)
    return sorted(results, key=lambda x: x["distance_km"])


def _simple_getter(store: Store, object_type: str, id_field: str, **kw) -> dict:
    id_val = kw.get(id_field, "")
    row = store.query_by_id(object_type, id_val)
    return row if row else {"error": f"未找到 {object_type} {id_val}"}


def register(registry: FunctionRegistry, store: Store, ontology: Ontology):
    fn_map = {
        "get_event": lambda **kw: _get_event(store, **kw),
        "get_affected_facilities": lambda **kw: _get_affected_facilities(store, **kw),
        "get_road_segment": lambda **kw: _simple_getter(store, "RoadSegment", "segment_id", **kw),
        "get_bridge_status": lambda **kw: _simple_getter(store, "Bridge", "bridge_id", **kw),
        "get_tunnel_status": lambda **kw: _simple_getter(store, "Tunnel", "tunnel_id", **kw),
        "get_depots_in_range": lambda **kw: _get_depots_in_range(store, **kw),
        "get_rescue_teams_in_range": lambda **kw: _get_rescue_teams_in_range(store, **kw),
        "get_drone": lambda **kw: _simple_getter(store, "Drone", "drone_id", **kw),
        "get_drone_operator": lambda **kw: _simple_getter(store, "DroneOperator", "operator_id", **kw),
        "get_drone_base": lambda **kw: _simple_getter(store, "DroneBase", "base_id", **kw),
        "get_airspace_zone": lambda **kw: _simple_getter(store, "AirspaceZone", "zone_id", **kw),
        "get_drones_in_range": lambda **kw: _get_drones_in_range(store, **kw),
        "get_operators_available": lambda **kw: store.query("DroneOperator", filters={"available": 1}),
        "get_weather_warning": lambda **kw: store.query("WeatherWarning", filters={"warning_id": kw["warning_id"]} if kw.get("warning_id") else None),
    }

    for name in list(fn_map.keys()):
        func_def = ontology.functions.get(name)
        if func_def:
            registry.register(name, fn_map[name], func_def)

    lookup_map = {
        "lookup_damage_grade": ("DamageGradeStandard", ["facility_type", "damage_grade"]),
        "lookup_event_level": ("EventLevelStandard", ["level"]),
        "lookup_clearance_technique": ("ClearanceTechniqueRule", ["facility_type", "damage_type"]),
        "lookup_traffic_control": ("TrafficControlRule", ["damage_grade", "facility_type"]),
        "lookup_response_level": ("ResponseLevelRule", ["event_level"]),
        "lookup_drone_class": ("DroneClassRule", ["category"]),
        "lookup_operator_license_rule": ("OperatorLicenseRule", ["drone_category", "operation_type"]),
        "lookup_airspace_rule": ("AirspaceRule", ["airspace_type"]),
        "lookup_flight_approval_rule": ("FlightApprovalRule", ["scenario"]),
        "lookup_payload_type": ("PayloadTypeRule", ["payload_type", "scenario"]),
    }

    for fn_name, (obj_type, filter_fields) in lookup_map.items():
        func_def = ontology.functions.get(fn_name)
        if not func_def:
            continue

        def _make_lookup(ot, fields):
            def _lookup(**kw):
                filters = {}
                for f in fields:
                    v = kw.get(f, "")
                    if v:
                        filters[f] = v
                return store.query(ot, filters=filters if filters else None)
            return _lookup

        registry.register(fn_name, _make_lookup(obj_type, filter_fields), func_def)

    stub_fns = [
        "inspect_facility", "assess_event_level",
        "generate_clearance_plans", "score_plans",
        "dispatch_resources", "set_traffic_control",
        "evaluate_traffic", "generate_detour",
        "plan_recon_mission", "check_compliance",
        "request_flight_approval", "dispatch_drone",
        "collect_recon_data", "schedule_patrol",
        "log_maintenance", "trigger_defense_response",
        "intensify_patrol", "generate_event_report",
        "get_equipment_by_depot", "get_material_by_depot",
    ]
    for fn_name in stub_fns:
        func_def = ontology.functions.get(fn_name)
        if not func_def:
            continue

        def _make_stub(name):
            def _stub(**kw):
                return {"status": "mock", "function": name, "args": kw, "message": f"{name} 已模拟执行（mock 模式）"}
            return _stub

        registry.register(fn_name, _make_stub(fn_name), func_def)
