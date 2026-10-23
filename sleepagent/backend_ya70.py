"""YA70 日期区间统计接口的只读适配层。"""
from __future__ import annotations

import asyncio
import json
import ipaddress
import time
import urllib.error
import urllib.parse
import urllib.request

from . import async_http, nightdata

DEFAULT_URL = "http://127.0.0.1:8081/infc/respStatisticsYa70/listByDateRange"
MODE_LABELS = {
    "1": "CPAP",
    "2": "AutoCPAP",
    "3": "S",
    "4": "AutoS",
    "5": "T",
    "6": "AutoST",
}


def _number(value, divisor=1.0):
    if value is None or value == "":
        return None
    try:
        return round(float(value) / divisor, 2)
    except (TypeError, ValueError):
        return None


def _copy_number(target: dict, row: dict, source: str, target_key: str,
                 divisor=1.0):
    value = _number(row.get(source), divisor)
    if value is not None:
        target[target_key] = value


def _opener_for(url: str):
    """局域网地址绕过系统代理，公网地址沿用系统代理配置。"""
    host = urllib.parse.urlsplit(url).hostname or ""
    try:
        local = host == "localhost" or ipaddress.ip_address(host).is_private
    except ValueError:
        local = False
    if local:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def fetch_rows(user_id: int, start_date: str, end_date: str, api_key: str,
               url: str = DEFAULT_URL, timeout: float = 20) -> list:
    payload = {
        "userid": int(user_id),
        "startDate": start_date,
        "endDate": end_date,
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "key": api_key},
        method="POST",
    )
    body = ""
    opener = _opener_for(url)
    for attempt in range(2):
        try:
            with opener.open(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", "replace")
            break
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise RuntimeError("YA70 接口 HTTP " + str(exc.code) + "：" + body)
        except urllib.error.URLError as exc:
            if attempt == 0:
                time.sleep(0.5)
                continue
            raise RuntimeError("YA70 接口连接失败：" + str(exc))
    data = json.loads(body)
    if data.get("recode") != 1:
        raise RuntimeError(
            "YA70 接口返回失败：" + str(data.get("msg") or data.get("message") or data)
        )
    rows = data.get("result")
    if not isinstance(rows, list):
        raise RuntimeError("YA70 接口没有返回 result 数组")
    return rows


async def fetch_rows_async(user_id: int, start_date: str, end_date: str,
                           api_key: str, url: str = DEFAULT_URL,
                           timeout: float = 20) -> list:
    """异步查询 YA70 接口。"""
    payload = {
        "userid": int(user_id),
        "startDate": start_date,
        "endDate": end_date,
    }
    response = None
    for attempt in range(2):
        try:
            response = await async_http.request(
                "POST", url,
                json=payload,
                headers={"Content-Type": "application/json", "key": api_key},
                timeout=timeout,
            )
            break
        except Exception as exc:
            if attempt == 0:
                await asyncio.sleep(0.5)
                continue
            raise RuntimeError("YA70 接口连接失败：" + str(exc)) from exc
    if response is None:
        raise RuntimeError("YA70 接口没有返回响应")
    if response.status_code >= 400:
        raise RuntimeError(
            "YA70 接口 HTTP " + str(response.status_code) + "："
            + response.text[:500]
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("YA70 接口返回非 JSON 内容") from exc
    if data.get("recode") != 1:
        raise RuntimeError(
            "YA70 接口返回失败：" + str(data.get("msg") or data.get("message") or data)
        )
    rows = data.get("result")
    if not isinstance(rows, list):
        raise RuntimeError("YA70 接口没有返回 result 数组")
    return rows


def map_row(row: dict, user_id: int) -> dict:
    record_date = str(row.get("recordeDate") or row.get("dateDate") or "")[:10]
    if not record_date:
        raise ValueError("YA70 行缺少 recordeDate/dateDate")
    result = {
        "user_id": str(row.get("userid") or user_id),
        "record_date": record_date,
        "report_id": str(row.get("id") or ""),
        "device_id": row.get("deviceid"),
        "device_type": row.get("deviceType"),
        "pi_code": row.get("piCode"),
        "firmware_version": row.get("fwVersion"),
        "hardware_version": row.get("hdVersion"),
        "timezone": row.get("timezone"),
        "start_time": row.get("startTime"),
        "end_time": row.get("endTime"),
        "pressure_unit": "hPa",
        "data_source": "YA70 本地统计接口",
        "minute_detail_coverage": "missing",
        "event_detail_coverage": "missing",
    }
    for source, target in (
        ("treatTime", "treat_time_minutes"),
        ("treatScore", "treat_score"),
        ("treatDurationScore", "treat_duration_score"),
        ("maskScore", "mask_score"),
        ("respScore", "resp_score"),
        ("ahi", "ahi"),
        ("apneaIndex", "apnea_index"),
        ("hypopneaIndex", "hypopnea_index"),
        ("centralApneaIndex", "central_apnea_index"),
        ("apneaCountTotal", "apnea_count_total"),
        ("hypopneaCountTotal", "hypopnea_count_total"),
        ("caCountTotal", "ca_count_total"),
        ("apneaDurationTotal", "apnea_duration_total_sec"),
        ("hypopneaDurationTotal", "hypopnea_duration_total_sec"),
        ("tidalVolumeAvg", "tidal_volume_avg_ml"),
        ("tidalVolume95", "tidal_volume_p95_ml"),
        ("hzAvg", "respiratory_rate_avg"),
        ("hz95", "respiratory_rate_p95"),
        ("inspiredAirTimeAvg", "inspiratory_time_avg_sec"),
        ("airLeakageAvg", "air_leakage_avg"),
        ("airLeakage95", "air_leakage_p95"),
        ("airLeakageDurationMax", "large_leak_minutes"),
        ("airLeakageDurationPercent", "large_leak_percent"),
        ("leakLevel", "leak_level"),
    ):
        _copy_number(result, row, source, target)

    # 接口压力实际按 Pa 返回；内部统一使用 hPa。
    for source, target in (
        ("treatPressure", "treat_pressure_avg"),
        ("treatPressure95", "treat_pressure_p95"),
        ("inspiredAirPressureAvg", "inspired_pressure_avg"),
        ("inspiredAirPressure95", "inspired_pressure_p95"),
        ("expirationPressureAvg", "expiratory_pressure_avg"),
        ("expirationPressure95", "expiratory_pressure_p95"),
        ("peepAvg", "peep_avg"),
        ("peep95", "peep_p95"),
    ):
        _copy_number(result, row, source, target, divisor=100.0)

    models = [
        MODE_LABELS.get(item.strip(), item.strip())
        for item in str(row.get("treatModels") or "").split(",")
        if item.strip()
    ]
    if models:
        result["therapy_mode"] = "/".join(models)
        result["therapy_modes"] = models
    return result


def rows_to_nights(rows: list, user_id: int) -> list:
    records = [map_row(row, user_id) for row in rows]
    records.sort(key=lambda item: item["record_date"])
    nights = []
    for index, record in enumerate(records):
        night = nightdata.Night(
            raw=record,
            user_id=str(user_id),
            history=records[:index],
        )
        nightdata.build_facts(str(user_id), night)
        nights.append(night)
    return nights
