"""
CSA / SMS BASIC Calculator — локальный веб-сервер.

Запуск:
    pip3 install flask requests pandas
    python3 app.py
Потом открой в браузере: http://localhost:5000

Все запросы к data.transportation.gov идут отсюда (с сервера), поэтому нет
проблем с CORS/CSP, которые блокируют такие запросы из опубликованной
статической HTML-страницы.
"""

from flask import Flask, request, jsonify, send_from_directory
import requests
import csv
import os
from datetime import datetime, timedelta

app = Flask(__name__, static_folder="static")

BASE = "https://data.transportation.gov/resource"
DS_INSPECTIONS = "fx4q-ay7w"
DS_VIOLATIONS = "876r-jsdb"
DS_CRASHES = "aayw-vxb3"
DS_CENSUS = "az4n-8mr2"
DS_CITATIONS = "qbt8-7vic"  # Inspections and Citations — adjudicated citation results

SEVERITY_CSV = os.path.join(os.path.dirname(__file__), "severity_weight_table.csv")


def load_severity_table():
    table = {}
    with open(SEVERITY_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = row["violation_code"].strip()
            if not code:
                continue
            table[code] = {
                "basic": row["basic"],
                "weight": int(row["severity_weight"]) if row["severity_weight"] else None,
            }
    return table


SEVERITY_TABLE = load_severity_table()


def _norm_code(c):
    """393.47(e) / 393.47E / 393.75A1- -> '393.47e' / '393.47e' / '393.75a1'"""
    c = (c or "").strip().lower().replace("(", "").replace(")", "").replace(" ", "")
    return c.rstrip("-.")


# нормализованный индекс; неоднозначные ключи отбрасываем
_NORM_INDEX = {}
_AMBIG = set()
for _k in SEVERITY_TABLE:
    _n = _norm_code(_k)
    if _n in _NORM_INDEX and _NORM_INDEX[_n] != _k:
        _AMBIG.add(_n)
    _NORM_INDEX[_n] = _k
for _n in _AMBIG:
    _NORM_INDEX.pop(_n, None)


def resolve_code(code):
    """Возвращает (найденный_код_из_Appendix_A или None, список_подсказок)."""
    if code in SEVERITY_TABLE:
        return code, []
    n = _norm_code(code)
    hit = _NORM_INDEX.get(n)
    if hit:
        return hit, []
    # подсказки: коды с тем же началом (секция), либо начинающиеся одинаково
    base = n.split("-")[0]
    cands = [k for k in SEVERITY_TABLE if _norm_code(k).startswith(base)] if base else []
    if not cands and "." in base:
        sec = base.split(".")[0] + "." + "".join(ch for ch in base.split(".")[1] if ch.isdigit())
        cands = [k for k in SEVERITY_TABLE if _norm_code(k).startswith(sec)]
    return None, cands[:8]


def soda_get(dataset, params):
    url = f"{BASE}/{dataset}.json"
    all_rows = []
    offset = 0
    limit = params.get("$limit", 50000)
    while True:
        p = dict(params)
        p["$limit"] = limit
        p["$offset"] = offset
        resp = requests.get(url, params=p, timeout=60)
        resp.raise_for_status()
        rows = resp.json()
        all_rows.extend(rows)
        if len(rows) < limit:
            break
        offset += limit
    return all_rows


def parse_soda_date(raw):
    """SODA иногда отдаёт дату как '2023-09-04T00:00:00.000' (ISO),
    иногда как компактную '20230904' — обрабатываем оба варианта."""
    s = str(raw)[:10].replace("-", "")
    if len(s) != 8:
        # на случай неожиданного формата — пробуем ISO напрямую
        return datetime.strptime(str(raw)[:10], "%Y-%m-%d")
    return datetime.strptime(s, "%Y%m%d")


def time_weight(event_date, as_of):
    days = (as_of - event_date).days
    if days <= 183:
        return 3
    elif days <= 365:
        return 2
    elif days <= 730:
        return 1
    return 0


def utilization_factor(vmt_per_avg_pu, segment):
    if vmt_per_avg_pu is None:
        return 1.0
    if segment == "Combination":
        if vmt_per_avg_pu < 80_000:
            return 1.0
        elif vmt_per_avg_pu <= 160_000:
            return 1 + (vmt_per_avg_pu - 80_000) / 133_333
        elif vmt_per_avg_pu <= 200_000:
            return 1.6
        return 1.0
    else:
        if vmt_per_avg_pu < 20_000:
            return 1.0
        elif vmt_per_avg_pu <= 60_000:
            return vmt_per_avg_pu / 20_000
        elif vmt_per_avg_pu <= 200_000:
            return 3.0
        return 1.0


@app.route("/")
def index():
    return send_from_directory(os.path.dirname(__file__), "index.html")


@app.route("/api/census/<dot>")
def api_census(dot):
    rows = soda_get(DS_CENSUS, {"dot_number": dot})
    if not rows:
        return jsonify({"error": f"DOT {dot} не найден в Company Census File"}), 404
    c = rows[0]
    return jsonify({
        "legal_name": c.get("legal_name", "(без названия)"),
        "power_units": int(c.get("power_units") or 0),
        "mcs150_mileage": float(c["mcs150_mileage"]) if c.get("mcs150_mileage") else None,
        "mcs150_mileage_year": c.get("mcs150_mileage_year"),
    })


@app.route("/api/calculate", methods=["POST"])
def api_calculate():
    try:
        return _do_calculate(request.get_json(force=True))
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


def _do_calculate(body):
    dot = str(body["dot"])
    as_of = datetime.strptime(body["as_of"], "%Y-%m-%d")
    pu_current = int(body["pu_current"])
    pu6 = int(body.get("pu6") or pu_current)
    pu18 = int(body.get("pu18") or pu_current)
    segment = body.get("segment", "Combination")
    mcs150_mileage = body.get("mcs150_mileage")
    corrections = body.get("corrections", {})  # {insp_violation_id: code}

    window_start = as_of - timedelta(days=730)

    # --- Инспекции ---
    insp_rows = soda_get(DS_INSPECTIONS, {"dot_number": dot})
    inspections = []
    for r in insp_rows:
        d = parse_soda_date(r["insp_date"])
        if window_start <= d <= as_of:
            r["_insp_date"] = d
            r["_time_weight"] = time_weight(d, as_of)
            inspections.append(r)

    tw_by_insp = {r["inspection_id"]: r["_time_weight"] for r in inspections}
    level_by_insp = {r["inspection_id"]: str(r.get("insp_level_id")) for r in inspections}
    report_num_by_insp = {r["inspection_id"]: r.get("report_number") for r in inspections}

    # --- Нарушения (батчами по inspection_id) ---
    ids = list(tw_by_insp.keys())
    violations = []
    chunk = 200
    for i in range(0, len(ids), chunk):
        batch = [str(x) for x in ids[i:i + chunk]]
        where = "inspection_id in (" + ",".join(batch) + ")"
        violations.extend(soda_get(DS_VIOLATIONS, {"$where": where}))

    # --- Адъюдикация (DataQs / Inspections and Citations) ---
    citations = []
    citations_ok = True
    for i in range(0, len(ids), chunk):
        batch = [str(x) for x in ids[i:i + chunk]]
        where_numeric = "inspection_id in (" + ",".join(batch) + ")"
        where_text = "inspection_id in (" + ",".join(f"'{b}'" for b in batch) + ")"
        try:
            citations.extend(soda_get(DS_CITATIONS, {"$where": where_numeric}))
        except requests.HTTPError:
            try:
                citations.extend(soda_get(DS_CITATIONS, {"$where": where_text}))
            except requests.HTTPError as e:
                print(f"[warn] Inspections and Citations (qbt8-7vic) недоступен: {e}")
                citations_ok = False
                break

    # берём самую позднюю запись (максимальный adjseq) на (inspection_id, vioseqnum)
    citation_map = {}
    for c in citations:
        key = (c["inspection_id"], c.get("vioseqnum"))
        adjseq = int(c.get("adjseq") or 0)
        prev = citation_map.get(key)
        if prev is None or adjseq >= prev[0]:
            citation_map[key] = (adjseq, c.get("citation_result"))

    enriched = []
    for v in violations:
        vid = v["insp_violation_id"]
        raw_code = v.get("viol_code") or ""
        manual = corrections.get(vid)
        auto_code, suggestions = (None, [])
        if manual:
            code = manual
        else:
            auto_code, suggestions = resolve_code(raw_code)
            code = auto_code or raw_code
        sev = SEVERITY_TABLE.get(code)
        oos = str(v.get("out_of_service_indicator", "")).strip().upper() == "Y"
        tw = tw_by_insp.get(v["inspection_id"], 0)

        # Adjudicated Citations policy (SMS Methodology, Table 2-1):
        #  - "Not Guilty/Dismissed" -> нарушение исключается из SMS целиком
        #  - "Conviction of a Different Charge" -> severity=1, OOS-бонус не применяется
        #  - "Conviction of Original Charge" / нет записи -> без изменений
        cit_key = (v["inspection_id"], v.get("seq_no"))
        citation_result = citation_map.get(cit_key, (None, None))[1]
        excluded = citation_result == "Not Guilty/Dismissed"
        forced_sw1 = citation_result == "Conviction of a Different Charge"

        weight_for_calc = sev["weight"] if sev else None
        if forced_sw1:
            weight_for_calc = 1
        weighted = None
        if sev and not excluded:
            oos_bonus_here = 0 if forced_sw1 else (2 if oos else 0)
            weighted = (weight_for_calc + oos_bonus_here) * tw

        enriched.append({
            "insp_violation_id": vid,
            "inspection_id": v["inspection_id"],
            "report_number": report_num_by_insp.get(v["inspection_id"]),
            "insp_date": next((r["_insp_date"].strftime("%Y-%m-%d") for r in inspections if r["inspection_id"] == v["inspection_id"]), None),
            "level": level_by_insp.get(v["inspection_id"]),
            "viol_code": v.get("viol_code"),
            "effective_code": code,
            "auto_matched": bool(auto_code and auto_code != raw_code),
            "suggestions": suggestions if not sev else [],
            "viol_desc": v.get("viol_desc"),
            "matched": bool(sev) and not excluded,
            "basic": sev["basic"] if sev else None,
            "severity_weight": weight_for_calc,
            "oos": oos,
            "time_weight": tw,
            "weighted": weighted,
            "citation_result": citation_result,
            "excluded": excluded,
        })

    basic_names = ["Unsafe Driving", "HOS Compliance", "Vehicle Maintenance",
                   "Controlled Substances/Alcohol", "Driver Fitness"]
    # OOS-бонус (+2) НЕ применяется для Unsafe Driving и Controlled Substances/Alcohol
    # (SMS Methodology v3.21, разделы 3.1 и 3.6 — "OOS weights are not assigned...")
    OOS_BONUS_APPLIES = {
        "Unsafe Driving": False,
        "HOS Compliance": True,
        "Vehicle Maintenance": True,
        "Controlled Substances/Alcohol": False,
        "Driver Fitness": True,
    }
    basic_sums = {}
    insp_breakdown = {}  # inspection_id -> {basic: {raw, capped, weighted}}
    for name in basic_names:
        subset = [v for v in enriched if v["basic"] == name and not v["excluded"]]
        apply_oos = OOS_BONUS_APPLIES[name]

        # --- dedup: "each violation cite only once per inspection" ---
        # ключ: (inspection_id, effective_code). Если код встретился на инспекции
        # несколько раз — считаем один раз, но OOS=True если хоть один был OOS.
        dedup = {}
        for v in subset:
            key = (v["inspection_id"], v["effective_code"])
            if key not in dedup:
                dedup[key] = v
                v["duplicate"] = False
            else:
                v["duplicate"] = True
                if v["oos"]:
                    dedup[key]["oos"] = True

        by_insp = {}
        for v in dedup.values():
            raw = (v["severity_weight"] or 0) + (2 if (v["oos"] and apply_oos) else 0)
            by_insp[v["inspection_id"]] = by_insp.get(v["inspection_id"], 0) + raw

        # баллы каждого нарушения (после dedup и OOS-правил)
        for v in dedup.values():
            v["points"] = (v["severity_weight"] or 0) + (2 if (v["oos"] and apply_oos) else 0)
            v["oos_bonus_applied"] = bool(v["oos"] and apply_oos)

        total = 0.0
        for insp_id, raw in by_insp.items():
            capped = min(raw, 30)  # кап 30 баллов на инспекцию — для ВСЕХ BASIC
            w = capped * tw_by_insp.get(insp_id, 0)
            total += w
            insp_breakdown.setdefault(insp_id, {})[name] = {"raw": raw, "capped": capped, "weighted": w}
        basic_sums[name] = total

    denom_all = sum(tw_by_insp.values())
    denom_vm = sum(tw for insp_id, tw in tw_by_insp.items() if level_by_insp.get(insp_id) in ("1", "2"))

    avg_pu = (pu_current + pu6 + pu18) / 3
    vmt_per_avg_pu = (mcs150_mileage / avg_pu) if (mcs150_mileage and avg_pu) else None
    uf = utilization_factor(vmt_per_avg_pu, segment)
    pu_uf = avg_pu * uf

    # --- Аварии ---
    crash_rows = soda_get(DS_CRASHES, {"dot_number": dot})
    crashes = []
    for c in crash_rows:
        d = parse_soda_date(c["report_date"])
        if window_start <= d <= as_of:
            inj = float(c.get("injuries") or 0)
            fat = float(c.get("fatalities") or 0)
            sw = 2 if (inj > 0 or fat > 0) else 1
            if str(c.get("hazmat_released", "")).strip().upper() == "Y":
                sw += 1
            tw = time_weight(d, as_of)
            crashes.append({"weighted": sw * tw})
    crash_sum = sum(c["weighted"] for c in crashes)

    measures = {
        "Unsafe Driving": basic_sums["Unsafe Driving"] / pu_uf if pu_uf else None,
        "Crash Indicator": crash_sum / pu_uf if pu_uf else None,
        "HOS Compliance": basic_sums["HOS Compliance"] / denom_all if denom_all else None,
        "Vehicle Maintenance": basic_sums["Vehicle Maintenance"] / denom_vm if denom_vm else None,
        "Controlled Substances/Alcohol": basic_sums["Controlled Substances/Alcohol"] / denom_all if denom_all else None,
        "Driver Fitness": basic_sums["Driver Fitness"] / denom_all if denom_all else None,
    }

    viols_by_insp = {}
    for v in enriched:
        viols_by_insp.setdefault(v["inspection_id"], []).append(v)
    insp_out = []
    for r in sorted(inspections, key=lambda x: x["_insp_date"], reverse=True):
        iid = r["inspection_id"]
        insp_out.append({
            "inspection_id": iid,
            "report_number": r.get("report_number"),
            "insp_date": r["_insp_date"].strftime("%Y-%m-%d"),
            "level": str(r.get("insp_level_id")),
            "time_weight": r["_time_weight"],
            "counts_in_vm_denom": str(r.get("insp_level_id")) in ("1", "2"),
            "violations": viols_by_insp.get(iid, []),
            "by_basic": insp_breakdown.get(iid, {}),
        })

    return jsonify({
        "inspections": insp_out,
        "measures": measures,
        "raw_sums": {
            "Unsafe Driving": basic_sums["Unsafe Driving"],
            "Crash Indicator": crash_sum,
            "HOS Compliance": basic_sums["HOS Compliance"],
            "Vehicle Maintenance": basic_sums["Vehicle Maintenance"],
            "Controlled Substances/Alcohol": basic_sums["Controlled Substances/Alcohol"],
            "Driver Fitness": basic_sums["Driver Fitness"],
        },
        "meta": {
            "avg_pu": avg_pu,
            "vmt_per_avg_pu": vmt_per_avg_pu,
            "uf": uf,
            "pu_uf": pu_uf,
            "denom_all": denom_all,
            "denom_vm": denom_vm,
            "inspections_count": len(inspections),
            "crashes_count": len(crashes),
            "citations_ok": citations_ok,
            "segment": segment,
            "mcs150_mileage": mcs150_mileage,
        },
        "violations": enriched,
    })


if __name__ == "__main__":
    print(f"Загружена таблица весов: {len(SEVERITY_TABLE)} кодов нарушений")
    port = int(os.environ.get("PORT", 5050))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", debug=debug, port=port)
