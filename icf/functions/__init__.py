from __future__ import annotations

import json
import uuid
from typing import Any

from oag.registry import FunctionRegistry
from oag.schema import Ontology
from oag.store import Store

DATA_FILES = {
    "SubsystemConfig": "subsystem_config.json",
    "NodeDefinition": "node_definition.json",
    "FlowEdge": "flow_edge.json",
    "LaunchMission": "launch_mission.json",
    "ShieldDoor": "shield_door.json",
    "PersonnelCounter": "personnel_counter.json",
    "VacuumStatus": "vacuum_status.json",
    "BeamLine": "beam_line.json",
    "InterlockCondition": "interlock_condition.json",
    "TimeoutRule": "timeout_rule.json",
}


def _gen_id(prefix=""):
    return prefix + uuid.uuid4().hex[:8].upper()


def _simple_getter(store: Store, object_type: str, id_field: str, **kw) -> dict:
    id_val = kw.get(id_field, "")
    row = store.query_by_id(object_type, id_val)
    return row if row else {"error": f"未找到 {object_type} {id_val}"}


def _get_mission(store: Store, mission_id: str = "", **kw) -> dict:
    row = store.query_by_id("LaunchMission", mission_id)
    return row if row else {"error": f"未找到发射任务 {mission_id}"}


def _get_subsystem(store: Store, subsystem_id: str = "", **kw) -> dict:
    row = store.query_by_id("SubsystemConfig", subsystem_id)
    if row:
        return row
    for r in store.query("SubsystemConfig"):
        if r.get("subsystem_type") == subsystem_id or r.get("name") == subsystem_id:
            return r
    return {"error": f"未找到分系统 {subsystem_id}"}


def _get_node_definition(store: Store, node_name: str = "", **kw) -> dict:
    for r in store.query("NodeDefinition"):
        if r.get("node_name") == node_name:
            return r
    return {"error": f"未找到节点定义 {node_name}"}


def _get_flow_edges(store: Store, phase: str = "", from_node: str = "", **kw) -> list:
    filters = {}
    if phase:
        filters["phase"] = phase
    if from_node:
        filters["from_node"] = from_node
    return store.query("FlowEdge", filters=filters if filters else None)


def _get_node_status(store: Store, mission_id: str = "", node_name: str = "", **kw) -> dict:
    nodes = store.query("FlowNode", filters={"mission_id": mission_id})
    for n in reversed(nodes):
        if n.get("node_name") == node_name:
            return n
    return {"status": "not_started", "node_name": node_name, "mission_id": mission_id}


def _execute_node(store: Store, mission_id: str = "", node_name: str = "", beam_ids: str = "", **kw) -> dict:
    mission = store.query_by_id("LaunchMission", mission_id)
    if not mission:
        return {"error": f"任务 {mission_id} 不存在"}
    if mission.get("safety_interlock") == "触发":
        return {"error": "安全联锁已触发，无法执行节点"}

    node_def = _get_node_definition(store, node_name)
    if "error" in node_def:
        return node_def

    edges = store.query("FlowEdge", filters={"to_node": node_name})
    unmet = []
    for edge in edges:
        prev = edge.get("from_node", "")
        if prev == "开始":
            continue
        prev_status = _get_node_status(store, mission_id, prev)
        if prev_status.get("status") != "completed":
            unmet.append(prev)

    node_id = _gen_id("N")
    record = {
        "node_id": node_id,
        "mission_id": mission_id,
        "node_name": node_name,
        "phase": node_def.get("phase", ""),
        "subsystem_id": node_def.get("subsystem_type", ""),
        "command": node_def.get("command", ""),
        "call_mode": node_def.get("call_mode", ""),
        "input_params": json.dumps({"shot_number": mission.get("shot_number"), "beam_ids": beam_ids}, ensure_ascii=False),
        "output_status": 1,
        "expected_duration_s": 0,
        "actual_duration_s": 0,
        "preconditions_met": "通过" if not unmet else f"未满足: {','.join(unmet)}",
        "status": "completed" if not unmet else "failed",
        "error_message": "" if not unmet else f"前置节点未完成: {','.join(unmet)}",
    }
    store.insert_record("FlowNode", record)

    return {
        "node_id": node_id,
        "node_name": node_name,
        "status": record["status"],
        "preconditions_met": record["preconditions_met"],
        "command": record["command"],
        "subsystem": record["subsystem_id"],
        "expected_duration": node_def.get("expected_duration", ""),
    }


def _check_readiness(store: Store, mission_id: str = "", **kw) -> dict:
    checks = {
        "诊断设备发射准备": "diagnostics_ready",
        "测量打靶运动准备": "measurement_ready",
        "实验靶复位": "target_ready",
        "光路准直": "alignment_ready",
        "光纤种子源/二倍频出光": "light_source_ready",
        "能量精闭环": "preamplifier_ready",
        "泵浦发射准备": "pump_ready",
    }

    result = {"check_id": _gen_id("RC"), "mission_id": mission_id}
    all_ready = True
    for node_name, field in checks.items():
        ns = _get_node_status(store, mission_id, node_name)
        ready = 1 if ns.get("status") == "completed" else 0
        result[field] = ready
        if not ready:
            all_ready = False

    mission = store.query_by_id("LaunchMission", mission_id)
    result["services_ready"] = 1
    result["interlock_ready"] = 1 if mission and mission.get("safety_interlock") == "正常" else 0
    if not result["interlock_ready"]:
        all_ready = False
    result["all_ready"] = 1 if all_ready else 0

    store.insert_record("ReadinessCheck", result)
    return result


def _trigger_sync(store: Store, mission_id: str = "", trigger_type: str = "主发射", **kw) -> dict:
    sync_result = _execute_node(store, mission_id, "同步触发")
    pump_result = _execute_node(store, mission_id, "泵浦触发准备")
    switch_result = _execute_node(store, mission_id, "开关触发准备")

    return {
        "trigger_type": trigger_type,
        "timeline": "T0→同步触发指令, T+3s→泵浦+开关触发准备, T+5s→正式触发",
        "sync": sync_result,
        "pump": pump_result,
        "switch": switch_result,
    }


def _check_interlock(store: Store, mission_id: str = "", **kw) -> dict:
    mission = store.query_by_id("LaunchMission", mission_id)
    if not mission:
        return {"error": f"任务 {mission_id} 不存在"}
    return {
        "mission_id": mission_id,
        "interlock_status": mission.get("safety_interlock", "未知"),
        "conditions": {
            "shield_door_locked": True,
            "personnel_count_zero": True,
            "vacuum_normal": True,
        },
    }


def _trigger_interlock(store: Store, mission_id: str = "", trigger_type: str = "", **kw) -> dict:
    event_id = _gen_id("SI")
    record = {
        "event_id": event_id,
        "mission_id": mission_id,
        "trigger_type": trigger_type,
        "trigger_time": "now",
        "resolve_time": "",
        "status": "触发",
    }
    store.insert_record("SafetyInterlockEvent", record)
    return {"event": record, "message": "安全联锁已触发，所有分系统已下发终止指令"}


def _resolve_interlock(store: Store, mission_id: str = "", event_id: str = "", **kw) -> dict:
    return {"status": "mock", "message": f"安全联锁 {event_id} 已解除（mock模式）"}


def _emergency_action(store: Store, mission_id: str = "", action_type: str = "", reason: str = "", **kw) -> dict:
    action_id = _gen_id("EA")
    record = {
        "action_id": action_id,
        "mission_id": mission_id,
        "action_type": action_type,
        "target_subsystem": "泵浦分系统" if "泄放" in action_type or "停充" in action_type else "同步分系统",
        "trigger_reason": reason,
        "result": "已执行（mock模式）",
        "action_time": "now",
    }
    store.insert_record("EmergencyAction", record)
    return {"action": record}


def _get_next_executable(store: Store, mission_id: str = "", **kw) -> dict:
    all_edges = store.query("FlowEdge")
    completed_nodes = {n["node_name"] for n in store.query("FlowNode", filters={"mission_id": mission_id}) if n.get("status") == "completed"}

    all_targets = {e["to_node"] for e in all_edges}
    all_sources = {e["from_node"] for e in all_edges}

    candidates = []
    for target in all_targets:
        if target in completed_nodes:
            continue
        incoming = [e for e in all_edges if e["to_node"] == target]
        all_met = all(e["from_node"] in completed_nodes or e["from_node"] == "开始" for e in incoming)
        if all_met and incoming:
            candidates.append(target)

    return {"mission_id": mission_id, "completed": list(completed_nodes), "next_executable": candidates}


def _execute_parallel(store: Store, mission_id: str = "", node_names: str = "", **kw) -> dict:
    names = [n.strip() for n in node_names.split(",") if n.strip()]
    results = []
    for name in names:
        r = _execute_node(store, mission_id, name)
        results.append(r)
    return {"mission_id": mission_id, "parallel_results": results}


def _reserve_subsystems(store: Store, mission_id: str = "", subsystem_types: str = "", **kw) -> dict:
    types = [t.strip() for t in subsystem_types.split(",") if t.strip()]
    reservations = []
    for st in types:
        rid = _gen_id("RES")
        record = {
            "reservation_id": rid,
            "mission_id": mission_id,
            "subsystem_type": st,
            "reserved_time": "now",
            "release_time": "",
            "status": "已预约",
        }
        store.insert_record("ServiceReservation", record)
        reservations.append(record)
    return {"mission_id": mission_id, "reserved": len(reservations), "reservations": reservations}


def register(registry: FunctionRegistry, store: Store, ontology: Ontology):
    fn_map = {
        "get_subsystem": lambda **kw: _get_subsystem(store, **kw),
        "get_beam_line": lambda **kw: _simple_getter(store, "BeamLine", "beam_id", **kw),
        "get_shield_door": lambda **kw: _simple_getter(store, "ShieldDoor", "door_id", **kw),
        "get_personnel_count": lambda **kw: store.query("PersonnelCounter", filters={"location": kw["location"]} if kw.get("location") else None),
        "get_vacuum_status": lambda **kw: _simple_getter(store, "VacuumStatus", "device_id", **kw) if kw.get("device_id") else store.query("VacuumStatus"),
        "get_mission": lambda **kw: _get_mission(store, **kw),
        "get_node_definition": lambda **kw: _get_node_definition(store, **kw),
        "get_flow_edges": lambda **kw: _get_flow_edges(store, **kw),
        "get_node_status": lambda **kw: _get_node_status(store, **kw),
        "get_next_executable_nodes": lambda **kw: _get_next_executable(store, **kw),
        "execute_node": lambda **kw: _execute_node(store, **kw),
        "execute_parallel_nodes": lambda **kw: _execute_parallel(store, **kw),
        "check_readiness": lambda **kw: _check_readiness(store, **kw),
        "trigger_sync": lambda **kw: _trigger_sync(store, **kw),
        "check_interlock": lambda **kw: _check_interlock(store, **kw),
        "trigger_interlock": lambda **kw: _trigger_interlock(store, **kw),
        "resolve_interlock": lambda **kw: _resolve_interlock(store, **kw),
        "emergency_action": lambda **kw: _emergency_action(store, **kw),
        "reserve_subsystems": lambda **kw: _reserve_subsystems(store, **kw),
        "release_subsystems": lambda **kw: {"status": "mock", "message": "分系统预约已释放（mock模式）"},
        "collect_energy_data": lambda **kw: {"status": "mock", "message": "能量数据采集完成（mock模式）", "args": kw},
        "collect_laser_params": lambda **kw: {"status": "mock", "message": "激光参数采集完成（mock模式）", "args": kw},
    }

    for name, fn in fn_map.items():
        func_def = ontology.functions.get(name)
        if func_def:
            registry.register(name, fn, func_def)
