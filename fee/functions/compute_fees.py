from __future__ import annotations

from collections import defaultdict
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


def _float(v: Any) -> float:
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v))
    except (ValueError, TypeError):
        return 0.0


def _str(v: Any) -> str | None:
    return str(v) if v is not None else None


def compute_fees(store: ObjectRepository,
                 vehicle_types: str = "1,2,3,4,11,12,13,14,15,16") -> dict:
    vcs = [int(x.strip()) for x in str(vehicle_types).split(",") if x.strip()]

    units = store.query("TollUnit")
    rates = store.query("BaseRate")
    discounts = store.query("SpecialTimeDiscount")

    rate_map: dict[str, dict[int, float]] = defaultdict(dict)
    for rate in rates:
        rate_map[_str(rate.get("rate_code"))][_int(rate.get("vehicle_type"))] = _float(rate.get("vc_rate"))

    discount_by_unit: dict[str, list[dict]] = defaultdict(list)
    for discount in discounts:
        unit_id = _str(discount.get("toll_interval_id"))
        if unit_id:
            discount_by_unit[unit_id].append(discount)

    rows = []
    r3_errors = 0
    for unit in units:
        unit_id = _str(unit.get("toll_interval_id"))
        rate_code = _str(unit.get("rate_code"))
        charge_length = _int(unit.get("charge_length"))

        vc_rates = rate_map.get(rate_code)
        if not vc_rates:
            r3_errors += len(vcs)
            continue

        for vc in vcs:
            vc_rate = vc_rates.get(vc)
            if vc_rate is None:
                r3_errors += 1
                continue

            fee = _base_fee(rate_code, vc_rate, charge_length)
            full_day = _find_full_day_discount(discount_by_unit.get(unit_id, []), vc)
            if full_day:
                cpc = _int(full_day.get("cpc_discount"))
                etc = _int(full_day.get("etc_discount"))
                mfee = round(fee * (1000 - cpc) / 1000)
                efee = round(fee * (1000 - etc) / 1000)
                rate_source = "discount"
            else:
                mfee = fee
                efee = round(fee * 0.95)
                rate_source = "default"

            rows.append({
                "param_id": f"{unit_id}:{vc}",
                "toll_interval_id": unit_id,
                "vehicle_type": vc,
                "fee": fee,
                "mfee": mfee,
                "efee": efee,
                "rate_source": rate_source,
            })

    _replace_all(store, "ProvinceRateParam", rows)
    return {"params_created": len(rows), "r3_errors": r3_errors}


def _base_fee(rate_code: str | None, vc_rate: float, charge_length: int) -> int:
    try:
        rate_code_val = int(rate_code or "0")
    except (ValueError, TypeError):
        rate_code_val = 0
    if rate_code_val < 50:
        return round(vc_rate * charge_length)
    return round(vc_rate)


def _find_full_day_discount(discounts: list[dict], vc: int) -> dict | None:
    for discount in discounts:
        if _int(discount.get("start_hour")) == 0 and _int(discount.get("end_hour")) == 24:
            if _str(discount.get("vehicle_type")) == str(vc):
                return discount
    return None


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
