from __future__ import annotations

import json
import math
import uuid
from typing import Any

from oag.registry import FunctionRegistry
from oag.schema import Ontology
from oag.store import Store

DATA_FILES = {
    "Substation": "substation.json",
    "MainTransformer": "main_transformer.json",
    "Busbar": "busbar.json",
    "Feeder": "feeder.json",
    "AccessPoint": "access_point.json",
    "FeederTieSwitch": "feeder_tie_switch.json",
    "TransformerTieSwitch": "transformer_tie_switch.json",
    "ImportanceLevelMap": "importance_level_map.json",
    "SourceRequirement": "source_requirement.json",
    "AccessRequest": "access_request.json",
    "ExpandRequest": "expand_request.json",
}


def _haversine(lng1, lat1, lng2, lat2):
    lng1, lat1, lng2, lat2 = float(lng1), float(lat1), float(lng2), float(lat2)
    R = 6371000
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _gen_id(prefix=""):
    return prefix + uuid.uuid4().hex[:8].upper()


# ============================================================
# Interface wrappers (get_xxx)
# ============================================================

def _simple_getter(store: Store, object_type: str, id_field: str, **kw) -> dict:
    id_val = kw.get(id_field, "")
    row = store.query_by_id(object_type, id_val)
    return row if row else {"error": f"未找到 {object_type} {id_val}"}


def _get_request(store: Store, request_id: str = "", **kw) -> dict:
    for rtype in ("AccessRequest", "ExpandRequest"):
        row = store.query_by_id(rtype, request_id)
        if row:
            row["request_type"] = rtype
            return row
    return {"error": f"未找到申请 {request_id}"}


def _get_access_points(store: Store, lng: float = 0, lat: float = 0, radius_m: float = 1000, **kw) -> list:
    lng, lat, radius_m = float(lng), float(lat), float(radius_m)
    results = []
    for row in store.query("AccessPoint"):
        dist = _haversine(lng, lat, float(row.get("lng", 0)), float(row.get("lat", 0)))
        if dist <= radius_m:
            row["distance_m"] = round(dist, 1)
            results.append(row)
    return sorted(results, key=lambda x: x["distance_m"])


def _get_feeder_tie_switches(store: Store, source_feeder_id: str = "", **kw) -> list:
    results = []
    for row in store.query("FeederTieSwitch"):
        if row.get("source_feeder_id") == source_feeder_id or row.get("target_feeder_id") == source_feeder_id:
            results.append(row)
    return results


def _get_transformer_tie_switches(store: Store, source_transformer_id: str = "", **kw) -> list:
    results = []
    for row in store.query("TransformerTieSwitch"):
        if row.get("source_transformer_id") == source_transformer_id or row.get("target_transformer_id") == source_transformer_id:
            results.append(row)
    return results


# ============================================================
# search_sources
# ============================================================

def _search_sources(store: Store, request_id: str = "", point_types: str = "", **kw) -> dict:
    req = _get_request(store, request_id)
    if "error" in req:
        return req
    if req.get("request_type") == "ExpandRequest":
        return {"error": "增容申请不适用 search_sources，请直接对原电源点 filter"}

    lng, lat = req.get("lng", 0), req.get("lat", 0)
    type_filter = [t.strip() for t in point_types.split(",") if t.strip()] if point_types else []
    if not type_filter:
        pref = req.get("preferred_point_types", "")
        if pref:
            type_filter = [t.strip() for t in pref.split(",") if t.strip()]

    all_points = store.query("AccessPoint")
    radii = [200, 500, 800, 1000, 1500, 2000]
    found = []
    found_feeders = set()

    for radius in radii:
        for pt in all_points:
            if pt["point_id"] in {p["point_id"] for p in found}:
                continue
            if type_filter and pt.get("point_type", "") not in type_filter:
                continue
            dist = _haversine(lng, lat, pt.get("lng", 0), pt.get("lat", 0))
            if dist <= radius:
                pt["distance_m"] = round(dist, 1)
                found.append(pt)
                found_feeders.add(pt.get("feeder_id", ""))
        if len(found_feeders) >= 5:
            break

    found.sort(key=lambda x: x.get("distance_m", 9999))
    max_searched = radii[-1]
    for r in radii:
        if len(found_feeders) >= 5:
            max_searched = r
            break
        max_searched = r

    return {
        "request_id": request_id,
        "search_complete": True,
        "search_radius_m": max_searched,
        "found_points": len(found),
        "distinct_feeders": len(found_feeders),
        "target_feeders": 5,
        "point_ids": ",".join(p["point_id"] for p in found),
        "points": found,
    }


# ============================================================
# filter_sources (F1-F7)
# ============================================================

def _filter_sources(store: Store, request_id: str = "", point_ids: str = "", per_path_capacity_kva: float = 0, **kw) -> dict:
    per_path_capacity_kva = float(per_path_capacity_kva)
    if not point_ids:
        return {"error": "缺少 point_ids 参数。新装场景传 search_sources 返回的接入点ID，增容场景传 ExpandRequest.original_point_id"}
    if per_path_capacity_kva <= 0:
        return {"error": "缺少 per_path_capacity_kva 参数。请传入单路接入容量(kVA)，增容场景传增容后总容量"}
    ids = [pid.strip() for pid in point_ids.split(",") if pid.strip()]
    results = []

    for pid in ids:
        pt = store.query_by_id("AccessPoint", pid)
        if not pt:
            results.append({"point_id": pid, "passed": False, "reasons": ["接入点不存在"]})
            continue

        feeder = store.query_by_id("Feeder", pt.get("feeder_id", ""))
        if not feeder:
            results.append({"point_id": pid, "passed": False, "reasons": ["馈线信息缺失"]})
            continue

        transformer = store.query_by_id("MainTransformer", feeder.get("transformer_id", ""))
        reasons = []

        f1 = feeder.get("max_load_rate", 0) < 0.8
        if not f1:
            reasons.append(f"F1: 馈线负载率{feeder.get('max_load_rate', 0):.0%}≥80%")

        f2 = feeder.get("openable_capacity_kva", 0) > per_path_capacity_kva
        if not f2:
            reasons.append(f"F2: 馈线可开放容量{feeder.get('openable_capacity_kva', 0)}kVA≤{per_path_capacity_kva}kVA")

        f3 = True
        if pt.get("point_type") in ("环网柜", "开关站"):
            f3 = pt.get("spare_intervals", 0) >= 2
            if not f3:
                reasons.append(f"F3: 备用间隔{pt.get('spare_intervals', 0)}<2")

        f4 = feeder.get("connected_users", 0) <= 50
        if not f4:
            reasons.append(f"F4: 已接入用户{feeder.get('connected_users', 0)}>50")

        f5 = feeder.get("loss_rate", 0) <= 0.07
        if not f5:
            reasons.append(f"F5: 线损率{feeder.get('loss_rate', 0):.1%}>7%")

        f6 = True
        f7 = True
        if transformer:
            f6 = transformer.get("openable_capacity_kva", 0) > per_path_capacity_kva
            if not f6:
                reasons.append(f"F6: 主变可开放容量{transformer.get('openable_capacity_kva', 0)}kVA≤{per_path_capacity_kva}kVA")
            f7 = transformer.get("load_rate", 0) < 0.8
            if not f7:
                reasons.append(f"F7: 主变负载率{transformer.get('load_rate', 0):.0%}≥80%")

        passed = f1 and f2 and f3 and f4 and f5 and f6 and f7
        remedy = ""
        if not passed:
            if not f1 or not f2 or not f4 or not f5:
                remedy = "馈线问题→考虑 transfer_feeder_load"
            elif not f6 or not f7:
                remedy = "主变问题→考虑 transfer_transformer_load"

        results.append({
            "point_id": pid,
            "point_name": pt.get("name", ""),
            "feeder_id": pt.get("feeder_id", ""),
            "passed": passed,
            "reasons": reasons if reasons else ["全部通过"],
            "remedy": remedy,
        })

    passed_points = [r for r in results if r["passed"]]
    failed_points = [r for r in results if not r["passed"]]

    return {
        "request_id": request_id,
        "per_path_capacity_kva": per_path_capacity_kva,
        "total": len(results),
        "passed": len(passed_points),
        "failed": len(failed_points),
        "results": results,
    }


# ============================================================
# compose_plans
# ============================================================

def _compose_plans(store: Store, request_id: str = "", source_structure: str = "", **kw) -> dict:
    passed = store.query("AccessPoint")
    if not passed:
        return {"error": "无可用电源点，无法组合方案"}

    passed.sort(key=lambda x: x.get("distance_m", 9999))
    plans = []

    if source_structure == "单电源":
        seen_feeders = set()
        for pt in passed:
            fid = pt.get("feeder_id", "")
            if fid in seen_feeders:
                continue
            seen_feeders.add(fid)
            plan_id = _gen_id("P")
            plans.append({
                "plan_id": plan_id,
                "request_id": request_id,
                "source_structure": "单电源",
                "operation_mode": "单电源供电",
                "point_ids": pt["point_id"],
                "total_distance_m": pt.get("distance_m", 0),
                "status": "candidate",
            })
            if len(plans) >= 5:
                break

    elif source_structure in ("双电源", "双回路"):
        for i, pt1 in enumerate(passed):
            for pt2 in passed[i + 1:]:
                f1 = store.query_by_id("Feeder", pt1.get("feeder_id", ""))
                f2 = store.query_by_id("Feeder", pt2.get("feeder_id", ""))
                if not f1 or not f2 or pt1.get("feeder_id") == pt2.get("feeder_id"):
                    continue

                if source_structure == "双电源":
                    ok = (f1.get("substation_id") != f2.get("substation_id")) or \
                         (f1.get("busbar_id") != f2.get("busbar_id"))
                else:
                    ok = f1.get("busbar_id") != f2.get("busbar_id")

                if ok:
                    for mode in ["同时供电互为备用", "同时供电", "一主一备"]:
                        plan_id = _gen_id("P")
                        plans.append({
                            "plan_id": plan_id,
                            "request_id": request_id,
                            "source_structure": source_structure,
                            "operation_mode": f"两路电源{mode}({source_structure})",
                            "point_ids": f"{pt1['point_id']},{pt2['point_id']}",
                            "total_distance_m": round(pt1.get("distance_m", 0) + pt2.get("distance_m", 0), 1),
                            "status": "candidate",
                        })
            if len(plans) >= 15:
                break

    for plan in plans:
        store.insert_record("AccessPlan", plan)

    return {
        "request_id": request_id,
        "source_structure": source_structure,
        "plans_generated": len(plans),
        "plans": plans,
    }


# ============================================================
# score_plans
# ============================================================

def _score_plans(store: Store, request_id: str = "", **kw) -> dict:
    plans = store.query("AccessPlan", filters={"request_id": request_id, "status": "candidate"})
    if not plans:
        return {"error": f"无候选方案 (request_id={request_id})"}

    all_distances = [p.get("total_distance_m", 0) for p in plans]
    max_dist = max(all_distances) if all_distances else 1
    min_dist = min(all_distances) if all_distances else 0

    source_score_map = {"双电源": 100, "双回路": 90, "单电源": 80}
    mode_score_map = {"同时供电互为备用": 100, "同时供电": 90, "一主一备": 80, "一路主供": 70, "单电源供电": 70}

    scored = []
    for plan in plans:
        dist = plan.get("total_distance_m", 0)
        if max_dist > min_dist:
            distance_score = round(100 * (1 - (dist - min_dist) / (max_dist - min_dist)), 1)
        else:
            distance_score = 100.0

        point_ids = plan.get("point_ids", "").split(",")
        cap_scores = []
        for pid in point_ids:
            pt = store.query_by_id("AccessPoint", pid.strip())
            if pt:
                feeder = store.query_by_id("Feeder", pt.get("feeder_id", ""))
                if feeder:
                    cap_scores.append(min(100, feeder.get("openable_capacity_kva", 0) / 100))
        capacity_score = round(sum(cap_scores) / len(cap_scores), 1) if cap_scores else 50.0

        ss = plan.get("source_structure", "单电源")
        source_dim = source_score_map.get(ss, 80)
        mode = plan.get("operation_mode", "")
        mode_dim = 70
        for k, v in mode_score_map.items():
            if k in mode:
                mode_dim = v
                break
        reliability_score = round((source_dim * 0.5 + mode_dim * 0.5), 1)

        total_score = round(distance_score * 0.4 + capacity_score * 0.3 + reliability_score * 0.3, 1)

        store.update_record("AccessPlan", plan["plan_id"], {
            "distance_score": distance_score,
            "capacity_score": capacity_score,
            "reliability_score": reliability_score,
            "total_score": total_score,
            "status": "scored",
        })

        scored.append({
            "plan_id": plan["plan_id"],
            "operation_mode": plan.get("operation_mode"),
            "distance_score": distance_score,
            "capacity_score": capacity_score,
            "reliability_score": reliability_score,
            "total_score": total_score,
        })

    scored.sort(key=lambda x: x["total_score"], reverse=True)
    return {"request_id": request_id, "scored_plans": len(scored), "plans": scored}


# ============================================================
# finalize_plans
# ============================================================

def _finalize_plans(store: Store, request_id: str = "", **kw) -> dict:
    plans = store.query("AccessPlan", filters={"request_id": request_id, "status": "scored"})
    if not plans:
        return {"error": f"无已评分方案 (request_id={request_id})"}

    plans.sort(key=lambda x: x.get("total_score", 0), reverse=True)

    best_by_mode = {}
    for p in plans:
        mode = p.get("operation_mode", "")
        if mode not in best_by_mode:
            best_by_mode[mode] = p

    final = list(best_by_mode.values())
    final.sort(key=lambda x: x.get("total_score", 0), reverse=True)
    final = final[:10]

    for p in final:
        store.update_record("AccessPlan", p["plan_id"], {"status": "final"})

    for p in plans:
        if p["plan_id"] not in {f["plan_id"] for f in final}:
            store.update_record("AccessPlan", p["plan_id"], {"status": "dropped"})

    return {
        "request_id": request_id,
        "final_plans": len(final),
        "plans": [{
            "plan_id": p["plan_id"],
            "operation_mode": p.get("operation_mode"),
            "point_ids": p.get("point_ids"),
            "total_score": p.get("total_score"),
        } for p in final],
    }


# ============================================================
# transfer_feeder_load
# ============================================================

def _transfer_feeder_load(store: Store, request_id: str = "", source_feeder_id: str = "",
                          required_capacity_kva: float = 0, **kw) -> dict:
    source = store.query_by_id("Feeder", source_feeder_id)
    if not source:
        return {"error": f"馈线 {source_feeder_id} 不存在"}

    switches = _get_feeder_tie_switches(store, source_feeder_id)
    if not switches:
        return {"error": f"馈线 {source_feeder_id} 无联络开关，无法划接", "remedy": "考虑 new_feeder"}

    candidates = []
    for sw in switches:
        target_id = sw["target_feeder_id"] if sw["source_feeder_id"] == source_feeder_id else sw["source_feeder_id"]
        target = store.query_by_id("Feeder", target_id)
        if not target:
            continue
        target_available = target.get("openable_capacity_kva", 0)
        target_rate_after = (target.get("max_load_rate", 0) * target.get("openable_capacity_kva", 1) + required_capacity_kva) / max(target.get("openable_capacity_kva", 1) + required_capacity_kva, 1)
        if target_available >= required_capacity_kva and target_rate_after <= 0.8:
            candidates.append({
                "target_feeder_id": target_id,
                "target_feeder_name": target.get("name", ""),
                "switch_id": sw["switch_id"],
                "target_available_kva": target_available,
                "target_load_rate_after": round(target_rate_after, 3),
            })

    if not candidates:
        return {"error": "1000m内无满足划接条件的线路(划接后≤80%)", "remedy": "考虑 new_feeder"}

    best = candidates[0]
    transfer_id = _gen_id("FLT")
    record = {
        "transfer_id": transfer_id,
        "request_id": request_id,
        "source_feeder_id": source_feeder_id,
        "target_feeder_id": best["target_feeder_id"],
        "switch_id": best["switch_id"],
        "transfer_capacity_kva": required_capacity_kva,
        "source_load_rate_after": round(source.get("max_load_rate", 0) - required_capacity_kva / max(source.get("openable_capacity_kva", 1) + required_capacity_kva, 1), 3),
        "target_load_rate_after": best["target_load_rate_after"],
    }
    store.insert_record("FeederLoadTransfer", record)
    return {"transfer": record, "message": f"建议将{required_capacity_kva}kVA从{source_feeder_id}转移至{best['target_feeder_id']}(通过开关{best['switch_id']})"}


# ============================================================
# transfer_transformer_load
# ============================================================

def _transfer_transformer_load(store: Store, request_id: str = "", source_transformer_id: str = "",
                               required_capacity_kva: float = 0, **kw) -> dict:
    source = store.query_by_id("MainTransformer", source_transformer_id)
    if not source:
        return {"error": f"主变 {source_transformer_id} 不存在"}

    switches = _get_transformer_tie_switches(store, source_transformer_id)
    if not switches:
        return {"error": f"主变 {source_transformer_id} 无联络开关，无法划接", "remedy": "考虑 new_feeder"}

    for sw in switches:
        target_id = sw["target_transformer_id"] if sw["source_transformer_id"] == source_transformer_id else sw["source_transformer_id"]
        target = store.query_by_id("MainTransformer", target_id)
        if not target:
            continue
        target_available = target.get("openable_capacity_kva", 0)
        target_rate_after = target.get("load_rate", 0) + required_capacity_kva / max(target.get("rated_capacity_kva", 1), 1)
        if target_available >= required_capacity_kva and target_rate_after <= 0.8:
            transfer_id = _gen_id("TLT")
            record = {
                "transfer_id": transfer_id,
                "request_id": request_id,
                "source_transformer_id": source_transformer_id,
                "target_transformer_id": target_id,
                "switch_id": sw["switch_id"],
                "transfer_capacity_kva": required_capacity_kva,
                "source_load_rate_after": round(source.get("load_rate", 0) - required_capacity_kva / max(source.get("rated_capacity_kva", 1), 1), 3),
                "target_load_rate_after": round(target_rate_after, 3),
            }
            store.insert_record("TransformerLoadTransfer", record)
            return {"transfer": record, "message": f"建议将{required_capacity_kva}kVA从{source_transformer_id}转移至{target_id}"}

    return {"error": "变电站内无满足划接条件的主变(划接后≤80%)", "remedy": "考虑 new_feeder"}


# ============================================================
# new_feeder
# ============================================================

def _new_feeder(store: Store, request_id: str = "", search_radius_m: float = 15000, **kw) -> dict:
    req = _get_request(store, request_id)
    if "error" in req:
        return req
    search_radius_m = float(search_radius_m)
    lng, lat = req.get("lng", 0), req.get("lat", 0)
    capacity = req.get("capacity_kva", 0)

    substations = store.query("Substation")
    candidates = []

    for ss in substations:
        dist = _haversine(lng, lat, ss.get("lng", 0), ss.get("lat", 0))
        if dist > search_radius_m:
            continue
        transformers = store.query("MainTransformer", filters={"substation_id": ss["substation_id"]})
        for mt in transformers:
            if mt.get("spare_intervals", 0) > 0 and mt.get("load_rate", 0) < 0.8 and mt.get("openable_capacity_kva", 0) > capacity:
                candidates.append({
                    "substation_id": ss["substation_id"],
                    "substation_name": ss.get("name", ""),
                    "transformer_id": mt["transformer_id"],
                    "transformer_name": mt.get("name", ""),
                    "distance_m": round(dist, 1),
                    "load_rate": mt.get("load_rate", 0),
                    "openable_capacity_kva": mt.get("openable_capacity_kva", 0),
                    "spare_intervals": mt.get("spare_intervals", 0),
                })

    candidates.sort(key=lambda x: x["distance_m"])

    if not candidates:
        verdict_id = _gen_id("NS")
        verdict = {
            "verdict_id": verdict_id,
            "request_id": request_id,
            "reason": f"15km内无同时满足空余间隔、主变负载率<80%及可开放容量>{capacity}kVA的变电站",
            "searched_radius_m": search_radius_m,
        }
        store.insert_record("NoSolutionVerdict", verdict)
        return {"verdict": verdict, "message": "无可用接入方案"}

    best = candidates[0]
    suggestion_id = _gen_id("NF")
    suggestion = {
        "suggestion_id": suggestion_id,
        "request_id": request_id,
        "substation_id": best["substation_id"],
        "transformer_id": best["transformer_id"],
        "distance_m": best["distance_m"],
        "load_rate": best["load_rate"],
        "openable_capacity_kva": best["openable_capacity_kva"],
        "spare_intervals": best["spare_intervals"],
    }
    store.insert_record("NewFeederSuggestion", suggestion)
    return {
        "suggestion": suggestion,
        "all_candidates": len(candidates),
        "message": f"建议从{best['substation_name']}{best['transformer_name']}新出线(距离{best['distance_m']}m)",
    }


# ============================================================
# register
# ============================================================

def register(registry: FunctionRegistry, store: Store, ontology: Ontology):
    fn_map = {
        "get_substation": lambda **kw: _simple_getter(store, "Substation", "substation_id", **kw),
        "get_main_transformer": lambda **kw: _simple_getter(store, "MainTransformer", "transformer_id", **kw),
        "get_busbar": lambda **kw: _simple_getter(store, "Busbar", "busbar_id", **kw),
        "get_feeder": lambda **kw: _simple_getter(store, "Feeder", "feeder_id", **kw),
        "get_access_points": lambda **kw: _get_access_points(store, **kw),
        "get_feeder_tie_switches": lambda **kw: _get_feeder_tie_switches(store, **kw),
        "get_transformer_tie_switches": lambda **kw: _get_transformer_tie_switches(store, **kw),
        "get_request": lambda **kw: _get_request(store, **kw),
        "search_sources": lambda **kw: _search_sources(store, **kw),
        "filter_sources": lambda **kw: _filter_sources(store, **kw),
        "compose_plans": lambda **kw: _compose_plans(store, **kw),
        "score_plans": lambda **kw: _score_plans(store, **kw),
        "finalize_plans": lambda **kw: _finalize_plans(store, **kw),
        "transfer_feeder_load": lambda **kw: _transfer_feeder_load(store, **kw),
        "transfer_transformer_load": lambda **kw: _transfer_transformer_load(store, **kw),
        "new_feeder": lambda **kw: _new_feeder(store, **kw),
    }

    for name, fn in fn_map.items():
        func_def = ontology.functions.get(name)
        if func_def:
            registry.register(name, fn, func_def)

    lookup_map = {
        "lookup_importance_level": ("ImportanceLevelMap", ["industry_code"]),
        "lookup_source_requirement": ("SourceRequirement", ["load_level", "importance_level"]),
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
