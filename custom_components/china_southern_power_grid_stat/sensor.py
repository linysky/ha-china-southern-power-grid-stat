"""Sensors for the China Southern Power Grid Statistics integration."""

from __future__ import annotations

import asyncio
import calendar
import datetime
import json
import logging
import os
import time
import traceback
from datetime import timedelta
from typing import Any

import async_timeout
from homeassistant.components import persistent_notification
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, STATE_UNAVAILABLE, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback

try:  # HA 2023.7+ ; kept optional so older cores still load this module
    from homeassistant.core import SupportsResponse
except ImportError:  # pragma: no cover
    SupportsResponse = None
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from . import CONF_UPDATED_AT
from .const import (
    ATTR_KEY_CURRENT_LADDER_START_DATE,
    ATTR_KEY_HISTORY_BY_MONTH,
    ATTR_KEY_LAST_MONTH_BY_DAY,
    ATTR_KEY_LAST_YEAR_BY_MONTH,
    ATTR_KEY_LATEST_DAY_DATE,
    ATTR_KEY_THIS_MONTH_BY_DAY,
    ATTR_KEY_THIS_YEAR_BY_MONTH,
    CONF_API_PROFILE,
    CONF_AUTH_TOKEN,
    CONF_ELE_ACCOUNTS,
    CONF_LOGIN_TYPE,
    CONF_SETTINGS,
    CONF_UPDATE_INTERVAL,
    DATA_KEY_LAST_UPDATE_DAY,
    DOMAIN,
    HISTORY_YEAR_COUNT,
    SETTING_LAST_MONTH_UPDATE_DAY_THRESHOLD,
    SETTING_LAST_YEAR_UPDATE_DAY_THRESHOLD,
    SETTING_UPDATE_TIMEOUT,
    STATE_UPDATE_UNCHANGED,
    SUFFIX_ARR,
    SUFFIX_BAL,
    SUFFIX_CURRENT_LADDER,
    SUFFIX_CURRENT_LADDER_REMAINING_KWH,
    SUFFIX_CURRENT_LADDER_TARIFF,
    SUFFIX_LAST_MONTH_COST,
    SUFFIX_LAST_MONTH_KWH,
    SUFFIX_LAST_YEAR_COST,
    SUFFIX_LAST_YEAR_KWH,
    SUFFIX_LATEST_DAY_COST,
    SUFFIX_LATEST_DAY_KWH,
    SUFFIX_THIS_MONTH_COST,
    SUFFIX_THIS_MONTH_KWH,
    SUFFIX_THIS_YEAR_COST,
    SUFFIX_THIS_YEAR_KWH,
    SUFFIX_YESTERDAY_KWH,
    auth_notification_id,
    redact_identifier,
)
from .csg_client import (
    JSON_KEY_METERING_POINT_NUMBER,
    WF_ATTR_CHARGE,
    WF_ATTR_DATE,
    WF_ATTR_KWH,
    WF_ATTR_LADDER,
    WF_ATTR_LADDER_REMAINING_KWH,
    WF_ATTR_LADDER_START_DATE,
    WF_ATTR_LADDER_TARIFF,
    WF_ATTR_MONTH,
    CSGAPIError,
    CSGClient,
    CSGElectricityAccount,
    NotLoggedIn,
    api_profile_for_login_type,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 每日电费历史 (v3, 2026-11-14)
# 南网不再提供每日电费接口，也不下发分档明细：每日电费按「当月累计电量所落
# 的阶梯档」计价，再反解第一档基准价，使每月合计精确等于官方账单。
# v2：阶梯电价 + 历史永久保留 + 旧缓存自动升级。
# v3 变更（针对「同档内当日单价会跳动」的显示问题）：
#   1. 按分记账产生的尾差不再全塞给月末最后一天，改用最大余数法分摊到
#      小数部分最大的若干天，月末不再出现单价异常尖峰；
#   2. 缓存每天额外记录 tier_price = 当天实际套用的电价（单档日为固定档位
#      价，跨档日为按电量加权的混合价），网页据此显示固定电价，并把
#      电费÷电量的实算值单独列为一列；
#   3. ALGO_VERSION 升到 3，旧缓存自动全量重算（复用已抓到的每日电量，
#      不重复请求南网接口）。
# v4 变更（针对「账单 0 元但确有用电量」的月份整月不显示数据）：
#   1. _bills_lookup 不再丢弃 charge=0 的账单行；预缴抵扣 / 未出账月份
#      （账单 0 元、但确有用电量）照样进入每日电量构建流程；
#   2. 这类月份照样读取南网每日电量日历，电费按账单真值记 0，
#      用电量显示真实每日电量（日历缺数据时退化为账单均摊）；
#   3. 新增来源标记 zb（0 元账单 + 真实每日电量）、zm（0 元账单 + 均摊）；
#   4. 若电网日后补出正式账单（charge 由 0 变正），该月自动重新计价；
#   5. ALGO_VERSION 升到 4，旧缓存自动重算并复用已抓到的每日电量，
#      不会重复请求南网接口。
# ---------------------------------------------------------------------------
DAILY_HISTORY_YEARS = 3       # 仅在 KEEP_ALL_HISTORY=False 时作为滚动窗口长度
DAILY_HISTORY_FILENAME = "csg_daily_cost_history.json"
SERVICE_QUERY_DAILY_COST = "query_daily_cost"
WRITE_DAILY_HISTORY_JSON = True   # 关闭后只保留内存数据，不写 /config/www
DAILY_FETCH_PAUSE = 0.25          # 逐月抓取的间隔，避免触发南网限流
DAILY_FETCH_MAX_FAILS = 3         # 连续失败即判定更早月份无数据，改用均摊

# ---- v2: 永久保留 -----------------------------------------------------------
KEEP_ALL_HISTORY = True        # True = 历史只增不删；False = 只保留近 DAILY_HISTORY_YEARS 年
ALGO_VERSION = 4               # 算法版本；变更计价参数后请 +1 以触发全量重算

# ---- v2: 阶梯电价 -----------------------------------------------------------
ENABLE_TIER_PRICING = True     # False = 退回整月统一单价（v1 行为）
# 阶梯按月累计电量分档（kWh）。依据粤价〔2012〕135 号，广东居民：
# 夏季(5-10 月) 0-260 / 261-600 / 601+；非夏季(11-4 月) 0-200 / 201-400 / 401+。
# 不在广东请改成当地档位；若已办理「一户多人口」(5 人及以上)，各档基数
# 增加 100 度，改为 (360, 700) / (300, 500)。
TIER_THRESHOLDS_SUMMER = (260, 600)
TIER_THRESHOLDS_NON_SUMMER = (200, 400)
# 各档相对第一档的加价（元/kWh）：二档 +0.05、三档 +0.30（同上文件）。
# 基准价由官方账单反解，这里只需档间差额，不必知道绝对电价；
# 因此即便当地第一档电价与广州不同，结果依然贴合你的真实账单。
TIER_SURCHARGES = (0.0, 0.05, 0.30)
# 如已知当地绝对电价，可填 (0.68, 0.73, 0.98)；仍会按账单总额校准。
TIER_TARIFFS = None

SRC_COORDINATOR = "cd"       # 集成现有当月/上月每日明细
SRC_DAILY_METER = "dm"       # 抓到的历史真实每日电量
SRC_MONTHLY_AVERAGE = "ma"   # 账单均摊估算
# v4: 账单电费为 0（预缴抵扣 / 未出账）但该月确有用电量时的专用标记。
SRC_ZERO_BILL_DM = "zb"      # 0 元账单 + 真实每日电量
SRC_ZERO_BILL_MA = "zm"      # 0 元账单 + 账单电量均摊估算
SRC_LABELS = {
    SRC_COORDINATOR: "集成每日明细",
    SRC_DAILY_METER: "真实每日电量 x 阶梯电价",
    SRC_MONTHLY_AVERAGE: "月度账单均摊估算",
    SRC_ZERO_BILL_DM: "账单 0 元（预缴抵扣）· 真实每日电量",
    SRC_ZERO_BILL_MA: "账单 0 元（预缴抵扣）· 电量均摊估算",
}


def _normalize_ym(value: str) -> str:
    """'2023-04' / '202304' -> '202304'."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[:6] if len(digits) >= 6 else ""


def _target_months(earliest: str | None = None) -> list[str]:
    """Months from `earliest` (or a rolling 3-year window) up to this month."""
    today = datetime.date.today()
    if earliest and len(earliest) >= 6:
        year, month = int(earliest[:4]), int(earliest[4:6])
    else:
        start = today - datetime.timedelta(days=365 * DAILY_HISTORY_YEARS)
        year, month = start.year, start.month
    result = []
    while (year, month) <= (today.year, today.month):
        result.append(f"{year:04d}{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return result


def _tier_thresholds(year_month: str) -> tuple[float, float]:
    month = int(str(year_month)[4:6])
    pair = TIER_THRESHOLDS_SUMMER if 5 <= month <= 10 else TIER_THRESHOLDS_NON_SUMMER
    return float(pair[0]), float(pair[1])


def _tier_segments(
    cum_before: float, kwh: float, thresholds: tuple[float, float]
) -> list[tuple[int, float]]:
    """Split one day's kWh into the ladder tiers it crosses."""
    bounds = (0.0, thresholds[0], thresholds[1], float("inf"))
    segments = []
    for index in range(3):
        overlap = min(cum_before + kwh, bounds[index + 1]) - max(
            cum_before, bounds[index]
        )
        if overlap > 1e-9:
            segments.append((index + 1, overlap))
    return segments


def _apportion_cents(raw: list[float], target: float) -> list[float]:
    """Spread the rounding remainder over whole cents (largest remainder).

    Money must be recorded in whole cents and the month must still add up to
    the official bill exactly. v2 dumped every leftover cent on the last day
    of the month, which made that day's effective unit price visibly higher.
    v3 gives the extra cents to the days with the largest fractional parts,
    so each day is off by less than one cent and no day is singled out.
    """
    if not raw:
        return []
    total = sum(raw)
    if total <= 0 or target <= 0:
        return [0.0 for _ in raw]
    factor = target / total
    ideal = [max(0.0, value * factor * 100.0) for value in raw]
    cents = [int(value) for value in ideal]
    remaining = int(round(target * 100)) - sum(cents)
    if remaining > 0:
        order = sorted(
            range(len(ideal)),
            key=lambda i: (ideal[i] - cents[i], ideal[i]),
            reverse=True,
        )
        while remaining > 0 and order:
            for i in order:
                if remaining <= 0:
                    break
                cents[i] += 1
                remaining -= 1
    elif remaining < 0:
        need = -remaining
        for i in sorted(range(len(cents)), key=lambda i: -cents[i]):
            if need <= 0:
                break
            take = min(need, cents[i])
            cents[i] -= take
            need -= take
    return [value / 100.0 for value in cents]


def _normalize_day_row(values: list) -> list:
    """Cached day row: [kwh, charge, source, tier, tier_price]."""
    row = list(values)[:5]
    while len(row) < 5:
        row.append(0.0 if len(row) == 4 else "")
    return row


def _tier_price_month(
    year_month: str,
    daily_kwh: list[tuple[str, float]],
    target_charge: float,
    base_price_fallback: float | None,
) -> tuple[list[float], list[object], float, list[float]]:
    """Ladder-aware daily charges whose sum equals the official monthly bill.

    The first-tier base price is solved from the bill:
        bill = base * total_kwh + sum(tier_kwh_i * surcharge_i)
    so tier differences are real while the month still reconciles exactly.
    Returns (charges, tier_labels, solved_base, applied_prices).
    """
    thresholds = _tier_thresholds(year_month)
    per_day: list[list[tuple[int, float]]] = []
    tier_kwh = [0.0, 0.0, 0.0]
    cumulative = 0.0
    for _date, kwh in daily_kwh:
        value = max(0.0, float(kwh))
        segments = _tier_segments(cumulative, value, thresholds)
        per_day.append(segments)
        for tier, amount in segments:
            tier_kwh[tier - 1] += amount
        cumulative += value
    total_kwh = cumulative
    target = float(target_charge or 0)

    if TIER_TARIFFS and ENABLE_TIER_PRICING:
        prices = [float(p) for p in TIER_TARIFFS]
        solved_base = prices[0]
    elif ENABLE_TIER_PRICING:
        surcharges = TIER_SURCHARGES
        if target > 0 and total_kwh > 0:
            extra = sum(tier_kwh[i] * surcharges[i] for i in range(3))
            solved_base = (target - extra) / total_kwh
            if solved_base <= 0:  # 账单过低，退化为统一单价
                solved_base = target / total_kwh
                surcharges = (0.0, 0.0, 0.0)
        else:
            solved_base = float(base_price_fallback or 0)
        prices = [solved_base + surcharges[i] for i in range(3)]
    else:
        solved_base = target / total_kwh if total_kwh > 0 else float(base_price_fallback or 0)
        prices = [solved_base] * 3

    raw = [
        sum(amount * prices[tier - 1] for tier, amount in segments)
        for segments in per_day
    ]
    total_raw = sum(raw)
    if total_raw <= 1e-9 and target > 0 and raw:
        # Defensive fallback when no usable daily kWh shape exists at all.
        raw = [target / len(raw) for _ in raw]
        total_raw = sum(raw)
    if total_raw > 0 and target > 0:
        factor = target / total_raw
        charges = _apportion_cents(raw, target)
    elif solved_base > 0:
        factor = 1.0
        charges = _apportion_cents(raw, total_raw)
    else:
        factor = 0.0
        charges = [0.0 for _ in raw]

    # Full-precision price actually applied that day. Single-tier days get a
    # fixed tariff; a day crossing a threshold gets the kWh-weighted blend.
    applied_prices = []
    for segments, value in zip(per_day, raw):
        kwh = sum(amount for _tier, amount in segments)
        applied_prices.append(
            round(value * factor / kwh, 6) if kwh > 1e-9 else 0.0
        )

    labels: list[object] = []
    for segments in per_day:
        tiers = [tier for tier, amount in segments if amount > 1e-9]
        if not tiers:
            labels.append(0)
        elif len(tiers) == 1:
            labels.append(tiers[0])
        else:
            labels.append(f"{tiers[0]}-{tiers[-1]}")
    return charges, labels, solved_base, applied_prices


def _zero_bill_rows(
    year_month: str, daily_kwh: list[tuple[str, float]]
) -> tuple[list[float], list[object], float, list[float]]:
    """Rows for a 0 CNY bill: keep the kWh shape, record charge as 0.

    Prepaid credit and not-yet-settled months report ``charge=0`` in the
    official monthly bill while the daily kWh calendar is perfectly fine.
    The bill amount is the truth, so every daily charge stays at 0; the
    ladder tier is still computed so the calendar keeps its meaning.
    """
    thresholds = _tier_thresholds(year_month)
    labels: list[object] = []
    cumulative = 0.0
    for _date, kwh in daily_kwh:
        value = max(0.0, float(kwh))
        segments = _tier_segments(cumulative, value, thresholds)
        tiers = [tier for tier, amount in segments if amount > 1e-9]
        if not tiers:
            labels.append(0)
        elif len(tiers) == 1:
            labels.append(tiers[0])
        else:
            labels.append(f"{tiers[0]}-{tiers[-1]}")
        cumulative += value
    size = len(daily_kwh)
    return [0.0] * size, labels, 0.0, [0.0] * size


class CSGDailyHistory:
    """Builds, caches and serves the rolling daily cost history."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._path = hass.config.path("www", DAILY_HISTORY_FILENAME)
        self._accounts: dict[str, dict[str, list]] = {}
        self._done: dict[str, set[str]] = {}
        self._tier_base: dict[str, dict[str, float]] = {}
        self._loaded = False
        self._building = False
        self._upgrade_pending = False
        self._lock = asyncio.Lock()
        self._last_error: str | None = None

    # ------------------------------------------------------------ storage --
    def _load_sync(self) -> None:
        if self._loaded:
            return
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            upgrade = payload.get("algo_version") != ALGO_VERSION
            self._upgrade_pending = upgrade
            for account, block in (payload.get("accounts") or {}).items():
                self._accounts[account] = {
                    str(date): _normalize_day_row(values)
                    for date, values in (block.get("days") or {}).items()
                    if isinstance(values, list) and len(values) >= 3
                }
                # Older cache: keep daily kWh for reuse, but force every month
                # to be repriced. No network request is needed for that.
                self._done[account] = (
                    set() if upgrade else set(block.get("months_done") or [])
                )
                self._tier_base[account] = (
                    {} if upgrade else dict(block.get("tier_base") or {})
                )
            if upgrade:
                _LOGGER.info(
                    "Daily cost cache algorithm changed: repricing all months "
                    "with v4 zero-bill handling, reusing cached daily kWh"
                )
        except FileNotFoundError:
            pass
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.debug("Daily cost cache unreadable, rebuilding: %s", err)
        self._loaded = True

    def _save_sync(self, accounts, done, tier_base_map) -> None:
        if not WRITE_DAILY_HISTORY_JSON:
            return
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            payload = {
                "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "algo_version": ALGO_VERSION,
                "window_years": DAILY_HISTORY_YEARS,
                "keep_all_history": KEEP_ALL_HISTORY,
                "tier_pricing": ENABLE_TIER_PRICING,
                "tier_config": {
                    "summer_thresholds": list(TIER_THRESHOLDS_SUMMER),
                    "non_summer_thresholds": list(TIER_THRESHOLDS_NON_SUMMER),
                    "surcharges": list(TIER_SURCHARGES),
                    "tariffs": list(TIER_TARIFFS) if TIER_TARIFFS else None,
                },
                "unit": {"kwh": "kWh", "charge": "CNY"},
                "day_fields": ["kwh", "charge", "source", "tier", "tier_price"],
                "source_labels": SRC_LABELS,
                "note": (
                    "南网已下线每日电费接口。每日电费按当月累计电量所处阶梯档计价，"
                    "第一档基准价由官方账单反解，每月合计精确等于账单；"
                    "按分记账的尾差用最大余数法分摊，不集中于月末；"
                    "tier_price 为当天实际套用的电价，实算单价=charge/kwh 会因"
                    "按分取整有微小抖动。历史每日电量抓不到时按账单均摊。"
                    "账单电费为 0（预缴抵扣/未出账）的月份电费按 0 记账，"
                    "用电量仍取南网真实每日电量，来源标记 zb/zm。"
                ),
                "accounts": {
                    account: {
                        "days": days,
                        "months_done": sorted(done.get(account, set())),
                        "tier_base": tier_base_map.get(account, {}),
                    }
                    for account, days in accounts.items()
                },
            }
            tmp_path = self._path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_path, self._path)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("Could not write daily cost cache: %s", err)

    def earliest_cached_month(self) -> str | None:
        """Oldest month already present in the cache (for keep-all window)."""
        dates = [
            min(days) for days in self._accounts.values() if days
        ]
        if not dates:
            return None
        oldest = min(dates)[:7].replace("-", "")
        return oldest if len(oldest) >= 6 else None

    def _latest_base_price(self, account_number: str) -> float | None:
        prices = self._tier_base.get(account_number) or {}
        if not prices:
            return None
        return float(prices[max(prices)])

    # -------------------------------------------------------------- build --
    def _build_sync(
        self,
        account_number: str,
        account: CSGElectricityAccount,
        client: CSGClient,
        bills: dict[str, tuple[float, float]],
        recent: dict[str, list],
        months: list[str],
    ) -> dict:
        """Recompute the daily rows. Runs in the executor thread."""
        self._load_sync()
        days = dict(self._accounts.get(account_number, {}))
        done = set(self._done.get(account_number, set()))
        tier_base = dict(self._tier_base.get(account_number, {}))
        latest_base = self._latest_base_price(account_number)
        stats = {
            "coordinator_months": 0, "meter_months": 0,
            "estimated_months": 0, "skipped_months": 0,
            "reused_months": 0, "tier_months": 0, "days": 0,
            "zero_bill_months": 0,
        }

        # Month -> cached daily kWh. Used when upgrading a v1 cache so that
        # already-fetched real daily readings are kept (no extra API calls).
        reusable: dict[str, dict[str, tuple[float, str]]] = {}
        for date, values in days.items():
            if values[2] in (
                SRC_DAILY_METER, SRC_COORDINATOR, SRC_ZERO_BILL_DM
            ):
                # v4: a 0 CNY bill can be settled later. When those cached
                # kWh are reused under a real bill, label them as real
                # meter readings again instead of a zero-bill row.
                reused_source = (
                    SRC_DAILY_METER
                    if values[2] == SRC_ZERO_BILL_DM
                    else values[2]
                )
                reusable.setdefault(date[:4] + date[5:7], {})[date] = (
                    float(values[0]), reused_source
                )

        # 1) Recent months: keep the integration's totals, spread by ladder.
        for year_month, rows in recent.items():
            if not isinstance(rows, list) or not rows:
                continue
            parsed = []
            for row in rows:
                date = str(row.get(WF_ATTR_DATE) or "")[:10]
                if len(date) != 10:
                    continue
                try:
                    kwh = round(float(row.get(WF_ATTR_KWH) or 0), 2)
                    charge = round(float(row.get(WF_ATTR_CHARGE) or 0), 2)
                except (TypeError, ValueError):
                    continue
                parsed.append((date, kwh, charge))
            if not parsed:
                continue
            target = sum(charge for _, _, charge in parsed)
            charges, labels, base, applied = _tier_price_month(
                year_month,
                [(date, kwh) for date, kwh, _ in parsed],
                target,
                latest_base,
            )
            for (date, kwh, _), charge, tier, price in zip(
                parsed, charges, labels, applied
            ):
                days[date] = [kwh, charge, SRC_COORDINATOR, tier, price]
            if base > 0:
                tier_base[year_month] = round(base, 4)
                latest_base = base
            done.add(year_month)
            stats["coordinator_months"] += 1
            stats["tier_months"] += 1

        # 2) Older months: newest first, so a recent success is found early.
        # v4: a month cached as a 0 CNY bill must be repriced if the grid
        # later publishes a real charge for it (prepaid credit settled).
        zero_cached = {
            date[:4] + date[5:7]
            for date, values in days.items()
            if values[2] in (SRC_ZERO_BILL_DM, SRC_ZERO_BILL_MA)
        }
        pending = sorted(
            (
                ym for ym in months
                if ym not in recent
                and (
                    ym not in done
                    or (
                        ym in zero_cached
                        and (bills.get(ym) or (0, 0))[0] > 0
                    )
                )
            ),
            reverse=True,
        )
        consecutive_failures = 0
        for year_month in pending:
            bill = bills.get(year_month)
            if not bill:
                stats["skipped_months"] += 1
                continue
            # v4: charge==0 is a valid bill (prepaid credit / not settled).
            # Only a missing month is skipped; kWh must still be displayed.
            zero_bill = bill[0] <= 0
            year, month = int(year_month[:4]), int(year_month[4:])
            bill_charge, bill_kwh = bill
            day_count = calendar.monthrange(year, month)[1]
            daily_kwh = None
            source = SRC_DAILY_METER

            cached = reusable.get(year_month)
            if cached and len(cached) >= day_count:
                daily_kwh = [
                    (date, cached[date][0]) for date in sorted(cached)
                ]
                source = cached[sorted(cached)[0]][1]
                stats["reused_months"] += 1
            elif consecutive_failures < DAILY_FETCH_MAX_FAILS:
                try:
                    response = client.api_query_electricity_calender(
                        year,
                        month,
                        account.area_code,
                        account.ele_customer_id,
                        account.metering_point_id,
                        account.metering_point_number
                        or account.metering_point_id,
                    )
                    rows = (response or {}).get("result") or []
                    lookup = {}
                    for row in rows:
                        date = str(row.get("date") or "")[:10]
                        try:
                            lookup[date] = float(row.get("power") or 0)
                        except (TypeError, ValueError):
                            continue
                    if lookup:
                        daily_kwh = [
                            (
                                f"{year:04d}-{month:02d}-{day:02d}",
                                lookup.get(f"{year:04d}-{month:02d}-{day:02d}", 0.0),
                            )
                            for day in range(1, day_count + 1)
                        ]
                        consecutive_failures = 0
                        stats["meter_months"] += 1
                    else:
                        consecutive_failures += 1
                except Exception as err:  # pylint: disable=broad-except
                    consecutive_failures += 1
                    _LOGGER.debug(
                        "Daily kWh unavailable for %s-%02d: %s", year, month, err
                    )
                time.sleep(DAILY_FETCH_PAUSE)

            if daily_kwh is None:
                # No daily data: spread the official bill evenly across the month.
                daily_kwh = [
                    (
                        f"{year:04d}-{month:02d}-{day:02d}",
                        bill_kwh / day_count if bill_kwh > 0 else 0.0,
                    )
                    for day in range(1, day_count + 1)
                ]
                source = SRC_MONTHLY_AVERAGE
                stats["estimated_months"] += 1

            # Scale daily kWh so the month sums to the official billed kWh.
            # v3: some old months (e.g. 2023-04) return all-zero daily readings
            # although the bill is non-zero. Treat them as "no daily shape"
            # and spread the billed kWh evenly, instead of putting the entire
            # charge on the last day of the month.
            total = sum(value for _, value in daily_kwh)
            if total <= 1e-9 and bill_kwh > 0 and daily_kwh:
                daily_kwh = [
                    (date, bill_kwh / len(daily_kwh))
                    for date, _ in daily_kwh
                ]
                source = SRC_MONTHLY_AVERAGE
                stats["estimated_months"] += 1
            elif total > 0 and bill_kwh > 0:
                scale = bill_kwh / total
                daily_kwh = [(date, value * scale) for date, value in daily_kwh]

            if zero_bill:
                total_kwh = sum(value for _, value in daily_kwh)
                if total_kwh <= 1e-9:
                    # Neither the bill nor the calendar has any kWh for this
                    # month. Keep it empty and NOT done, so a later poll can
                    # still pick it up if the grid publishes the readings.
                    stats["skipped_months"] += 1
                    continue
                charges, labels, base, applied = _zero_bill_rows(
                    year_month, daily_kwh
                )
                source = (
                    SRC_ZERO_BILL_MA
                    if source == SRC_MONTHLY_AVERAGE
                    else SRC_ZERO_BILL_DM
                )
                stats["zero_bill_months"] += 1
            else:
                charges, labels, base, applied = _tier_price_month(
                    year_month, daily_kwh, bill_charge, latest_base
                )
            if base > 0:
                tier_base[year_month] = round(base, 4)
                latest_base = base
            for (date, value), charge, tier, price in zip(
                daily_kwh, charges, labels, applied
            ):
                days[date] = [round(value, 2), charge, source, tier, price]
            done.add(year_month)
            stats["tier_months"] += 1

        # 3) Keep-all mode never trims. Otherwise drop data older than the window.
        if not KEEP_ALL_HISTORY:
            cutoff = (
                datetime.date.today()
                - datetime.timedelta(days=365 * DAILY_HISTORY_YEARS)
            ).isoformat()
            for date in list(days):
                if date < cutoff:
                    del days[date]
                    for key in list(tier_base):
                        if key == date[:4] + date[5:7]:
                            del tier_base[key]
        stats["days"] = len(days)

        accounts = dict(self._accounts)
        accounts[account_number] = days
        done_map = dict(self._done)
        done_map[account_number] = done
        tier_map = dict(self._tier_base)
        tier_map[account_number] = tier_base
        self._save_sync(accounts, done_map, tier_map)
        return days, done, tier_base, stats

    async def async_build(
        self,
        account_number: str,
        account: CSGElectricityAccount,
        client: CSGClient,
        bills: dict[str, tuple[float, float]],
        recent: dict[str, list],
        months: list[str],
    ) -> None:
        if self._building:
            return
        async with self._lock:
            if self._building:
                return
            self._building = True
            try:
                days, done, tier_base, stats = await self._hass.async_add_executor_job(
                    self._build_sync,
                    account_number,
                    account,
                    client,
                    bills,
                    recent,
                    months,
                )
                self._accounts[account_number] = days
                self._done[account_number] = done
                self._tier_base[account_number] = tier_base
                self._last_error = None
                self._upgrade_pending = False
                _LOGGER.info(
                    "Daily cost history ready for %s: %s days, %s tier months, "
                    "%s meter months, %s reused, %s estimated",
                    redact_identifier(account_number),
                    stats["days"],
                    stats["tier_months"],
                    stats["meter_months"],
                    stats["reused_months"],
                    stats["estimated_months"],
                )
            except Exception as err:  # pylint: disable=broad-except
                self._last_error = str(err)
                _LOGGER.warning("Daily cost history build failed: %s", err)
            finally:
                self._building = False

    # -------------------------------------------------------------- query --
    async def async_query(
        self, account: str | None, start: str | None, end: str | None
    ) -> dict:
        if not self._loaded:
            await self._hass.async_add_executor_job(self._load_sync)
        today = datetime.date.today()
        end_date = (end or today.isoformat())[:10]
        start_date = (
            start
            or (today - datetime.timedelta(days=365 * DAILY_HISTORY_YEARS)).isoformat()
        )[:10]

        if not account:
            if len(self._accounts) == 1:
                account = next(iter(self._accounts))
            else:
                return {
                    "error": "account_required",
                    "available_accounts": sorted(self._accounts),
                }
        rows = []
        for date in sorted(self._accounts.get(account, {})):
            if not (start_date <= date <= end_date):
                continue
            values = self._accounts[account][date]
            kwh, charge, source = values[0], values[1], values[2]
            tier = values[3] if len(values) > 3 else ""
            try:
                tier_price = float(values[4]) if len(values) > 4 else 0.0
            except (TypeError, ValueError):
                tier_price = 0.0
            rows.append({
                "date": date,
                "kwh": kwh,
                "charge": charge,
                "source": source,
                "source_label": SRC_LABELS.get(source, source),
                "tier": tier,
                "tier_price": round(tier_price, 4) if tier_price > 0 else None,
                "unit_price": round(charge / kwh, 4) if kwh and kwh > 0 else None,
            })
        by_source: dict[str, int] = {}
        for row in rows:
            by_source[row["source"]] = by_source.get(row["source"], 0) + 1
        return {
            "account": account,
            "start": start_date,
            "end": end_date,
            "count": len(rows),
            "total_charge": round(sum(row["charge"] for row in rows), 2),
            "total_kwh": round(sum(row["kwh"] for row in rows), 2),
            "sources": by_source,
            "source_labels": SRC_LABELS,
            "keep_all_history": KEEP_ALL_HISTORY,
            "tier_pricing": ENABLE_TIER_PRICING,
            "algo_version": ALGO_VERSION,
            "building": self._building,
            "last_error": self._last_error,
            "days": rows,
        }
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    """Setup sensors from a config entry created in the integrations UI."""
    if not config_entry.data[CONF_ELE_ACCOUNTS]:
        _LOGGER.info("No ele accounts in config, exit entry setup")
        return
    coordinator = CSGCoordinator(hass, config_entry)

    all_sensors = []
    for ele_account_number, _ in config_entry.data[CONF_ELE_ACCOUNTS].items():
        sensors = [
            # balance
            CSGCostSensor(coordinator, ele_account_number, SUFFIX_BAL),
            # arrears
            CSGCostSensor(coordinator, ele_account_number, SUFFIX_ARR),
            # yesterday kwh
            CSGEnergySensor(
                coordinator,
                ele_account_number,
                SUFFIX_YESTERDAY_KWH,
            ),
            # latest day usage that is available, with extra attributes about the date
            CSGEnergySensor(
                coordinator,
                ele_account_number,
                SUFFIX_LATEST_DAY_KWH,
                extra_state_attributes_key=ATTR_KEY_LATEST_DAY_DATE,
            ),
            # latest day cost that is available, with extra attributes about the date
            CSGCostSensor(
                coordinator,
                ele_account_number,
                SUFFIX_LATEST_DAY_COST,
                extra_state_attributes_key=ATTR_KEY_LATEST_DAY_DATE,
            ),
            # this year's total energy, with extra attributes about monthly usage
            CSGEnergySensor(
                coordinator,
                ele_account_number,
                SUFFIX_THIS_YEAR_KWH,
                extra_state_attributes_key=(
                    ATTR_KEY_THIS_YEAR_BY_MONTH,
                    ATTR_KEY_HISTORY_BY_MONTH,
                ),
            ),
            # this year's total cost
            CSGCostSensor(
                coordinator,
                ele_account_number,
                SUFFIX_THIS_YEAR_COST,
            ),
            # this month's total energy, with extra attributes about daily usage
            CSGEnergySensor(
                coordinator,
                ele_account_number,
                SUFFIX_THIS_MONTH_KWH,
                extra_state_attributes_key=ATTR_KEY_THIS_MONTH_BY_DAY,
            ),
            # this month's total cost, with extra attributes about daily usage
            CSGCostSensor(
                coordinator,
                ele_account_number,
                SUFFIX_THIS_MONTH_COST,
                extra_state_attributes_key=ATTR_KEY_THIS_MONTH_BY_DAY,
            ),
            # current ladder, with extra attributes about start date
            CSGLadderStageSensor(
                coordinator,
                ele_account_number,
                SUFFIX_CURRENT_LADDER,
                extra_state_attributes_key=ATTR_KEY_CURRENT_LADDER_START_DATE,
            ),
            # current ladder remaining kwh
            CSGEnergySensor(
                coordinator, ele_account_number, SUFFIX_CURRENT_LADDER_REMAINING_KWH
            ),
            # current ladder tariff
            CSGCostSensor(
                coordinator, ele_account_number, SUFFIX_CURRENT_LADDER_TARIFF
            ),
            # last year's total energy, with extra attributes about monthly usage
            CSGEnergySensor(
                coordinator,
                ele_account_number,
                SUFFIX_LAST_YEAR_KWH,
                extra_state_attributes_key=ATTR_KEY_LAST_YEAR_BY_MONTH,
            ),
            # last year's total cost
            CSGCostSensor(
                coordinator,
                ele_account_number,
                SUFFIX_LAST_YEAR_COST,
            ),
            # last month's total energy, with extra attributes about daily usage
            CSGEnergySensor(
                coordinator,
                ele_account_number,
                SUFFIX_LAST_MONTH_KWH,
                extra_state_attributes_key=ATTR_KEY_LAST_MONTH_BY_DAY,
            ),
            # last month's total cost, with extra attributes about daily usage
            CSGCostSensor(
                coordinator,
                ele_account_number,
                SUFFIX_LAST_MONTH_COST,
                extra_state_attributes_key=ATTR_KEY_LAST_MONTH_BY_DAY,
            ),
        ]

        all_sensors.extend(sensors)

    async_add_entities(all_sensors)
    _LOGGER.debug("Created %d CSG sensors", len(all_sensors))

    async def _handle_query_daily_cost(call):
        """Service: china_southern_power_grid_stat.query_daily_cost"""
        data = call.data
        return await coordinator.daily_history.async_query(
            data.get("account"), data.get("start"), data.get("end")
        )

    register_kwargs = {}
    if SupportsResponse is not None:
        register_kwargs["supports_response"] = SupportsResponse.OPTIONAL
    hass.services.async_register(
        DOMAIN,
        SERVICE_QUERY_DAILY_COST,
        _handle_query_daily_cost,
        **register_kwargs,
    )

    # Schedule the first update to run in the background
    config_entry.async_create_task(
        hass,
        coordinator.async_config_entry_first_refresh(),
        f"{config_entry.entry_id}_first_update",
    )


class CSGBaseSensor(
    CoordinatorEntity,
    SensorEntity,
):
    """Base CSG sensor"""

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        account_number: str,
        entity_suffix: str,
        extra_state_attributes_key: str | tuple[str, ...] | None = None,
    ) -> None:
        SensorEntity.__init__(self)
        CoordinatorEntity.__init__(self, coordinator)
        self._coordinator = coordinator
        self._account_number = account_number

        self._entity_suffix = entity_suffix
        self._attr_extra_state_attributes = {}
        if isinstance(extra_state_attributes_key, str):
            self._extra_state_attributes_keys = (extra_state_attributes_key,)
        else:
            self._extra_state_attributes_keys = extra_state_attributes_key or ()

    @property
    def unique_id(self) -> str | None:
        return f"{DOMAIN}.{self._account_number}.{self._entity_suffix}"

    @property
    def name(self) -> str | None:
        return self._entity_suffix

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def _log_id(self) -> str:
        """Return a non-sensitive sensor identifier for diagnostics."""
        return self._entity_suffix

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._account_number)},
            name=f"CSGAccount-{self._account_number}",
            manufacturer="CSG",
            model="CSG Virtual Electricity Meter",
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # _LOGGER.debug(
        #     "%s coordinator update triggered",
        #     self.unique_id,
        # )

        if not self._coordinator.data:
            _LOGGER.error(
                "%s coordinator has no data",
                self._log_id,
            )
            self._attr_available = False
            self.async_write_ha_state()
            return

        account_data = self._coordinator.data.get(self._account_number)
        if account_data is None:
            _LOGGER.warning("%s not found in coordinator data", self._log_id)
            self._attr_available = False
            self.async_write_ha_state()
            return

        new_native_value = account_data.get(self._entity_suffix)
        if new_native_value is None:
            _LOGGER.warning("%s data not found in coordinator data", self._log_id)
            self._attr_available = False
            self.async_write_ha_state()
            return

        if new_native_value == STATE_UNAVAILABLE:
            _LOGGER.debug("%s data is unavailable", self._log_id)
            self.async_write_ha_state()
            self._attr_available = False
            return

        # from this point the value is available
        self._attr_available = True

        if new_native_value == STATE_UPDATE_UNCHANGED:
            # no update for this sensor, skip
            _LOGGER.debug("%s doesn't need to be updated, skip", self._log_id)
            return

        # from this point, `new_native_value` is a true value
        self._attr_native_value = new_native_value

        if self._extra_state_attributes_keys:
            new_attributes = {}
            for attribute_key in self._extra_state_attributes_keys:
                attribute_data = account_data.get(attribute_key)
                if attribute_data is None:
                    _LOGGER.warning(
                        "%s attribute %s not found in coordinator data",
                        self._log_id,
                        attribute_key,
                    )
                    continue
                new_attributes.update(attribute_data)
            self._attr_extra_state_attributes = new_attributes
        _LOGGER.debug("%s state update done", self._log_id)
        self.async_write_ha_state()


class CSGEnergySensor(CSGBaseSensor):
    """Representation of a CSG Energy Sensor."""

    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:lightning-bolt"


class CSGCostSensor(CSGBaseSensor):
    """Representation of a CSG Cost Sensor."""

    _attr_native_unit_of_measurement = "CNY"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:currency-cny"


class CSGLadderStageSensor(CSGBaseSensor):
    """Representation of a CSG Ladder Stage Sensor."""

    _attr_icon = "mdi:stairs"


class CSGCoordinator(DataUpdateCoordinator):
    """CSG custom coordinator."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize coordinator."""
        self._config_entry_id = config_entry.entry_id
        self._config = hass.config_entries.async_get_entry(self._config_entry_id).data
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            # Name of the data. For logging purposes.
            name=f"CSG Account {self._config_entry_id}",
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(
                seconds=self._config[CONF_SETTINGS][CONF_UPDATE_INTERVAL]
            ),
        )
        self._client: CSGClient | None = None
        self._if_update_last_month = True
        self._if_update_last_year = True
        self._this_day = None
        self._this_year = None
        self._this_month_ym = None
        self._last_year = None
        self._last_month_ym = None
        self._this_month_update_completed_flag = asyncio.Event()
        self._gathered_data = {}
        self._historical_months = {}
        self.daily_history = CSGDailyHistory(hass)
        self._daily_history_task = None

    async def _async_refresh_client(self):
        """Refresh the client, update the user data.
        It cannot re-login if the session is invalidated.
        """
        _LOGGER.debug("Refreshing client")
        self._client = await self.hass.async_add_executor_job(
            CSGClient.load,
            {
                CONF_AUTH_TOKEN: self._config[CONF_AUTH_TOKEN],
                CONF_API_PROFILE: self._config.get(
                    CONF_API_PROFILE,
                    api_profile_for_login_type(self._config[CONF_LOGIN_TYPE]),
                ),
            },
        )
        logged_in = await self.hass.async_add_executor_job(
            self._client.verify_login,
        )
        if not logged_in:
            _LOGGER.warning(
                "%s: Login expired",
                redact_identifier(self._config[CONF_USERNAME]),
            )
            persistent_notification.async_create(
                self.hass,
                (
                    "南网在线登录已过期，电费数据已停止更新。请前往“设置 → "
                    "设备与服务 → 南方电网电费统计”，选择“重新配置”后再次扫码。"
                ),
                title="南方电网需要重新登录",
                notification_id=auth_notification_id(self._config_entry_id),
            )
            raise ConfigEntryAuthFailed("Login expired")

        _LOGGER.debug(
            "%s: Session still valid",
            redact_identifier(self._config[CONF_USERNAME]),
        )
        await self.hass.async_add_executor_job(self._client.initialize)

    async def _async_fetch(self, func: callable, *args, **kwargs) -> (bool, tuple):
        """Wrapper to fetch data from API. Return (success, result) with timeout.
        Also handle all exceptions here to avoid task group being cancelled.
        """
        try:
            async with async_timeout.timeout(SETTING_UPDATE_TIMEOUT):
                return True, await self.hass.async_add_executor_job(
                    func, *args, **kwargs
                )

        except asyncio.TimeoutError as err:
            _LOGGER.error("Timeout fetching data in function: %s", func.__name__)
            return False, (func.__name__, err)
        except NotLoggedIn as err:
            _LOGGER.error(
                "Session invalidated unexpectedly in function: %s", func.__name__
            )
            return False, (func.__name__, err)
        except CSGAPIError as err:
            _LOGGER.error(
                "Error fetching data in coordinator: API error, function %s, %s",
                func.__name__,
                err,
            )
            return False, (func.__name__, err)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.error("Unexpected exception: %s", err)
            _LOGGER.error(traceback.format_exc())
            return False, (func.__name__, err)

    async def _async_update_bal_arr(self, account: CSGElectricityAccount):
        """Update balance and arrears"""
        success, result = await self._async_fetch(
            self._client.get_balance_and_arrears, account
        )
        if success:
            balance, arrears = result
            _LOGGER.debug(
                "Updated balance and arrears for account %s",
                redact_identifier(account.account_number),
            )
        else:
            balance, arrears = STATE_UNAVAILABLE, STATE_UNAVAILABLE
            _LOGGER.error(
                "Error updating balance and arrears for account %s: %s",
                redact_identifier(account.account_number),
                result,
            )
        self._gathered_data[account.account_number][SUFFIX_BAL] = balance
        self._gathered_data[account.account_number][SUFFIX_ARR] = arrears

    async def _async_update_yesterday_kwh(self, account: CSGElectricityAccount):
        """Update yesterday's kwh"""
        success, result = await self._async_fetch(
            self._client.get_yesterday_kwh,
            account,
        )
        if success and result is not None:
            yesterday_kwh = result
            _LOGGER.debug(
                "Updated yesterday's kwh for account %s",
                redact_identifier(account.account_number),
            )
        elif success:
            # fetch ok but csg hasn't settled yesterday's data yet (common,
            # data lags 1~2 days). not an error - the latest_day_kwh sensor
            # still reflects the most recent available day
            yesterday_kwh = STATE_UNAVAILABLE
            _LOGGER.debug(
                "Yesterday's kwh not available yet for account %s",
                redact_identifier(account.account_number),
            )
        else:
            yesterday_kwh = STATE_UNAVAILABLE
            _LOGGER.error(
                "Error updating yesterday's kwh for account %s: %s",
                redact_identifier(account.account_number),
                result,
            )
        self._gathered_data[account.account_number][
            SUFFIX_YESTERDAY_KWH
        ] = yesterday_kwh

    async def _async_update_this_year_stats(self, account: CSGElectricityAccount):
        """Update this year's data"""
        success, result = await self._async_fetch(
            self._client.get_year_month_stats, account, self._this_year
        )
        if success:
            (
                this_year_cost,
                this_year_kwh,
                this_year_by_month,
            ) = result

            _LOGGER.debug(
                "Updated this year's data for account %s",
                redact_identifier(account.account_number),
            )
        else:
            _LOGGER.error(
                "Error updating this year's data for account %s: %s",
                redact_identifier(account.account_number),
                result,
            )
            this_year_cost, this_year_kwh, this_year_by_month = (
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
            )
        self._gathered_data[account.account_number][
            SUFFIX_THIS_YEAR_KWH
        ] = this_year_kwh
        self._gathered_data[account.account_number][
            SUFFIX_THIS_YEAR_COST
        ] = this_year_cost
        self._gathered_data[account.account_number][ATTR_KEY_THIS_YEAR_BY_MONTH] = {
            ATTR_KEY_THIS_YEAR_BY_MONTH: this_year_by_month
        }

    async def _async_update_last_year_stats(self, account: CSGElectricityAccount):
        """Update last year's data"""
        if not self._if_update_last_year:
            self._gathered_data[account.account_number][
                SUFFIX_LAST_YEAR_KWH
            ] = STATE_UPDATE_UNCHANGED
            self._gathered_data[account.account_number][
                SUFFIX_LAST_YEAR_COST
            ] = STATE_UPDATE_UNCHANGED
            self._gathered_data[account.account_number][ATTR_KEY_LAST_YEAR_BY_MONTH] = {
                ATTR_KEY_LAST_YEAR_BY_MONTH: STATE_UPDATE_UNCHANGED
            }
            _LOGGER.debug(
                "Last year's data for account %s: no need to update",
                redact_identifier(account.account_number),
            )
            return
        success, result = await self._async_fetch(
            self._client.get_year_month_stats, account, self._last_year
        )
        if success:
            (
                last_year_cost,
                last_year_kwh,
                last_year_by_month,
            ) = result

            _LOGGER.debug(
                "Updated last year's data for account %s",
                redact_identifier(account.account_number),
            )
        else:
            _LOGGER.error(
                "Error updating last year's data for account %s: %s",
                redact_identifier(account.account_number),
                result,
            )
            last_year_cost, last_year_kwh, last_year_by_month = (
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
            )
        self._gathered_data[account.account_number][
            SUFFIX_LAST_YEAR_KWH
        ] = last_year_kwh
        self._gathered_data[account.account_number][
            SUFFIX_LAST_YEAR_COST
        ] = last_year_cost
        self._gathered_data[account.account_number][ATTR_KEY_LAST_YEAR_BY_MONTH] = {
            ATTR_KEY_LAST_YEAR_BY_MONTH: last_year_by_month
        }

    async def _async_update_historical_stats(self, account: CSGElectricityAccount):
        """Merge official monthly usage and cost rows for a rolling four years."""
        account_number = account.account_number
        by_month = {
            row[WF_ATTR_MONTH]: row
            for row in self._historical_months.get(account_number, [])
        }

        for attribute_key in (
            ATTR_KEY_THIS_YEAR_BY_MONTH,
            ATTR_KEY_LAST_YEAR_BY_MONTH,
        ):
            attribute_data = self._gathered_data[account_number].get(attribute_key)
            rows = attribute_data.get(attribute_key) if attribute_data else None
            if isinstance(rows, list):
                by_month.update({row[WF_ATTR_MONTH]: row for row in rows})

        oldest_year = self._this_year - HISTORY_YEAR_COUNT + 1
        older_years = list(range(oldest_year, self._last_year))
        cached_years = {int(month[:4]) for month in by_month}
        missing_years = [year for year in older_years if year not in cached_years]
        if missing_years:
            success, result = await self._async_fetch(
                self._client.get_years_month_stats,
                account,
                missing_years,
            )
            if success:
                by_month.update({row[WF_ATTR_MONTH]: row for row in result})
            else:
                _LOGGER.warning(
                    "Historical monthly data is incomplete for account %s: %s",
                    redact_identifier(account_number),
                    result,
                )

        history = [by_month[month] for month in sorted(by_month)]
        self._historical_months[account_number] = history
        self._gathered_data[account_number][ATTR_KEY_HISTORY_BY_MONTH] = {
            ATTR_KEY_HISTORY_BY_MONTH: history
        }

    async def _async_update_this_month_stats_and_ladder(
        self, account: CSGElectricityAccount
    ):
        """Update this month's usage, cost and ladder"""
        success, result = await self._async_fetch(
            self._client.get_month_daily_detail,
            account,
            self._this_month_ym,
        )
        if success:
            (
                this_month_cost,
                this_month_kwh,
                ladder,
                this_month_by_day,
            ) = result
            if this_month_cost is None:
                this_month_cost = STATE_UNAVAILABLE
            if this_month_kwh is None:
                this_month_kwh = STATE_UNAVAILABLE
            ladder_stage = (
                ladder[WF_ATTR_LADDER]
                if ladder[WF_ATTR_LADDER] is not None
                else STATE_UNAVAILABLE
            )
            ladder_remaining_kwh = (
                ladder[WF_ATTR_LADDER_REMAINING_KWH]
                if ladder[WF_ATTR_LADDER_REMAINING_KWH] is not None
                else STATE_UNAVAILABLE
            )
            ladder_tariff = (
                ladder[WF_ATTR_LADDER_TARIFF]
                if ladder[WF_ATTR_LADDER_TARIFF] is not None
                else STATE_UNAVAILABLE
            )
            ladder_start_date = (
                ladder[WF_ATTR_LADDER_START_DATE]
                if ladder[WF_ATTR_LADDER_START_DATE] is not None
                else STATE_UNAVAILABLE
            )
        else:
            (
                this_month_cost,
                this_month_kwh,
                this_month_by_day,
                ladder_stage,
                ladder_remaining_kwh,
                ladder_tariff,
                ladder_start_date,
            ) = (
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
            )

        if not this_month_by_day or this_month_by_day == STATE_UNAVAILABLE:
            # need last month's data to update `latest_day` entity
            self._if_update_last_month = True

        self._gathered_data[account.account_number][
            SUFFIX_THIS_MONTH_KWH
        ] = this_month_kwh
        self._gathered_data[account.account_number][
            SUFFIX_THIS_MONTH_COST
        ] = this_month_cost
        self._gathered_data[account.account_number][ATTR_KEY_THIS_MONTH_BY_DAY] = {
            ATTR_KEY_THIS_MONTH_BY_DAY: this_month_by_day
        }
        self._gathered_data[account.account_number][
            SUFFIX_CURRENT_LADDER
        ] = ladder_stage
        self._gathered_data[account.account_number][
            SUFFIX_CURRENT_LADDER_REMAINING_KWH
        ] = ladder_remaining_kwh
        self._gathered_data[account.account_number][
            SUFFIX_CURRENT_LADDER_TARIFF
        ] = ladder_tariff
        self._gathered_data[account.account_number][
            ATTR_KEY_CURRENT_LADDER_START_DATE
        ] = {ATTR_KEY_CURRENT_LADDER_START_DATE: ladder_start_date}

        self._this_month_update_completed_flag.set()

    async def _async_update_last_month_stats(self, account: CSGElectricityAccount):
        """Update last month's usage and cost"""
        if not self._if_update_last_month:
            # original condition, don't need to update last month's data

            # wait for this month's data to be updated to see if last month's data is needed
            await self._this_month_update_completed_flag.wait()

            if not self._if_update_last_month:
                # don't need last month's data for latest day
                _LOGGER.debug(
                    "Last month's data for account %s: no need to update",
                    redact_identifier(account.account_number),
                )
                self._gathered_data[account.account_number][
                    SUFFIX_LAST_MONTH_KWH
                ] = STATE_UPDATE_UNCHANGED
                self._gathered_data[account.account_number][
                    SUFFIX_LAST_MONTH_COST
                ] = STATE_UPDATE_UNCHANGED
                self._gathered_data[account.account_number][
                    ATTR_KEY_LAST_MONTH_BY_DAY
                ] = {ATTR_KEY_LAST_MONTH_BY_DAY: STATE_UPDATE_UNCHANGED}
                return

        # continue to update last month's data through the maintained combined API
        success, result = await self._async_fetch(
            self._client.get_month_daily_detail,
            account,
            self._last_month_ym,
        )
        if success:
            (
                last_month_cost,
                last_month_kwh,
                _,  # ladder is discarded
                last_month_by_day,
            ) = result
            if last_month_cost is None:
                last_month_cost = STATE_UNAVAILABLE
            if last_month_kwh is None:
                last_month_kwh = STATE_UNAVAILABLE
        else:
            (
                last_month_cost,
                last_month_kwh,
                last_month_by_day,
            ) = (
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
                STATE_UNAVAILABLE,
            )

        self._gathered_data[account.account_number][
            SUFFIX_LAST_MONTH_KWH
        ] = last_month_kwh
        self._gathered_data[account.account_number][
            SUFFIX_LAST_MONTH_COST
        ] = last_month_cost
        self._gathered_data[account.account_number][ATTR_KEY_LAST_MONTH_BY_DAY] = {
            ATTR_KEY_LAST_MONTH_BY_DAY: last_month_by_day
        }

    def _update_latest_day(self, account: CSGElectricityAccount):
        this_month_by_day = self._gathered_data[account.account_number][
            ATTR_KEY_THIS_MONTH_BY_DAY
        ][ATTR_KEY_THIS_MONTH_BY_DAY]
        last_month_by_day = self._gathered_data[account.account_number][
            ATTR_KEY_LAST_MONTH_BY_DAY
        ][ATTR_KEY_LAST_MONTH_BY_DAY]

        if (
            this_month_by_day == STATE_UNAVAILABLE
            and last_month_by_day == STATE_UNAVAILABLE
        ):
            latest_day_kwh = STATE_UNAVAILABLE
            latest_day_cost = STATE_UNAVAILABLE
            latest_day_date = STATE_UNAVAILABLE
        else:
            if this_month_by_day != STATE_UNAVAILABLE and len(this_month_by_day) >= 1:
                # we have this month's data, use the latest day
                latest_day_kwh = this_month_by_day[-1][WF_ATTR_KWH]
                latest_day_cost = (
                    this_month_by_day[-1].get(WF_ATTR_CHARGE) or STATE_UNAVAILABLE
                )
                latest_day_date = this_month_by_day[-1][WF_ATTR_DATE]
            else:
                # this month isn't available yet (typically during the first 3 days)
                # let's try last month
                if (
                    last_month_by_day
                    not in [
                        STATE_UNAVAILABLE,
                        STATE_UPDATE_UNCHANGED,
                    ]
                    and len(last_month_by_day) >= 1
                ):
                    latest_day_kwh = last_month_by_day[-1][WF_ATTR_KWH]
                    latest_day_cost = STATE_UNAVAILABLE
                    latest_day_date = last_month_by_day[-1][WF_ATTR_DATE]
                else:
                    _LOGGER.error(
                        "Ele account %s, no latest day data available",
                        redact_identifier(account.account_number),
                    )
                    latest_day_kwh = STATE_UNAVAILABLE
                    latest_day_cost = STATE_UNAVAILABLE
                    latest_day_date = STATE_UNAVAILABLE
        self._gathered_data[account.account_number][
            SUFFIX_LATEST_DAY_KWH
        ] = latest_day_kwh
        self._gathered_data[account.account_number][
            SUFFIX_LATEST_DAY_COST
        ] = latest_day_cost
        self._gathered_data[account.account_number][ATTR_KEY_LATEST_DAY_DATE] = {
            ATTR_KEY_LATEST_DAY_DATE: latest_day_date
        }

    def _update_states(self):
        current_dt = datetime.datetime.now()
        this_year, this_month, this_day = (
            current_dt.year,
            current_dt.month,
            current_dt.day,
        )
        last_year, last_month = this_year - 1, this_month - 1
        if last_month == 0:
            last_month_ym = (last_year, 12)
        else:
            last_month_ym = (this_year, last_month)
        self._this_day = this_day
        self._this_year = this_year
        self._this_month_ym = (this_year, this_month)
        self._last_year = last_year
        self._last_month_ym = last_month_ym

        # for last month and last year data, they won't change over a long period of time
        # so we could use cache
        #
        # update policy for last month:
        # for the first <LAST_MONTH_UPDATE_DAY_THRESHOLD> days of a month,
        # update every `update_interval`.
        # for the rest of the time, do not update.

        # update policy for last year:
        # for the first <LAST_YEAR_UPDATE_DAY_THRESHOLD> days of Jan, update daily at first update
        # for the rest of the time, do not update
        #
        # when integration is reloaded, all updates will be triggered
        # so user could just reload the integration to refresh the data if needed

        if (
            self.hass.data[DOMAIN][self._config_entry_id].get(DATA_KEY_LAST_UPDATE_DAY)
            is None
        ):
            # first update
            update_last_month = True
            update_last_year = True
            _LOGGER.debug(
                "First update for account %s, getting all past data",
                redact_identifier(self._config[CONF_USERNAME]),
            )
        else:
            update_last_month = False
            update_last_year = False

            if this_day <= SETTING_LAST_MONTH_UPDATE_DAY_THRESHOLD:
                update_last_month = True
            today_first_update_triggered = (
                self.hass.data[DOMAIN][self._config_entry_id][DATA_KEY_LAST_UPDATE_DAY]
                == this_day
            )
            if this_month == 1 and this_day <= SETTING_LAST_YEAR_UPDATE_DAY_THRESHOLD:
                if not today_first_update_triggered:
                    update_last_year = True
        self._if_update_last_month = update_last_month
        self._if_update_last_year = update_last_year

    async def _async_update_account_data(self, account: CSGElectricityAccount):
        start_time = time.time()
        # TODO use asyncio.TaskGroup() in 3.11

        # async with asyncio.TaskGroup() as task_group:
        #     task_group.create_task(self._async_update_bal_arr(account))
        #     task_group.create_task(self._async_update_yesterday_kwh(account))
        #     task_group.create_task(self._async_update_this_year_stats(account))
        #     task_group.create_task(self._async_update_last_year_stats(account))
        #     task_group.create_task(
        #         self._async_update_this_month_stats_and_ladder(account)
        #     )
        #     task_group.create_task(self._async_update_last_month_stats(account))
        await asyncio.gather(
            self._async_update_bal_arr(account),
            self._async_update_yesterday_kwh(account),
            self._async_update_this_year_stats(account),
            self._async_update_last_year_stats(account),
            self._async_update_this_month_stats_and_ladder(account),
            self._async_update_last_month_stats(account),
            return_exceptions=True,
        )
        await self._async_update_historical_stats(account)
        try:
            self._update_latest_day(account)
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.error(
                "Ele account %s, update latest day data failed: %s",
                redact_identifier(account.account_number),
                exc,
            )

        _LOGGER.debug(
            "Ele account %s, update took %s seconds",
            redact_identifier(account.account_number),
            time.time() - start_time,
        )

    def _bills_lookup(self, account_number: str) -> dict[str, tuple[float, float]]:
        """Official monthly bills already gathered by the integration."""
        bills = {}
        for row in self._historical_months.get(account_number, []):
            year_month = _normalize_ym(row.get(WF_ATTR_MONTH))
            if not year_month:
                continue
            try:
                charge = float(row.get(WF_ATTR_CHARGE) or 0)
                kwh = float(row.get(WF_ATTR_KWH) or 0)
            except (TypeError, ValueError):
                continue
            # v4: keep zero-charge rows too. Prepaid credit / unsettled bills
            # report charge=0 while kWh is still real (e.g. 2026-02: 0 CNY /
            # 201 kWh). Filtering on charge>0 used to hide those months.
            bills[year_month] = (charge, kwh)
        return bills

    def _recent_days_lookup(self, account_number: str) -> dict[str, list]:
        """This/last month by-day rows (already contain estimated charge)."""
        result = {}
        data = self._gathered_data.get(account_number, {})
        pairs = (
            (ATTR_KEY_THIS_MONTH_BY_DAY, self._this_month_ym),
            (ATTR_KEY_LAST_MONTH_BY_DAY, self._last_month_ym),
        )
        for key, year_month in pairs:
            block = data.get(key) or {}
            rows = block.get(key) if isinstance(block, dict) else None
            if isinstance(rows, list) and rows and year_month:
                year, month = year_month
                result[f"{year:04d}{month:02d}"] = rows
        return result

    async def _async_update_daily_history(self) -> None:
        """Refresh the daily cost history in the background."""
        if self._client is None:
            return
        bills_by_account = {
            number: self._bills_lookup(number)
            for number in self._config[CONF_ELE_ACCOUNTS]
        }
        earliest = None
        if KEEP_ALL_HISTORY:
            candidates = [
                min(bills) for bills in bills_by_account.values() if bills
            ]
            cached = self.daily_history.earliest_cached_month()
            if cached:
                candidates.append(cached)
            if candidates:
                earliest = min(candidates)
        months = _target_months(earliest)
        for account_number, account_data in self._config[CONF_ELE_ACCOUNTS].items():
            account = CSGElectricityAccount.load(account_data)
            await self.daily_history.async_build(
                account_number,
                account,
                self._client,
                bills_by_account[account_number],
                self._recent_days_lookup(account_number),
                months,
            )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        self.update_interval = timedelta(
            seconds=self._config[CONF_SETTINGS][CONF_UPDATE_INTERVAL]
        )
        self._update_states()
        # _LOGGER.debug("Coordinator update interval: %d", self.update_interval.seconds)
        _LOGGER.debug("Coordinator update started")
        start_time = time.time()

        metering_point_data = {}
        config_entry_need_update = False
        await self._async_refresh_client()
        new_config = dict(self._config)
        new_accounts = dict(new_config[CONF_ELE_ACCOUNTS])
        new_config[CONF_ELE_ACCOUNTS] = new_accounts
        for account_number, account_data in self._config[CONF_ELE_ACCOUNTS].items():
            self._gathered_data[account_number] = {}
            account = CSGElectricityAccount.load(account_data)
            # handling the addition of metering point number
            if not account.metering_point_number:
                if not metering_point_data:
                    ok, data = await self._async_fetch(
                        self._client.api_get_metering_point,
                        account.area_code,
                        account.ele_customer_id,
                    )
                    if ok:
                        metering_point_data = data
                if metering_point_data:
                    for mp in metering_point_data:
                        if mp["eleCustNumber"] == account.account_number:
                            config_entry_need_update = True
                            account.metering_point_number = mp[
                                JSON_KEY_METERING_POINT_NUMBER
                            ]
                            new_accounts[account_number] = account.dump()
                            break

            await self._async_update_account_data(account)
        if config_entry_need_update:
            new_config[CONF_UPDATED_AT] = str(int(time.time() * 1000))
            self.hass.config_entries.async_update_entry(
                self.hass.config_entries.async_get_entry(self._config_entry_id),
                data=new_config,
            )
            _LOGGER.debug("Updated accounts with metering point number")
        _LOGGER.debug("Coordinator update took %s seconds", time.time() - start_time)
        self.hass.data[DOMAIN][self._config_entry_id][
            DATA_KEY_LAST_UPDATE_DAY
        ] = self._this_day

        # Build/refresh the rolling 3-year daily cost history in the background
        # so it never slows down the normal sensor update.
        if self._daily_history_task is None or self._daily_history_task.done():
            self._daily_history_task = self.hass.async_create_task(
                self._async_update_daily_history(),
                f"{self._config_entry_id}_daily_cost_history",
            )
        return self._gathered_data
