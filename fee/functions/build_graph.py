from __future__ import annotations

from typing import Any

from oag_ontology.repository import ObjectRepository


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


def build_graph(store: ObjectRepository, version_id: str = "") -> dict:
    units = store.query("TollUnit")
    stations = store.query("TollStation")
    rules = store.query("NoContiguityRule")

    edges: list[dict] = []

    # E1: toll unit -> toll unit.
    for a in units:
        a_end_id = _str(a.get("end_org_id"))
        a_end_type = _int(a.get("end_org_type"))
        a_opposite = _str(a.get("opposite_id"))
        a_id = _str(a.get("toll_interval_id"))
        a_miles = _int(a.get("actual_length"))
        a_charge = _int(a.get("charge_length"))

        for b in units:
            b_id = _str(b.get("toll_interval_id"))
            if b_id == a_id:
                continue
            if a_opposite and b_id == a_opposite:
                continue
            b_start_id = _str(b.get("start_org_id"))
            b_start_type = _int(b.get("start_org_type"))

            if a_end_id and a_end_id == b_start_id and a_end_type == b_start_type:
                edges.append(_edge(
                    en=a_id,
                    en_type=0,
                    ex=b_id,
                    ex_type=0,
                    miles=a_miles,
                    charge_miles=a_charge,
                ))

    # E2: active station -> unit.
    for unit in units:
        if _int(unit.get("start_org_type")) != 1:
            continue
        start_org_id = _str(unit.get("start_org_id"))
        unit_id = _str(unit.get("toll_interval_id"))
        for station in stations:
            if _int(station.get("use_status")) != 2:
                continue
            if _str(station.get("station_id")) == start_org_id:
                edges.append(_edge(
                    en=_str(station.get("station_id")),
                    en_type=1,
                    ex=unit_id,
                    ex_type=0,
                    miles=0,
                    charge_miles=0,
                ))

    # E3: unit -> station.
    for unit in units:
        if _int(unit.get("end_org_type")) != 1:
            continue
        end_org_id = _str(unit.get("end_org_id"))
        unit_id = _str(unit.get("toll_interval_id"))
        miles = _int(unit.get("actual_length"))
        charge = _int(unit.get("charge_length"))
        for station in stations:
            if _str(station.get("station_id")) == end_org_id:
                edges.append(_edge(
                    en=unit_id,
                    en_type=0,
                    ex=_str(station.get("station_id")),
                    ex_type=1,
                    miles=miles,
                    charge_miles=charge,
                ))

    invalidated = _apply_rules(edges, rules)
    deduped = _dedupe(edges)
    _replace_all(store, "Contiguity", [
        dict(edge, edge_id=f"E{i + 1:04d}") for i, edge in enumerate(deduped)
    ])

    return {
        "edges_created": len(deduped),
        "edges_invalidated": invalidated,
        "version_id": version_id,
    }


def _edge(en: str | None, en_type: int, ex: str | None, ex_type: int,
          miles: int, charge_miles: int, invalid: int = 0,
          rule_id: str | None = None) -> dict:
    return {
        "en_road_node_id": en or "",
        "en_road_node_type": en_type,
        "ex_road_node_id": ex or "",
        "ex_road_node_type": ex_type,
        "miles": miles,
        "charge_miles": charge_miles,
        "invalid": invalid,
        "rule_id": rule_id or "",
    }


def _apply_rules(edges: list[dict], rules: list[dict]) -> int:
    invalidated = 0
    for rule in rules:
        c_type = _int(rule.get("contiguity_type"))
        en_id = _str(rule.get("en_road_node_id"))
        en_type = _int(rule.get("en_road_node_type"))
        ex_id = _str(rule.get("ex_road_node_id"))
        ex_type = _int(rule.get("ex_road_node_type"))
        if c_type == 1:
            for edge in edges:
                if _same_edge(edge, en_id, en_type, ex_id, ex_type):
                    edge["invalid"] = 1
                    edge["rule_id"] = "E4"
                    invalidated += 1
        elif c_type in (2, 3):
            exists = any(
                _same_edge(edge, en_id, en_type, ex_id, ex_type)
                and not edge.get("invalid")
                for edge in edges
            )
            if not exists:
                edges.append(_edge(
                    en=en_id,
                    en_type=en_type,
                    ex=ex_id,
                    ex_type=ex_type,
                    miles=0,
                    charge_miles=0,
                    rule_id="E5",
                ))
    return invalidated


def _same_edge(edge: dict, en_id: str | None, en_type: int,
               ex_id: str | None, ex_type: int) -> bool:
    return (
        edge.get("en_road_node_id") == en_id
        and edge.get("en_road_node_type") == en_type
        and edge.get("ex_road_node_id") == ex_id
        and edge.get("ex_road_node_type") == ex_type
    )


def _dedupe(edges: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result = []
    for edge in edges:
        key = (
            f"{edge['en_road_node_id']}|{edge['en_road_node_type']}|"
            f"{edge['ex_road_node_id']}|{edge['ex_road_node_type']}"
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(edge)
    return result


def _replace_all(store: ObjectRepository, object_type: str, rows: list[dict]):
    adapter = store.adapter_for(object_type)
    replace_all = getattr(adapter, "replace_all", None)
    if callable(replace_all):
        replace_all(rows)
        return
    for row in store.query(object_type):
        store.delete_record(object_type, row[store.ontology.get_id_column(object_type)])
    for row in rows:
        store.insert_record(object_type, row)
