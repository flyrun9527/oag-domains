from __future__ import annotations

import heapq
import math
from collections import defaultdict
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


def find_path(store: ObjectRepository, en_station_id: str = "",
              ex_station_id: str = "", vehicle_type: int = 1) -> dict:
    if not en_station_id or not ex_station_id:
        return {"error": "需要参数: en_station_id, ex_station_id"}

    vt = int(vehicle_type)
    rate_params = {
        row["toll_interval_id"]: (row["fee"], row["mfee"], row["efee"])
        for row in store.query("ProvinceRateParam", filters={"vehicle_type": vt})
    }
    if not rate_params:
        return {"error": "未找到车型费额参数，请先执行 compute_fees"}

    graph: dict[str, list[tuple]] = defaultdict(list)
    for row in store.query("Contiguity", filters={"invalid": 0}):
        en_id = str(row["en_road_node_id"])
        en_type = _int(row["en_road_node_type"])
        ex_id = str(row["ex_road_node_id"])
        ex_type = _int(row["ex_road_node_type"])
        charge_miles = _int(row.get("charge_miles"))

        from_key = f"{en_id}|{en_type}"
        toll_unit_id = en_id if en_type == 0 else None
        weight = 0
        if toll_unit_id and toll_unit_id in rate_params:
            weight = rate_params[toll_unit_id][1]

        graph[from_key].append((ex_id, ex_type, toll_unit_id, weight, charge_miles))

    start_key = f"{en_station_id}|1"
    end_key = f"{ex_station_id}|1"
    if start_key not in graph:
        return {"error": f"入口站 {en_station_id} 在图中没有出边"}

    dist, prev, prev_edge = _dijkstra(graph, start_key, end_key)
    if dist[end_key] == float("inf"):
        return {"error": f"从 {en_station_id} 到 {ex_station_id} 无可达路径"}

    path_keys = _backtrack(prev, start_key, end_key)
    toll_units, mfees, efees, total_mileage = _collect_path(
        path_keys,
        prev_edge,
        rate_params,
    )

    total_fee = int(math.floor(sum(mfees) / 100 + 0.5) * 100)
    total_fee95 = int(min(sum(efees), math.floor(sum(mfees) / 100) * 100 * 0.95))
    total_fee95 = int(math.floor(total_fee95 / 100 + 0.5) * 100)

    path_id = _next_path_id(store)
    record = {
        "path_id": path_id,
        "en_station_id": en_station_id,
        "ex_station_id": ex_station_id,
        "vehicle_type": vt,
        "toll_intervals": ",".join(toll_units),
        "chargefee_group": ",".join(str(x) for x in mfees),
        "chargefee95_group": ",".join(str(x) for x in efees),
        "total_fee": total_fee,
        "total_fee95": total_fee95,
        "total_mileage": total_mileage,
    }
    store.insert_record("MinimumFeePath", record)

    return {
        **record,
        "toll_units": toll_units,
        "mfee_list": mfees,
        "efee_list": efees,
    }


def _dijkstra(graph: dict[str, list[tuple]], start_key: str, end_key: str):
    dist: dict[str, float] = defaultdict(lambda: float("inf"))
    dist[start_key] = 0
    prev: dict[str, str | None] = {}
    prev_edge: dict[str, tuple | None] = {}
    visited: set[str] = set()
    pq = [(0, start_key)]

    while pq:
        d, cur = heapq.heappop(pq)
        if cur in visited:
            continue
        visited.add(cur)
        if cur == end_key:
            break
        for ex_id, ex_type, tu_id, weight, charge_miles in graph.get(cur, []):
            next_key = f"{ex_id}|{ex_type}"
            new_dist = d + weight
            if new_dist < dist[next_key]:
                dist[next_key] = new_dist
                prev[next_key] = cur
                prev_edge[next_key] = (ex_id, ex_type, tu_id, weight, charge_miles)
                heapq.heappush(pq, (new_dist, next_key))
    return dist, prev, prev_edge


def _backtrack(prev: dict[str, str | None], start_key: str, end_key: str) -> list[str]:
    path_keys = []
    cur = end_key
    while cur and cur != start_key:
        path_keys.append(cur)
        cur = prev.get(cur)
    path_keys.append(start_key)
    path_keys.reverse()
    return path_keys


def _collect_path(path_keys: list[str], prev_edge: dict[str, tuple | None],
                  rate_params: dict[str, tuple[int, int, int]]):
    toll_units = []
    mfees = []
    efees = []
    total_mileage = 0
    for key in path_keys[1:]:
        edge = prev_edge.get(key)
        if edge and edge[2]:
            toll_unit_id = edge[2]
            toll_units.append(toll_unit_id)
            params = rate_params.get(toll_unit_id, (0, 0, 0))
            mfees.append(params[1])
            efees.append(params[2])
            total_mileage += edge[4]
    return toll_units, mfees, efees, total_mileage


def _next_path_id(store: ObjectRepository) -> int:
    rows = store.query("MinimumFeePath")
    existing = [_int(row.get("path_id")) for row in rows]
    return max(existing, default=0) + 1
