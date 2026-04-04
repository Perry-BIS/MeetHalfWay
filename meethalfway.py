"""
MeetHalfway AI — v2.0 (竞赛进化版)

算法 : 等时线交集 (Mapbox Isochrone API) + 时间公平性指数惩罚
AI   : GPT-4o-mini 结构化 JSON 语义提取（替代硬编码关键词）
工程 : asyncio + httpx 并发 · 优雅降级 (Mapbox->OSM / OpenAI->关键词) · logging
演示 : folium 交互地图 · 惊喜 (Surprise Me) 模式 · 疲劳度参数
隐私 : 零足迹设计——用户 GPS 仅在内存中计算，函数返回后立即销毁，不做任何持久化。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx
import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# 可选重型依赖 — 缺失时优雅降级，不中断程序
# ---------------------------------------------------------------------------
try:
    from shapely.geometry import Point, Polygon, mapping, shape  # type: ignore
    from shapely.ops import unary_union  # type: ignore
    HAS_SHAPELY = True
except ImportError:
    HAS_SHAPELY = False

try:
    import folium  # type: ignore
    HAS_FOLIUM = True
except ImportError:
    HAS_FOLIUM = False

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
logger = logging.getLogger("meethalfway")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
MAPBOX_GEOCODE_BASE = "https://api.mapbox.com/geocoding/v5/mapbox.places"
MAPBOX_ISOCHRONE_BASE = "https://api.mapbox.com/isochrone/v1/mapbox"
ORS_ISOCHRONE_BASE = "https://api.openrouteservice.org/v2/isochrones"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
YELP_SEARCH_URL = "https://api.yelp.com/v3/businesses/search"
OVERPASS_API_URL = "https://overpass-api.de/api/interpreter"

# Mapbox isochrone profile 映射
_PROFILE_MAP: Dict[str, str] = {
    "drive": "driving",
    "walk": "walking",
    "transit": "driving",  # Mapbox 暂不支持公共交通等时线，driving 作为近似
}

# 估算速度（km/min）
_SPEED_KM_MIN: Dict[str, float] = {
    "walk": 5.0 / 60,
    "drive": 40.0 / 60,
    "transit": 20.0 / 60,
}

_COMMUTE_BIAS_BASE: Dict[str, float] = {
    "walk": 1.35,
    "transit": 1.15,
    "drive": 0.85,
}


def normalize_transport_mode(mode: Optional[str]) -> str:
    raw = str(mode or "").strip().lower()
    aliases = {
        "walking": "walk",
        "walk": "walk",
        "on foot": "walk",
        "foot": "walk",
        "public transit": "transit",
        "transit": "transit",
        "bus": "transit",
        "train": "transit",
        "subway": "transit",
        "metro": "transit",
        "driving": "drive",
        "drive": "drive",
        "car": "drive",
    }
    return aliases.get(raw, "transit")


def compute_commute_bias_weights(
    transport_a: Optional[str],
    transport_b: Optional[str],
    tolerance_a_miles: Optional[float] = None,
    tolerance_b_miles: Optional[float] = None,
) -> Tuple[float, float]:
    def _weight(mode: Optional[str], miles: Optional[float]) -> float:
        normalized_mode = normalize_transport_mode(mode)
        base = _COMMUTE_BIAS_BASE.get(normalized_mode, _COMMUTE_BIAS_BASE["transit"])
        if miles is None:
            return base
        miles_value = max(1.0, float(miles))
        tolerance_factor = min(1.35, max(0.8, 15.0 / miles_value))
        return base * tolerance_factor

    return _weight(transport_a, tolerance_a_miles), _weight(transport_b, tolerance_b_miles)


# ---------------------------------------------------------------------------
# 场所类型 & 场景配置（供 CLI / Streamlit 两端共享）
# ---------------------------------------------------------------------------
VENUE_TYPES: Dict[str, Dict[str, str]] = {
    "restaurant":  {"display": "餐厅",         "query": "restaurant",                    "icon": "cutlery"},
    "cafe":        {"display": "咖啡店",         "query": "cafe coffee",                   "icon": "coffee"},
    "park":        {"display": "公园",           "query": "park",                          "icon": "tree"},
    "mall":        {"display": "商场/购物中心",   "query": "shopping mall",                 "icon": "shopping-bag"},
    "cinema":      {"display": "电影院",         "query": "cinema movie theater",          "icon": "film"},
    "bar":         {"display": "酒吧/酒馆",       "query": "bar pub lounge",                "icon": "glass"},
    "bookstore":   {"display": "书店",           "query": "bookstore library",             "icon": "book"},
    "gas_station": {"display": "加油站/便利店",   "query": "gas station convenience store", "icon": "road"},
    "sports":      {"display": "运动/健身",       "query": "gym sports center stadium",     "icon": "futbol-o"},
    "museum":      {"display": "博物馆/展览",     "query": "museum gallery exhibition",     "icon": "university"},
}

_OVERPASS_FILTERS: Dict[str, List[str]] = {
    "restaurant": ['nwr["amenity"="restaurant"]'],
    "cafe": ['nwr["amenity"="cafe"]'],
    "park": ['nwr["leisure"="park"]'],
    "mall": ['nwr["shop"="mall"]', 'nwr["building"="retail"]'],
    "cinema": ['nwr["amenity"="cinema"]'],
    "bar": ['nwr["amenity"~"^(bar|pub)$"]'],
    "bookstore": ['nwr["shop"="books"]'],
    "gas_station": ['nwr["amenity"="fuel"]', 'nwr["shop"="convenience"]'],
    "sports": ['nwr["leisure"~"^(fitness_centre|sports_centre|stadium)$"]'],
    "museum": ['nwr["tourism"~"^(museum|gallery)$"]'],
}

# 场景预设：影响默认隐私模式 & 推荐场所类型排序
MEET_SCENARIOS: Dict[str, Dict] = {
    "blind_date": {
        "display": "相亲 / 初次见面",
        "desc": "两人不方便共享位置，推荐公开安全、人流适中的场所。",
        "default_mode": "隐私分离上传",
        "venue_types": ["cafe", "restaurant", "park", "bookstore"],
    },
    "couple": {
        "display": "情侣约会",
        "desc": "寻找浪漫、适合双人放松的约会地点。",
        "default_mode": "地址输入",
        "venue_types": ["restaurant", "cinema", "park", "cafe", "bar"],
    },
    "friends": {
        "display": "朋友聚会",
        "desc": "多人聚餐或休闲活动的最优集合点。",
        "default_mode": "地址输入",
        "venue_types": ["restaurant", "bar", "sports", "mall", "cinema"],
    },
    "business": {
        "display": "商务会面",
        "desc": "专业、安静、中立的见面场所。",
        "default_mode": "隐私分离上传",
        "venue_types": ["cafe", "restaurant", "mall"],
    },
}


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class Location:
    lat: float
    lon: float


@dataclass
class CandidateRestaurant:
    name: str
    lat: float
    lon: float
    place_name: str
    mapbox_relevance: float
    distance_to_center_km: float
    fairness_delta_km: float = 0.0
    fairness_delta_minutes: float = 0.0       # 新增：时间公平差（分钟）
    in_isochrone_intersection: bool = False    # 新增：是否在等时线交集内
    rating_proxy: float = 0.5
    web_signals: Dict[str, Any] = field(default_factory=dict)
    final_score: float = 0.0
    venue_category: str = "restaurant"         # 场所类型键，与 VENUE_TYPES 对应
    best_time_slot: str = ""
    availability_overlap: float = 0.0
    radius_tolerance_score: float = 0.0
    venue_popularity_score: float = 0.0
    mutual_vote_score: float = 0.0
    time_vote_score: float = 0.0
    search_area_mode: str = "intersection"
    time_conflict: bool = False
    score_breakdown: Dict[str, float] = field(default_factory=dict)


def _clip01(x: float) -> float:
    return min(max(x, 0.0), 1.0)


# ---------------------------------------------------------------------------
# 推荐引擎
# ---------------------------------------------------------------------------
class MeetHalfwayRecommender:
    def __init__(
        self,
        mapbox_token: str,
        ors_api_key: Optional[str],
        yelp_api_key: Optional[str],
        tavily_key: str,
        openai_key: Optional[str],
        openai_model: str,
        openai_base: Optional[str] = None,
        transport: str = "transit",
        isochrone_minutes: int = 20,
        low_cost_mode: bool = False,
        use_yelp: bool = True,
        use_llm_extraction: bool = True,
        use_llm_summary: bool = True,
        max_enriched_candidates: Optional[int] = None,
    ) -> None:
        self.mapbox_token = mapbox_token
        self.ors_api_key = ors_api_key
        self.yelp_api_key = yelp_api_key
        self.tavily_key = tavily_key
        self.openai_key = openai_key
        self.openai_model = openai_model
        self.openai_base = openai_base
        self.transport = transport
        self.isochrone_minutes = isochrone_minutes
        self.low_cost_mode = low_cost_mode
        self.use_yelp = use_yelp and bool(yelp_api_key)
        self.use_llm_extraction = use_llm_extraction
        self.use_llm_summary = use_llm_summary
        # 降级标志：首次失败后切换备选方案
        self._mapbox_ok: bool = True
        self._openai_ok: bool = bool(openai_key)
        self._http_max_retries: int = 2 if low_cost_mode else 4
        self._retry_base_seconds: float = 0.35 if low_cost_mode else 0.6
        self._retry_jitter_seconds: float = 0.2 if low_cost_mode else 0.35
        self._yelp_sem = asyncio.Semaphore(2 if low_cost_mode else 3)
        self._tavily_search_depth = "basic" if low_cost_mode else "advanced"
        self._tavily_max_results = 3 if low_cost_mode else 5
        if max_enriched_candidates is not None:
            self.max_enriched_candidates = max(1, int(max_enriched_candidates))
        elif low_cost_mode:
            self.max_enriched_candidates = 3
        else:
            self.max_enriched_candidates = None
        self._async_openai_client: Optional[Any] = None
        self._sync_openai_client: Optional[Any] = None

    def recommend_search_limit(self, top_k: int) -> int:
        base = max(1, int(top_k))
        if self.low_cost_mode:
            return min(max(base + 1, 3), 4)
        return max(base * 2, 10)

    def _get_async_openai_client(self) -> Optional[Any]:
        if not self.openai_key or not self._openai_ok:
            return None
        if self._async_openai_client is None:
            import openai as _openai  # noqa: PLC0415

            self._async_openai_client = _openai.AsyncOpenAI(
                api_key=self.openai_key,
                base_url=self.openai_base,
            )
        return self._async_openai_client

    def _get_sync_openai_client(self) -> Optional[Any]:
        if not self.openai_key or not self._openai_ok:
            return None
        if self._sync_openai_client is None:
            from openai import OpenAI  # noqa: PLC0415

            self._sync_openai_client = OpenAI(
                api_key=self.openai_key,
                base_url=self.openai_base,
            )
        return self._sync_openai_client

    def _backoff_seconds(self, attempt: int) -> float:
        return self._retry_base_seconds * (2 ** attempt) + random.uniform(0.0, self._retry_jitter_seconds)

    def _post_overpass_with_retry(self, query: str, timeout: int = 25) -> Optional[Dict[str, Any]]:
        """Overpass 请求带指数退避，减少公共节点 429 影响。"""
        for attempt in range(self._http_max_retries):
            try:
                resp = requests.post(OVERPASS_API_URL, data={"data": query}, timeout=timeout)
                if resp.status_code == 429 and attempt < self._http_max_retries - 1:
                    delay = self._backoff_seconds(attempt)
                    logger.warning("Overpass 触发 429，%.2fs 后重试（%d/%d）", delay, attempt + 1, self._http_max_retries)
                    time.sleep(delay)
                    continue
                if resp.status_code >= 500 and attempt < self._http_max_retries - 1:
                    delay = self._backoff_seconds(attempt)
                    logger.warning(
                        "Overpass 服务异常 %s，%.2fs 后重试（%d/%d）",
                        resp.status_code,
                        delay,
                        attempt + 1,
                        self._http_max_retries,
                    )
                    time.sleep(delay)
                    continue
                resp.raise_for_status()
                return resp.json()
            except Exception as exc:
                if attempt < self._http_max_retries - 1:
                    delay = self._backoff_seconds(attempt)
                    logger.warning("Overpass 请求异常: %s，%.2fs 后重试（%d/%d）", exc, delay, attempt + 1, self._http_max_retries)
                    time.sleep(delay)
                    continue
                logger.warning("Overpass 请求最终失败: %s", exc)
                return None
        return None

    @staticmethod
    def _normalize_vote(v: Any) -> float:
        """将投票值归一化到 [0,1]。支持 [-2,2]、[0,2]、[0,1]。"""
        try:
            fv = float(v)
        except Exception:
            return 0.5
        if fv < 0:
            return _clip01((fv + 2.0) / 4.0)
        if fv <= 1.0:
            return _clip01(fv)
        return _clip01(fv / 2.0)

    def _time_negotiation_for_candidate(
        self,
        candidate: CandidateRestaurant,
        a: Location,
        b: Location,
        time_slots: List[str],
        availability: Optional[Dict[str, List[str]]],
        time_votes: Optional[Dict[str, Dict[str, float]]],
        radius_tolerance: Optional[Dict[str, float]],
        a_fatigue: float,
        b_fatigue: float,
        time_conflict: bool = False,
    ) -> Tuple[str, float, float, float]:
        """
        对单个候选场所进行“时间协商”打分：
        返回 (最佳时间段, 可用性重叠分, 半径容忍分, 时间投票分)。
        """
        if not time_slots:
            return "", 0.5, 0.5, 0.5

        loc = Location(candidate.lat, candidate.lon)
        ta = self._travel_minutes(a, loc) * a_fatigue
        tb = self._travel_minutes(b, loc) * b_fatigue

        tol_a = float((radius_tolerance or {}).get("a", self.isochrone_minutes) or self.isochrone_minutes)
        tol_b = float((radius_tolerance or {}).get("b", self.isochrone_minutes) or self.isochrone_minutes)
        tol_a = max(tol_a, 5.0)
        tol_b = max(tol_b, 5.0)

        a_avail = set((availability or {}).get("a", time_slots))
        b_avail = set((availability or {}).get("b", time_slots))

        if time_conflict:
            radius_a = _clip01(1.0 - max(0.0, ta - tol_a) / tol_a)
            radius_b = _clip01(1.0 - max(0.0, tb - tol_b) / tol_b)
            radius_score = (radius_a + radius_b) / 2.0
            return "No shared time available", 0.0, radius_score, 0.0

        best_slot = time_slots[0]
        best_score = -1.0
        best_overlap = 0.0
        best_time_vote = 0.5

        for slot in time_slots:
            a_ok = slot in a_avail
            b_ok = slot in b_avail
            if a_ok and b_ok:
                overlap = 1.0
            elif a_ok or b_ok:
                overlap = 0.35
            else:
                overlap = 0.0

            va = self._normalize_vote((time_votes or {}).get("a", {}).get(slot, 1.0))
            vb = self._normalize_vote((time_votes or {}).get("b", {}).get(slot, 1.0))
            agreement_score = 1.0 - abs(va - vb)
            vote_score = 0.7 * ((va + vb) / 2.0) + 0.3 * agreement_score

            slot_score = 0.65 * overlap + 0.35 * vote_score
            if slot_score > best_score:
                best_score = slot_score
                best_slot = slot
                best_overlap = overlap
                best_time_vote = vote_score

        # 行程时间超过个人容忍半径（分钟）时，分数线性下降
        radius_a = _clip01(1.0 - max(0.0, ta - tol_a) / tol_a)
        radius_b = _clip01(1.0 - max(0.0, tb - tol_b) / tol_b)
        radius_score = (radius_a + radius_b) / 2.0
        return best_slot, best_overlap, radius_score, best_time_vote

    def _place_vote_for_candidate(
        self,
        candidate: CandidateRestaurant,
        place_votes: Optional[Dict[str, Dict[str, float]]],
    ) -> float:
        """计算双方对场所的互选偏好分，支持按类型与按名称投票。"""
        if not place_votes:
            return 0.5

        a_votes = place_votes.get("a", {})
        b_votes = place_votes.get("b", {})
        keys = [candidate.venue_category, candidate.name.lower()]

        def _pick(votes: Dict[str, float]) -> float:
            for k in keys:
                if k in votes:
                    return self._normalize_vote(votes[k])
            return 0.5

        a_score = _pick(a_votes)
        b_score = _pick(b_votes)
        mean_score = (a_score + b_score) / 2.0
        agreement_score = 1.0 - abs(a_score - b_score)
        return 0.65 * mean_score + 0.35 * agreement_score

    # -----------------------------------------------------------------------
    # 几何工具
    # -----------------------------------------------------------------------
    @staticmethod
    def haversine_km(a: Location, b: Location) -> float:
        """Haversine 公式计算两点球面距离（km）。"""
        r = 6371.0
        lat1, lon1 = math.radians(a.lat), math.radians(a.lon)
        lat2, lon2 = math.radians(b.lat), math.radians(b.lon)
        dlat, dlon = lat2 - lat1, lon2 - lon1
        x = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return r * 2 * math.atan2(math.sqrt(x), math.sqrt(1 - x))

    def compute_weighted_midpoint(
        self, a: Location, b: Location, weight_a: float = 1.0, weight_b: float = 1.0
    ) -> Location:
        total = weight_a + weight_b
        return Location(
            lat=(a.lat * weight_a + b.lat * weight_b) / total,
            lon=(a.lon * weight_a + b.lon * weight_b) / total,
        )

    def _travel_minutes(self, a: Location, b: Location) -> float:
        """根据交通方式估算行程时间（分钟）。"""
        dist_km = self.haversine_km(a, b)
        speed = _SPEED_KM_MIN.get(self.transport, _SPEED_KM_MIN["transit"])
        return dist_km / speed

    # -----------------------------------------------------------------------
    # 等时线（Isochrone）— Mapbox API + 圆形近似降级
    # -----------------------------------------------------------------------
    def _fetch_isochrone(self, loc: Location, minutes: int, profile: str) -> Optional[Any]:
        """调用 Mapbox Isochrone API，返回 shapely Polygon。"""
        if not self.mapbox_token:
            return None
        if not HAS_SHAPELY:
            logger.warning("shapely 未安装 — 等时线功能已降级为半径近似圆。")
            return None
        url = f"{MAPBOX_ISOCHRONE_BASE}/{profile}/{loc.lon},{loc.lat}"
        params = {
            "contours_minutes": str(minutes),
            "polygons": "true",
            "access_token": self.mapbox_token,
        }
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            features = resp.json().get("features", [])
            if not features:
                logger.warning("Isochrone API 返回空要素 (%.4f, %.4f)。", loc.lat, loc.lon)
                return None
            poly = shape(features[0]["geometry"])
            logger.info(
                "等时线获取成功 (%.4f, %.4f) | %d 分钟 | profile=%s",
                loc.lat, loc.lon, minutes, profile,
            )
            return poly
        except Exception as exc:
            logger.warning("Mapbox Isochrone 请求失败: %s — 尝试半径近似降级", exc)
            self._mapbox_ok = False
            return None

    def _fetch_isochrone_ors(self, loc: Location, minutes: int, transport: str) -> Optional[Any]:
        """调用 OpenRouteService Isochrone API，返回 shapely Polygon。"""
        if not self.ors_api_key or not HAS_SHAPELY:
            return None

        profile_map = {
            "drive": "driving-car",
            "walk": "foot-walking",
            "transit": "driving-car",
        }
        profile = profile_map.get(transport, "driving-car")
        url = f"{ORS_ISOCHRONE_BASE}/{profile}"
        payload = {
            "locations": [[loc.lon, loc.lat]],
            "range": [int(minutes) * 60],
            "range_type": "time",
        }
        headers = {
            "Authorization": self.ors_api_key,
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
            resp.raise_for_status()
            features = resp.json().get("features", [])
            if not features:
                logger.warning("ORS Isochrone 返回空要素 (%.4f, %.4f)。", loc.lat, loc.lon)
                return None
            poly = shape(features[0]["geometry"])
            logger.info(
                "ORS 等时线获取成功 (%.4f, %.4f) | %d 分钟 | profile=%s",
                loc.lat,
                loc.lon,
                minutes,
                profile,
            )
            return poly
        except Exception as exc:
            logger.warning("ORS Isochrone 请求失败: %s", exc)
            return None

    def _circle_fallback(self, loc: Location, minutes: int) -> Optional[Any]:
        """当 Mapbox 不可用时，用圆形缓冲区近似等时线。"""
        if not HAS_SHAPELY:
            return None
        speed = _SPEED_KM_MIN.get(self.transport, _SPEED_KM_MIN["transit"])
        radius_km = speed * minutes
        radius_deg = radius_km / 111.0  # 1° ≈ 111 km
        pt = Point(loc.lon, loc.lat)
        logger.info("使用圆形降级等时线 (%.4f, %.4f) | 半径 %.2f km", loc.lat, loc.lon, radius_km)
        return pt.buffer(radius_deg)

    def get_distance_circle(self, loc: Location, radius_km: float) -> Optional[Any]:
        """
        以公里为半径，在经纬度坐标系中生成椭球校正后的圆形多边形。

        由于经度方向 1° 的实际距离随纬度变化（1° lon = 111*cos(lat) km），
        此方法用仿射缩放使圆形在大地上近似正圆。

        参数:
            loc       : 圆心位置
            radius_km : 半径（公里）
        返回:
            Shapely Polygon，或 None（Shapely 不可用时）
        """
        if not HAS_SHAPELY:
            return None
        lat_rad = math.radians(loc.lat)
        cos_lat = math.cos(lat_rad)
        lat_deg = radius_km / 111.0
        lon_deg = radius_km / (111.0 * cos_lat) if cos_lat > 1e-9 else lat_deg
        pt = Point(loc.lon, loc.lat)
        unit_circle = pt.buffer(lat_deg, resolution=64)
        try:
            from shapely.affinity import scale as _shapely_scale  # type: ignore
            ellipse = _shapely_scale(unit_circle, xfact=lon_deg / lat_deg, yfact=1.0, origin=pt)
        except Exception:
            ellipse = unit_circle  # 仿射失败时保守降级
        logger.info(
            "距离圆 (%.5f, %.5f) 半径 %.2f km / lat_deg=%.5f lon_deg=%.5f",
            loc.lat, loc.lon, radius_km, lat_deg, lon_deg,
        )
        return ellipse

    def get_intersection_from_radii(
        self,
        a: Location,
        b: Location,
        radius_a_km: float,
        radius_b_km: float,
    ) -> Optional[Any]:
        """
        根据两人各自的公里半径生成可达圆，返回两圆交集多边形。

        若交集为空（双方距离超出两半径之和），降级为两圆并集（宽松模式）。
        可用于 search_nearby_venues 的 intersection 参数，将搜索范围约束在双方均可达区域。
        """
        circle_a = self.get_distance_circle(a, radius_a_km)
        circle_b = self.get_distance_circle(b, radius_b_km)
        return self.compute_intersection(circle_a, circle_b)

    def get_search_area_from_radii(
        self,
        a: Location,
        b: Location,
        radius_a_km: float,
        radius_b_km: float,
    ) -> Dict[str, Any]:
        """
        根据两人半径返回搜索区域与区域模式。

        mode:
          - intersection: 双方半径存在重叠
          - union_fallback: 双方半径无重叠，已降级为并集宽松搜索
          - unknown: 几何计算失败
        """
        circle_a = self.get_distance_circle(a, radius_a_km)
        circle_b = self.get_distance_circle(b, radius_b_km)
        if circle_a is None or circle_b is None:
            return {"geometry": None, "overlap_exists": False, "mode": "unknown"}
        try:
            inter = circle_a.intersection(circle_b)
            if inter.is_empty:
                logger.warning("半径交集为空（双方距离过远）— 使用并集降级搜索。")
                return {
                    "geometry": circle_a.union(circle_b),
                    "overlap_exists": False,
                    "mode": "union_fallback",
                }
            ratio = inter.area / min(circle_a.area, circle_b.area) * 100
            logger.info("半径交集占较小区域面积 %.1f%%", ratio)
            return {"geometry": inter, "overlap_exists": True, "mode": "intersection"}
        except Exception as exc:
            logger.error("半径交集计算失败: %s", exc)
            return {"geometry": None, "overlap_exists": False, "mode": "unknown"}

    def get_isochrone(self, loc: Location) -> Optional[Any]:
        """获取等时线多边形（优先 Mapbox，失败后使用圆形近似）。"""
        poly = self._fetch_isochrone_ors(loc, self.isochrone_minutes, self.transport)
        if poly is None:
            profile = _PROFILE_MAP.get(self.transport, "driving")
            poly = self._fetch_isochrone(loc, self.isochrone_minutes, profile)
        if poly is None:
            poly = self._circle_fallback(loc, self.isochrone_minutes)
        return poly

    def compute_intersection(self, iso_a: Any, iso_b: Any) -> Optional[Any]:
        """
        计算两个等时线多边形的空间交集。
        若交集为空（双方距离过远），降级为两者并集（宽松模式）。
        """
        if iso_a is None or iso_b is None:
            return None
        try:
            inter = iso_a.intersection(iso_b)
            if inter.is_empty:
                logger.warning("等时线交集为空（双方距离过远）— 使用并集降级。")
                return iso_a.union(iso_b)
            ratio = inter.area / min(iso_a.area, iso_b.area) * 100
            logger.info("等时线交集占较小多边形面积 %.1f%%", ratio)
            return inter
        except Exception as exc:
            logger.error("等时线交集计算失败: %s", exc)
            return None

    # -----------------------------------------------------------------------
    # 自然障碍剔除（水体 / 森林）— OSM Overpass API
    # -----------------------------------------------------------------------
    def _fetch_natural_barriers(self, poly: Any) -> List[Any]:
        """
        通过 Overpass API 获取多边形 bbox 内的水体和森林要素，
        返回 Shapely Polygon 列表。请求失败时返回空列表（降级保守模式）。

        查询目标：
          - natural=water  (湖泊、池塘、河流水面)
          - natural=wood   (树林)
          - landuse=forest (林地)
          - waterway=riverbank (河岸围合水面)
        """
        if not HAS_SHAPELY:
            return []

        # bounds: (lon_min, lat_min, lon_max, lat_max)
        minx, miny, maxx, maxy = poly.bounds
        # Overpass bbox 格式: south,west,north,east (即 lat_min,lon_min,lat_max,lon_max)
        query = (
            f"[out:json][timeout:20][bbox:{miny:.6f},{minx:.6f},{maxy:.6f},{maxx:.6f}];\n"
            "(\n"
            '  way["natural"~"^(water|wood)$"];\n'
            '  way["landuse"="forest"];\n'
            '  way["waterway"="riverbank"];\n'
            ");\n"
            "out geom;"
        )
        result = self._post_overpass_with_retry(query, timeout=25)
        if result is None:
            logger.warning("Overpass 自然障碍查询失败 — 跳过障碍剔除（保守模式）")
            return []
        elements = result.get("elements", [])

        barriers: List[Any] = []
        for elem in elements:
            if elem.get("type") != "way":
                continue
            geom_nodes = elem.get("geometry", [])
            if len(geom_nodes) < 4:  # 至少 3 顶点 + 闭合点
                continue
            coords = [(pt["lon"], pt["lat"]) for pt in geom_nodes]
            try:
                barrier = Polygon(coords)
                if barrier.is_valid and not barrier.is_empty:
                    barriers.append(barrier)
            except Exception:
                pass

        logger.info(
            "Overpass 自然障碍：在 bbox(%.4f,%.4f,%.4f,%.4f) 内找到 %d 个水体/森林要素",
            miny, minx, maxy, maxx, len(barriers),
        )
        return barriers

    def subtract_natural_barriers(self, intersection: Any) -> Any:
        """
        从等时线交集多边形中扣除水体、森林等不可实际抵达的自然地物。

        降级链：
          Overpass 查询失败 → 返回原交集（保守模式，不剔除任何区域）
          差集结果为空     → 返回原交集（保守模式）
          Shapely 未安装   → 返回原交集
        """
        if intersection is None or not HAS_SHAPELY:
            return intersection

        barriers = self._fetch_natural_barriers(intersection)
        if not barriers:
            return intersection

        original_area = intersection.area
        barrier_union = unary_union(barriers)
        try:
            result = intersection.difference(barrier_union)
            if result.is_empty:
                logger.warning("剔除自然障碍后交集为空 — 保留原交集多边形（保守模式）。")
                return intersection
            removed_pct = (original_area - result.area) / original_area * 100
            logger.info(
                "自然障碍剔除完成：共 %d 个区域，移除面积占比 %.1f%%",
                len(barriers),
                removed_pct,
            )
            return result
        except Exception as exc:
            logger.warning("障碍剔除计算异常: %s — 保留原多边形", exc)
            return intersection

    # -----------------------------------------------------------------------
    # 人流密度 Hard Filter（POI 密度过低 → 偏僻区域直接剔除）
    # -----------------------------------------------------------------------
    def filter_by_poi_density(
        self,
        candidates: List[CandidateRestaurant],
        radius_m: float = 300.0,
        min_poi_count: int = 5,
    ) -> List[CandidateRestaurant]:
        """
        人流密度 Hard Filter：剔除 POI 密度过低（过于偏僻）的候选场所。

        策略：
          1. 一次性用 Overpass API 批量查询所有候选点 bbox 内的公开 POI
             （amenity / shop / tourism / leisure）。
          2. 统计每个候选在 radius_m 米半径内的 POI 数量。
          3. 低于 min_poi_count 的候选直接剔除（hard filter）。
          4. 降级保守模式：Overpass 失败 或 全部被过滤 → 保留原列表。

        参数：
          radius_m      : 半径（米），默认 300m
          min_poi_count : 最低 POI 门槛，默认 5 个
        """
        if not candidates:
            return candidates

        # 计算 bbox，留 0.01° 缓冲（约 1 km）
        lats = [c.lat for c in candidates]
        lons = [c.lon for c in candidates]
        lat_min = min(lats) - 0.01
        lat_max = max(lats) + 0.01
        lon_min = min(lons) - 0.01
        lon_max = max(lons) + 0.01

        query = (
            f"[out:json][timeout:25]"
            f"[bbox:{lat_min:.6f},{lon_min:.6f},{lat_max:.6f},{lon_max:.6f}];\n"
            "(\n"
            '  node["amenity"];\n'
            '  node["shop"];\n'
            '  node["tourism"];\n'
            '  node["leisure"];\n'
            ");\n"
            "out body;"
        )
        result = self._post_overpass_with_retry(query, timeout=25)
        if result is None:
            logger.warning("Overpass POI 密度查询失败 — 跳过密度过滤（保守模式）")
            return candidates
        poi_nodes = result.get("elements", [])

        # 仅保留带坐标的 node
        poi_points: List[Tuple[float, float]] = [
            (float(n["lat"]), float(n["lon"]))
            for n in poi_nodes
            if n.get("type") == "node" and "lat" in n and "lon" in n
        ]
        logger.info(
            "Overpass POI 密度查询：bbox 内共 %d 个公开 POI 节点", len(poi_points)
        )

        radius_km = radius_m / 1000.0
        passed: List[CandidateRestaurant] = []
        for c in candidates:
            c_loc = Location(c.lat, c.lon)
            count = sum(
                1
                for (plat, plon) in poi_points
                if self.haversine_km(c_loc, Location(plat, plon)) <= radius_km
            )
            if count >= min_poi_count:
                passed.append(c)
                logger.debug(
                    "POI 密度通过  %-22s | %3d POI in %.0fm",
                    c.name[:22], count, radius_m,
                )
            else:
                logger.info(
                    "POI 密度剔除  %-22s | 仅 %d POI in %.0fm（阈值=%d）",
                    c.name[:22], count, radius_m, min_poi_count,
                )

        if not passed:
            logger.warning(
                "POI 密度 Hard Filter 后候选为空 — 保留原列表（保守模式）。"
            )
            return candidates

        logger.info(
            "POI 密度 Hard Filter：%d → %d 个候选（剔除 %d 个偏僻场所）",
            len(candidates), len(passed), len(candidates) - len(passed),
        )
        return passed

    def filter_closed_candidates(
        self,
        candidates: List[CandidateRestaurant],
    ) -> Tuple[List[CandidateRestaurant], Dict[str, int]]:
        """
        根据 Web 信号中的营业状态剔除已知关闭的候选。

        规则：
          - status=closed: 直接剔除
          - status=open / uncertain: 保留
        """
        stats = {"open": 0, "closed": 0, "uncertain": 0}
        filtered: List[CandidateRestaurant] = []
        for c in candidates:
            status = str((c.web_signals or {}).get("status", "uncertain")).lower()
            if status not in stats:
                status = "uncertain"
            stats[status] += 1
            if status == "closed":
                continue
            filtered.append(c)
        logger.info(
            "营业状态过滤：open=%d uncertain=%d closed=%d -> 保留 %d/%d",
            stats["open"],
            stats["uncertain"],
            stats["closed"],
            len(filtered),
            len(candidates),
        )
        return filtered, stats

    def tag_with_isochrone(
        self,
        candidates: List[CandidateRestaurant],
        intersection: Optional[Any],
        area_mode: str = "intersection",
    ) -> None:
        """标记每个候选餐厅是否落在等时线交集内。"""
        if intersection is None or not HAS_SHAPELY:
            for c in candidates:
                c.search_area_mode = area_mode
                c.in_isochrone_intersection = area_mode == "intersection"
            return
        for c in candidates:
            pt = Point(c.lon, c.lat)
            c.search_area_mode = area_mode
            c.in_isochrone_intersection = area_mode == "intersection" and intersection.contains(pt)
            verdict = "交集内" if c.in_isochrone_intersection else "交集外"
            logger.debug("%s  %s", verdict, c.name)

    # -----------------------------------------------------------------------
    # 餐厅搜索（Mapbox POI -> OSM 降级）
    # -----------------------------------------------------------------------
    def search_nearby_venues(
        self,
        center: Location,
        venue_type: str = "restaurant",
        keyword: str = "",
        limit: int = 12,
        intersection: Optional[Any] = None,
    ) -> List[CandidateRestaurant]:
        """
        搜索中心点周边场所（Mapbox POI → OSM 降级）。

        venue_type  : VENUE_TYPES 中的键（restaurant/cafe/park/mall/cinema/…）
        keyword     : 自定义搜索词（非空时覆盖 venue_type 内置 query）
        intersection: 交集多边形（Shapely Polygon）。若提供则：
                      1) 用交集质心替代 center 作为搜索中心；
                      2) 结果后置过滤——仅保留落在交集内的场所。
                         若过滤后为空则保守地保留全部结果。
        """
        cfg = VENUE_TYPES.get(venue_type, VENUE_TYPES["restaurant"])
        q = keyword.strip() if keyword.strip() else cfg["query"]

        # —— 若提供交集多边形，改用其质心作为搜索中心 ——
        search_center = center
        if intersection is not None and HAS_SHAPELY:
            try:
                if not intersection.is_empty:
                    centroid = intersection.centroid
                    search_center = Location(lat=centroid.y, lon=centroid.x)
                    logger.info(
                        "交集质心作为搜索中心: (%.5f, %.5f)",
                        search_center.lat, search_center.lon,
                    )
            except Exception as exc:
                logger.warning("无法提取交集质心，回退默认中心: %s", exc)

        if not self.mapbox_token:
            overpass_items = self._search_overpass(
                search_center, venue_type=venue_type, limit=limit, intersection=intersection
            )
            if overpass_items:
                return overpass_items
            logger.warning("Mapbox 未配置且 Overpass 无结果，切换 OSM Nominatim。")
            return self._search_osm(search_center, q, limit, venue_category=venue_type)

        url = f"{MAPBOX_GEOCODE_BASE}/{requests.utils.quote(q)}.json"
        params = {
            "access_token": self.mapbox_token,
            "proximity": f"{search_center.lon},{search_center.lat}",
            "types": "poi",
            "language": "zh",
            "limit": min(max(limit, 1), 20),
        }
        try:
            resp = requests.get(url, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("Mapbox 场所搜索失败: %s — 切换 Overpass/OSM", exc)
            overpass_items = self._search_overpass(
                search_center, venue_type=venue_type, limit=limit, intersection=intersection
            )
            if overpass_items:
                return overpass_items
            return self._search_osm(search_center, q, limit, venue_category=venue_type)

        items: List[CandidateRestaurant] = []
        for ft in data.get("features", []):
            coords = ft.get("center", [None, None])
            if coords[0] is None:
                continue
            pos = Location(lat=float(coords[1]), lon=float(coords[0]))
            items.append(
                CandidateRestaurant(
                    name=ft.get("text", "Unknown"),
                    lat=pos.lat,
                    lon=pos.lon,
                    place_name=ft.get("place_name", ""),
                    mapbox_relevance=float(ft.get("relevance", 0.5)),
                    distance_to_center_km=self.haversine_km(center, pos),
                    rating_proxy=float(ft.get("relevance", 0.5)),
                    venue_category=venue_type,
                )
            )

        # —— 交集过滤：仅保留落在交集区域内的场所 ——
        items = self._filter_by_intersection(items, intersection, center)
        if not items:
            logger.warning("Mapbox 成功返回但无候选，切换 Overpass/OSM 降级。")
            overpass_items = self._search_overpass(
                search_center, venue_type=venue_type, limit=limit, intersection=intersection
            )
            if overpass_items:
                return overpass_items
            return self._search_osm(search_center, q, limit, venue_category=venue_type)

        venue_display = cfg["display"]
        logger.info("Mapbox 搜索返回 %d 个候选场所（类型=%s）", len(items), venue_display)
        return items

    def _filter_by_intersection(
        self,
        items: List[CandidateRestaurant],
        intersection: Optional[Any],
        fallback_center: Location,
    ) -> List[CandidateRestaurant]:
        """
        将候选列表过滤到交集多边形内部。
        若过滤后为空（交集过小或候选稀疏），保守地保留原列表并记录警告。
        """
        if not items or intersection is None or not HAS_SHAPELY:
            return items
        try:
            if intersection.is_empty:
                return items
            inside = [c for c in items if intersection.contains(Point(c.lon, c.lat))]
            if inside:
                logger.info(
                    "交集过滤: %d → %d 个候选场所（仅保留重叠区域内）",
                    len(items), len(inside),
                )
                return inside
            logger.warning(
                "交集过滤后无候选 — 保留全部 %d 个候选并更新至中心距离（保守模式）",
                len(items),
            )
            # 保守模式：按交集质心重新排序
            centroid = intersection.centroid
            c_loc = Location(lat=centroid.y, lon=centroid.x)
            for c in items:
                c.distance_to_center_km = self.haversine_km(c_loc, Location(c.lat, c.lon))
            items.sort(key=lambda x: x.distance_to_center_km)
            return items
        except Exception as exc:
            logger.warning("交集过滤异常 — 保留原列表: %s", exc)
            return items

    def search_nearby_restaurants(
        self, center: Location, cuisine: str, limit: int = 12
    ) -> List[CandidateRestaurant]:
        """向后兼容接口，内部调用 search_nearby_venues。"""
        return self.search_nearby_venues(
            center=center, venue_type="restaurant", keyword=cuisine, limit=limit
        )

    def geocode_address(self, address: str, city_hint: str = "") -> Optional[Location]:
        """地址转坐标（优先 Mapbox，失败后降级 OSM）。"""
        query = address.strip()
        if city_hint and city_hint not in query:
            query = f"{city_hint} {query}"

        url = f"{MAPBOX_GEOCODE_BASE}/{requests.utils.quote(query)}.json"
        params = {
            "access_token": self.mapbox_token,
            "language": "zh",
            "limit": 1,
        }
        try:
            resp = requests.get(url, params=params, timeout=20)
            resp.raise_for_status()
            features = resp.json().get("features", [])
            if features:
                lon, lat = features[0]["center"]
                logger.info("地址解析成功(Mapbox): %s -> (%.6f, %.6f)", address, lat, lon)
                return Location(lat=float(lat), lon=float(lon))
        except Exception as exc:
            logger.warning("Mapbox 地址解析失败: %s", exc)

        return self._geocode_address_osm(address, city_hint)

    def _geocode_address_osm(self, address: str, city_hint: str = "") -> Optional[Location]:
        """地址转坐标 OSM 降级方案。"""
        query = address.strip()
        if city_hint and city_hint not in query:
            query = f"{city_hint} {query}"

        url = "https://nominatim.openstreetmap.org/search"
        params = {
            "q": query,
            "format": "json",
            "limit": 1,
        }
        headers = {"User-Agent": "MeetHalfwayAI/2.0 (hackathon demo)"}
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=20)
            resp.raise_for_status()
            rows = resp.json()
            if rows:
                lat = float(rows[0]["lat"])
                lon = float(rows[0]["lon"])
                logger.info("地址解析成功(OSM): %s -> (%.6f, %.6f)", address, lat, lon)
                return Location(lat=lat, lon=lon)
        except Exception as exc:
            logger.warning("OSM 地址解析失败: %s", exc)
        return None

    def _search_osm(
        self, center: Location, query: str, limit: int, venue_category: str = "restaurant"
    ) -> List[CandidateRestaurant]:
        """OSM Nominatim 降级搜索（Mapbox 不可用时使用）。"""
        logger.info("使用 OSM Nominatim 降级搜索 ...")
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            "q": query,
            "format": "json",
            "limit": limit,
            "addressdetails": 1,
            "viewbox": (
                f"{center.lon - 0.05},{center.lat + 0.05},"
                f"{center.lon + 0.05},{center.lat - 0.05}"
            ),
            "bounded": 1,
        }
        headers = {"User-Agent": "MeetHalfwayAI/2.0 (hackathon demo)"}
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error("OSM 降级搜索也失败: %s", exc)
            return []

        items = []
        for row in data:
            pos = Location(lat=float(row["lat"]), lon=float(row["lon"]))
            items.append(
                CandidateRestaurant(
                    name=row.get("display_name", "Unknown")[:50],
                    lat=pos.lat,
                    lon=pos.lon,
                    place_name=row.get("display_name", ""),
                    mapbox_relevance=0.5,
                    distance_to_center_km=self.haversine_km(center, pos),
                    rating_proxy=0.5,
                    venue_category=venue_category,
                )
            )
        venue_display = VENUE_TYPES.get(venue_category, {}).get("display", venue_category)
        logger.info("OSM 返回 %d 个候选场所（类型=%s）", len(items), venue_display)
        return items

    def _search_overpass(
        self,
        center: Location,
        venue_type: str = "restaurant",
        limit: int = 12,
        intersection: Optional[Any] = None,
    ) -> List[CandidateRestaurant]:
        """
        Overpass QL 场所搜索（无 Key），尽量单次请求避免高频。

        intersection: 若提供交集多边形，则用其 bbox 作为 Overpass 查询边界
                      并对结果进行多边形内部过滤，确保只返回双方可达区域内的场所。
        """
        selectors = _OVERPASS_FILTERS.get(venue_type, _OVERPASS_FILTERS["restaurant"])

        # 如果提供了交集多边形，优先用其 bbox 而非固定圆形半径
        if intersection is not None and HAS_SHAPELY:
            try:
                minx, miny, maxx, maxy = intersection.bounds  # (lon_min,lat_min,lon_max,lat_max)
                # 转换为 Overpass bbox 格式: south,west,north,east
                bbox_str = f"{miny:.6f},{minx:.6f},{maxy:.6f},{maxx:.6f}"
                selector_block = "\n".join(
                    f"  {s}({bbox_str});" for s in selectors
                )
                logger.info(
                    "Overpass 使用交集 bbox 查询: SW(%.5f,%.5f) NE(%.5f,%.5f)",
                    miny, minx, maxy, maxx,
                )
            except Exception as exc:
                logger.warning("交集 bbox 提取失败，回退固定半径: %s", exc)
                intersection = None  # 强制回退

        if intersection is None:
            radius_m = 4500
            selector_block = "\n".join(
                f"  {s}(around:{radius_m},{center.lat:.6f},{center.lon:.6f});" for s in selectors
            )

        query = (
            "[out:json][timeout:20];\n"
            "(\n"
            f"{selector_block}\n"
            ");\n"
            "out center tags;"
        )

        result_json = self._post_overpass_with_retry(query, timeout=25)
        if result_json is None:
            logger.warning("Overpass 场所搜索失败")
            return []
        rows = result_json.get("elements", [])

        dedup = set()
        items: List[CandidateRestaurant] = []
        for row in rows:
            lat = row.get("lat")
            lon = row.get("lon")
            if lat is None or lon is None:
                center_obj = row.get("center") or {}
                lat = center_obj.get("lat")
                lon = center_obj.get("lon")
            if lat is None or lon is None:
                continue

            tags = row.get("tags") or {}
            name = str(tags.get("name") or "Unknown")
            key = (name.lower(), round(float(lat), 5), round(float(lon), 5))
            if key in dedup:
                continue
            dedup.add(key)

            address = tags.get("addr:full") or tags.get("addr:street") or tags.get("name", "")
            pos = Location(lat=float(lat), lon=float(lon))
            items.append(
                CandidateRestaurant(
                    name=name[:80],
                    lat=pos.lat,
                    lon=pos.lon,
                    place_name=str(address),
                    mapbox_relevance=0.5,
                    distance_to_center_km=self.haversine_km(center, pos),
                    rating_proxy=0.5,
                    venue_category=venue_type,
                )
            )

        items.sort(key=lambda x: x.distance_to_center_km)
        # 若提供交集，过滤多边形内部的场所
        items = self._filter_by_intersection(items, intersection, center)
        result = items[: max(1, limit)]
        logger.info("Overpass 返回 %d 个候选场所（类型=%s）", len(result), venue_type)
        return result

    # -----------------------------------------------------------------------
    # 异步并发 Web 信号采集 + LLM 语义提取
    # -----------------------------------------------------------------------
    async def _fetch_tavily(
        self,
        client: httpx.AsyncClient,
        candidate: CandidateRestaurant,
        city_hint: str,
        year_hint: int,
        time_slot: str,
        party_size: int,
    ) -> Dict[str, Any]:
        """单个场所的网页采集（优先 Tavily，失败/缺失降级 DuckDuckGo）。"""
        venue_display = VENUE_TYPES.get(candidate.venue_category, {}).get("display", "场所")
        query = (
            f"{candidate.name} {city_hint} {time_slot} {party_size}人 "
            f"{venue_display} 开放状态 人流量 排队 等待 优惠 {year_hint}"
        )
        if not self.tavily_key:
            return await self._fetch_duckduckgo(
                query,
                candidate,
                time_slot,
                party_size,
                fallback_reason="missing_tavily_key",
            )

        payload = {
            "api_key": self.tavily_key,
            "query": query,
            "search_depth": self._tavily_search_depth,
            "include_answer": True,
            "max_results": self._tavily_max_results,
        }
        try:
            r = await client.post(TAVILY_SEARCH_URL, json=payload, timeout=30)
            r.raise_for_status()
            result = r.json()
        except Exception as exc:
            logger.warning("Tavily 搜索失败 [%s]: %s", candidate.name, exc)
            return await self._fetch_duckduckgo(
                query,
                candidate,
                time_slot,
                party_size,
                fallback_reason=f"tavily_failed: {type(exc).__name__}",
            )

        answer = (result.get("answer") or "").strip()
        snippets = [
            {
                "title": row.get("title", ""),
                "content": row.get("content", "")[:400],
                "url": row.get("url", ""),
            }
            for row in result.get("results", [])
        ]

        # LLM 语义提取（优先），关键词匹配（降级）
        if self.use_llm_extraction and self._openai_ok and self.openai_key:
            signals = await self._llm_extract(
                candidate.name,
                answer,
                snippets,
                time_slot=time_slot,
                party_size=party_size,
                venue_type=candidate.venue_category,
            )
        else:
            signals = self._keyword_extract(answer, snippets)

        signals.update(
            {
                "query": query,
                "answer": answer,
                "snippets": snippets,
                "web_source": "tavily",
                "fallback_reason": "",
                "fetch_error": "",
            }
        )
        return signals

    async def _fetch_duckduckgo(
        self,
        query: str,
        candidate: CandidateRestaurant,
        time_slot: str,
        party_size: int,
        fallback_reason: str = "",
    ) -> Dict[str, Any]:
        """DuckDuckGo 搜索降级（无需 API Key）。"""

        def _ddg_text(q: str) -> List[Dict[str, Any]]:
            from ddgs import DDGS  # noqa: PLC0415

            with DDGS() as ddgs:
                return list(ddgs.text(q, max_results=5))

        try:
            rows = await asyncio.to_thread(_ddg_text, query)
        except Exception as exc:
            logger.warning("DuckDuckGo 搜索失败 [%s]: %s", candidate.name, exc)
            out = self._default_signals(f"ddg_failed: {exc}")
            out.update(
                {
                    "web_source": "duckduckgo",
                    "fallback_reason": fallback_reason or "direct_duckduckgo",
                    "fetch_error": str(exc)[:120],
                }
            )
            return out

        snippets = [
            {
                "title": str(r.get("title") or "")[:120],
                "content": str(r.get("body") or "")[:400],
                "url": str(r.get("href") or r.get("url") or ""),
            }
            for r in rows
        ]
        answer = "\n".join(s["content"] for s in snippets if s.get("content"))[:1200]

        if self.use_llm_extraction and self._openai_ok and self.openai_key:
            signals = await self._llm_extract(
                candidate.name,
                answer,
                snippets,
                time_slot=time_slot,
                party_size=party_size,
                venue_type=candidate.venue_category,
            )
        else:
            signals = self._keyword_extract(answer, snippets)

        signals.update(
            {
                "query": query,
                "answer": answer,
                "snippets": snippets,
                "web_source": "duckduckgo",
                "fallback_reason": fallback_reason or "direct_duckduckgo",
                "fetch_error": "",
            }
        )
        return signals

    async def _llm_extract(
        self,
        restaurant_name: str,
        answer: str,
        snippets: List[Dict],
        time_slot: str,
        party_size: int,
        venue_type: str = "restaurant",
    ) -> Dict[str, Any]:
        """
        将 Tavily 搜索结果喂给 GPT-4o-mini，返回结构化 JSON。

        Prompt 设计目标：
        - 识别委婉表达（如"老板回老家结婚了，暂别一个月" -> closed）
        - 提取置信度字段，避免过度自信
        - 强制 JSON 输出格式，防止幻觉污染评分
        """
        snippet_text = "\n".join(
            f"[{i + 1}] {s['title']}\n{s['content']}" for i, s in enumerate(snippets[:5])
        )
        venue_display = VENUE_TYPES.get(venue_type, {}).get("display", "场所")
        system_prompt = (
            f"你是一个{venue_display}实时信息提取专家。仔细阅读搜索结果（包括隐晦表达），"
            "返回合法 JSON，只含以下字段，不输出其他任何文字：\n"
            "  status        : 'open' | 'closed' | 'uncertain'\n"
            "  promo_bonus   : 0.0~1.0（有折扣/优惠券/满减等促销 -> 0.6，无 -> 0.0）\n"
            "  queue_level   : 'low' | 'medium' | 'high' | 'unknown'\n"
            "  crowd_index   : 0.0~1.0（该时段拥挤程度，越大越拥挤）\n"
            "  estimated_wait_minutes : 0~180（该时段该人数预估等位分钟）\n"
            "  risk_penalty  : 0.0~1.0（停业/装修/卫生投诉等风险 -> 0.8，无 -> 0.0）\n"
            "  confidence    : 'high' | 'medium' | 'low'\n"
            "  reason        : 最多 30 字的中文说明\n"
            "注意：若描述含有委婉停业表达（如'暂别''老板有事''改造升级'等）"
            "应识别为 status=closed。信息不足时 confidence=low。"
        )
        user_msg = (
            f"{venue_display}名称: {restaurant_name}\n\n"
            f"目标场景: {time_slot}，{party_size}人\n\n"
            f"Tavily 汇总:\n{answer}\n\n"
            f"搜索片段:\n{snippet_text}"
        )
        try:
            aclient = self._get_async_openai_client()
            if aclient is None:
                raise RuntimeError("openai_client_unavailable")
            resp = await aclient.chat.completions.create(
                model=self.openai_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                temperature=0.0,
                max_tokens=200,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content or "{}"
            data = json.loads(raw)
            logger.debug(
                "LLM 提取 [%s] -> status=%s reason=%s",
                restaurant_name,
                data.get("status"),
                data.get("reason", ""),
            )
            return {
                "status": data.get("status", "uncertain"),
                "risk_penalty": float(data.get("risk_penalty", 0.0)),
                "promo_bonus": float(data.get("promo_bonus", 0.0)),
                "queue_level": data.get("queue_level", "unknown"),
                "crowd_index": min(max(float(data.get("crowd_index", 0.5)), 0.0), 1.0),
                "estimated_wait_minutes": max(int(float(data.get("estimated_wait_minutes", 0) or 0)), 0),
                "confidence": data.get("confidence", "low"),
                "reason": data.get("reason", ""),
            }
        except Exception as exc:
            logger.warning(
                "LLM 提取失败 [%s]: %s — 降级为关键词匹配", restaurant_name, exc
            )
            self._openai_ok = False  # 本次会话不再重试
            return self._keyword_extract(answer, snippets)

    async def _fetch_yelp(
        self,
        client: httpx.AsyncClient,
        candidate: CandidateRestaurant,
    ) -> Dict[str, Any]:
        """查询 Yelp Fusion 评分与评论量，补强 rating_proxy。"""
        if not self.yelp_api_key:
            return {"matched": False, "source": "yelp", "error": "missing_yelp_api_key"}

        params = {
            "term": candidate.name,
            "latitude": candidate.lat,
            "longitude": candidate.lon,
            "limit": 1,
            "radius": 500,
            "sort_by": "best_match",
        }
        headers = {"Authorization": f"Bearer {self.yelp_api_key}"}
        async with self._yelp_sem:
            for attempt in range(self._http_max_retries):
                try:
                    r = await client.get(YELP_SEARCH_URL, params=params, headers=headers, timeout=20)
                    if r.status_code in (429, 500, 502, 503, 504):
                        raise httpx.HTTPStatusError(
                            f"retryable status: {r.status_code}",
                            request=r.request,
                            response=r,
                        )

                    r.raise_for_status()
                    businesses = r.json().get("businesses", [])
                    if not businesses:
                        return {"matched": False, "source": "yelp", "error": "no_match"}

                    top = businesses[0]
                    rating = float(top.get("rating", 0.0) or 0.0)
                    review_count = int(top.get("review_count", 0) or 0)
                    rating_norm = _clip01(rating / 5.0)
                    # 评论量置信补偿：避免仅靠少量评论抬高评分
                    review_conf = _clip01(math.log1p(review_count) / math.log(501))
                    blended = _clip01(0.8 * rating_norm + 0.2 * review_conf)

                    return {
                        "matched": True,
                        "source": "yelp",
                        "name": top.get("name", ""),
                        "rating": rating,
                        "review_count": review_count,
                        "rating_norm": rating_norm,
                        "blended_rating": blended,
                    }
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code if exc.response is not None else None
                    if status in (429, 500, 502, 503, 504) and attempt < self._http_max_retries - 1:
                        delay = self._backoff_seconds(attempt)
                        logger.warning(
                            "Yelp 暂时限流/异常 [%s] status=%s，%.2fs 后重试（%d/%d）",
                            candidate.name,
                            status,
                            delay,
                            attempt + 1,
                            self._http_max_retries,
                        )
                        await asyncio.sleep(delay)
                        continue
                    logger.warning("Yelp 查询失败 [%s]: %s", candidate.name, exc)
                    return {
                        "matched": False,
                        "source": "yelp",
                        "status_code": status,
                        "error": str(exc)[:120],
                    }
                except Exception as exc:
                    if attempt < self._http_max_retries - 1:
                        delay = self._backoff_seconds(attempt)
                        logger.warning(
                            "Yelp 请求异常 [%s]: %s，%.2fs 后重试（%d/%d）",
                            candidate.name,
                            exc,
                            delay,
                            attempt + 1,
                            self._http_max_retries,
                        )
                        await asyncio.sleep(delay)
                        continue
                    logger.warning("Yelp 查询失败 [%s]: %s", candidate.name, exc)
                    return {"matched": False, "source": "yelp", "error": str(exc)[:120]}

        return {"matched": False, "source": "yelp", "error": "retry_exhausted"}

    @staticmethod
    def _keyword_extract(answer: str, snippets: List[Dict]) -> Dict[str, Any]:
        """关键词匹配降级方案（OpenAI 不可用时）。"""
        blob = answer + "\n" + "\n".join(s.get("content", "") for s in snippets)

        status = "uncertain"
        risk_penalty = 0.0
        if any(
            k in blob
            for k in ["停业", "闭店", "装修", "歇业", "暂停营业", "暂别", "temporarily closed"]
        ):
            status = "closed"
            risk_penalty = 0.8
        elif any(k in blob for k in ["营业中", "正常营业", "open now", "营业时间"]):
            status = "open"

        promo_bonus = (
            0.6
            if any(k in blob for k in ["优惠", "折扣", "团购", "代金券", "满减", "coupon"])
            else 0.0
        )
        queue_level = (
            "high"
            if any(k in blob for k in ["排队", "等位", "wait", "line"])
            else "unknown"
        )
        crowd_index = 0.75 if queue_level == "high" else 0.45
        estimated_wait_minutes = 35 if queue_level == "high" else 0
        return {
            "status": status,
            "risk_penalty": risk_penalty,
            "promo_bonus": promo_bonus,
            "queue_level": queue_level,
            "crowd_index": crowd_index,
            "estimated_wait_minutes": estimated_wait_minutes,
            "confidence": "low",
            "reason": "keyword_fallback",
        }

    @staticmethod
    def _default_signals(msg: str = "") -> Dict[str, Any]:
        return {
            "status": "uncertain",
            "risk_penalty": 0.0,
            "promo_bonus": 0.0,
            "queue_level": "unknown",
            "crowd_index": 0.5,
            "estimated_wait_minutes": 0,
            "confidence": "low",
            "reason": msg[:80],
            "answer": "",
            "snippets": [],
            "web_source": "unknown",
            "fallback_reason": "",
            "fetch_error": "",
        }

    def _seed_default_web_signals(self, candidates: List[CandidateRestaurant], reason: str = "") -> None:
        for c in candidates:
            if not c.web_signals:
                c.web_signals = self._default_signals(reason)

    def _pick_candidates_for_enrichment(
        self,
        candidates: List[CandidateRestaurant],
    ) -> List[CandidateRestaurant]:
        if not candidates:
            return []
        limit = self.max_enriched_candidates
        if limit is None or limit >= len(candidates):
            return candidates

        ranked = sorted(
            candidates,
            key=lambda c: (
                0 if c.in_isochrone_intersection else 1,
                c.distance_to_center_km,
                -float(c.mapbox_relevance),
                -float(c.rating_proxy),
            ),
        )
        selected = ranked[:limit]
        logger.info(
            "低成本模式：仅增强前 %d/%d 个候选，其余候选使用默认网页信号。",
            len(selected),
            len(candidates),
        )
        return selected

    async def enrich_all_async(
        self,
        candidates: List[CandidateRestaurant],
        city_hint: str,
        year_hint: int = 2026,
        time_slot: str = "今晚 19:00",
        party_size: int = 2,
    ) -> None:
        """
        并发获取所有候选餐厅的网络信号（asyncio.gather 提速 5-10x）。

        零足迹承诺：所有中间数据仅存活于本函数栈帧，不写入磁盘。
        """
        logger.info("启动并发 Web 信号采集，共 %d 家餐厅 ...", len(candidates))
        self._seed_default_web_signals(candidates, "not_enriched")
        active_candidates = self._pick_candidates_for_enrichment(candidates)
        if not active_candidates:
            return

        t0 = time.monotonic()
        async with httpx.AsyncClient() as client:
            tavily_tasks = [
                self._fetch_tavily(client, c, city_hint, year_hint, time_slot, party_size)
                for c in active_candidates
            ]
            tavily_results = await asyncio.gather(*tavily_tasks, return_exceptions=True)
            if self.use_yelp and self.yelp_api_key:
                yelp_tasks = [self._fetch_yelp(client, c) for c in active_candidates]
                yelp_results = await asyncio.gather(*yelp_tasks, return_exceptions=True)
            else:
                yelp_results = [{} for _ in active_candidates]

        for c, sig, yelp in zip(active_candidates, tavily_results, yelp_results):
            if isinstance(sig, dict):
                c.web_signals = sig
            else:
                c.web_signals = self._default_signals(str(sig))

            if isinstance(yelp, dict) and yelp:
                c.web_signals["yelp"] = yelp
                if yelp.get("matched") and "blended_rating" in yelp:
                    c.rating_proxy = _clip01(
                        0.5 * c.rating_proxy + 0.5 * float(yelp["blended_rating"])
                    )

        elapsed = time.monotonic() - t0
        logger.info(
            "并发采集完成 %.2fs（平均 %.2fs/家）",
            elapsed,
            elapsed / max(len(candidates), 1),
        )

    # -----------------------------------------------------------------------
    # 评分（时间公平性 + 等时线 + 指数惩罚）
    # -----------------------------------------------------------------------
    def score_candidates(
        self,
        a: Location,
        b: Location,
        center: Location,
        candidates: List[CandidateRestaurant],
        w_dist: float,
        w_rating: float,
        w_pref: float,
        tired_person: Optional[str] = None,
        time_slots: Optional[List[str]] = None,
        availability: Optional[Dict[str, List[str]]] = None,
        place_votes: Optional[Dict[str, Dict[str, float]]] = None,
        time_votes: Optional[Dict[str, Dict[str, float]]] = None,
        radius_tolerance: Optional[Dict[str, float]] = None,
        time_conflict: bool = False,
    ) -> List[CandidateRestaurant]:
        """
        MCDM 评分 v2：

        1. 时间公平性替代距离公平性
           - 用行程分钟数代替公里差
           - 指数惩罚：时间差 > 20 分钟时分数呈指数级下降
             penalty = exp(|ta - tb| / 10) - 1

        2. 等时线归属奖惩
           - 在交集内：+0.4 分奖励
           - 在交集外：-0.6 分惩罚

        3. 疲劳度参数（tired_person='a'|'b'）
           - 疲劳方的行程时间系数上调 1.3，中心点自动向其倾斜
        """
        max_center_dist = max((c.distance_to_center_km for c in candidates), default=1.0) or 1.0
        active_slots = time_slots or ["灵活"]

        a_fatigue = 1.3 if tired_person == "a" else 1.0
        b_fatigue = 1.3 if tired_person == "b" else 1.0

        for c in candidates:
            rest = Location(c.lat, c.lon)
            da_km = self.haversine_km(a, rest)
            db_km = self.haversine_km(b, rest)
            ta_min = self._travel_minutes(a, rest) * a_fatigue
            tb_min = self._travel_minutes(b, rest) * b_fatigue

            c.fairness_delta_km = abs(da_km - db_km)
            c.fairness_delta_minutes = abs(
                self._travel_minutes(a, rest) - self._travel_minutes(b, rest)
            )

            # 核心：时间差指数惩罚（避免"一人走 5 分钟，另一人走 50 分钟"极端不公平）
            time_gap = abs(ta_min - tb_min)
            fairness_penalty = math.exp(time_gap / 10.0) - 1.0  # 0 when perfectly fair
            fairness_balance = _clip01(1.0 - time_gap / max(ta_min + tb_min, 1.0))

            dist_component = 1.0 - min(c.distance_to_center_km / max_center_dist, 1.0)
            rating_component = min(max(c.rating_proxy, 0.0), 1.0)
            pref_component = _clip01(
                0.6 * max(0.0, 1.0 - fairness_penalty / 5.0)
                + 0.4 * fairness_balance
            )

            c.best_time_slot, c.availability_overlap, c.radius_tolerance_score, c.time_vote_score = (
                self._time_negotiation_for_candidate(
                    c,
                    a,
                    b,
                    active_slots,
                    availability,
                    time_votes,
                    radius_tolerance,
                    a_fatigue,
                    b_fatigue,
                    time_conflict,
                )
            )
            c.time_conflict = time_conflict
            c.mutual_vote_score = self._place_vote_for_candidate(c, place_votes)

            web_bonus = float(c.web_signals.get("promo_bonus", 0.0))
            web_penalty = float(c.web_signals.get("risk_penalty", 0.0))
            status = str(c.web_signals.get("status", "uncertain")).lower()
            status_penalty = {
                "open": 0.0,
                "uncertain": 0.12,
                "closed": 1.4,
            }.get(status, 0.12)
            queue_level = str(c.web_signals.get("queue_level", "unknown")).lower()
            queue_penalty = {
                "low": 0.00,
                "medium": 0.08,
                "high": 0.18,
                "unknown": 0.05,
            }.get(queue_level, 0.05)
            crowd_index = min(max(float(c.web_signals.get("crowd_index", 0.5)), 0.0), 1.0)
            wait_minutes = max(float(c.web_signals.get("estimated_wait_minutes", 0.0)), 0.0)
            crowd_penalty = max(queue_penalty, crowd_index * 0.22 + min(wait_minutes / 180.0, 0.25))
            # 人气/密度在中高水平较优：过冷与过热都扣分
            density_balance = _clip01(1.0 - abs(crowd_index - 0.55) / 0.55)
            c.venue_popularity_score = _clip01(0.55 * rating_component + 0.45 * density_balance)
            iso_bonus = 0.4 if c.in_isochrone_intersection else -0.6

            spatiotemporal_bonus = (
                0.23 * c.radius_tolerance_score
                + 0.23 * c.availability_overlap
                + 0.18 * c.venue_popularity_score
                + 0.20 * c.mutual_vote_score
                + 0.16 * c.time_vote_score
                - 0.5
            )

            raw = (
                w_dist * dist_component
                + w_rating * rating_component
                + w_pref * pref_component
                + 0.25 * web_bonus
                - 0.5 * web_penalty
                - status_penalty
                - crowd_penalty
                + iso_bonus
                + 0.9 * spatiotemporal_bonus
            )
            c.final_score = max(0.0, raw)
            c.score_breakdown = {
                "distance": round(dist_component, 4),
                "rating": round(rating_component, 4),
                "fairness": round(pref_component, 4),
                "radius_tolerance": round(c.radius_tolerance_score, 4),
                "availability_overlap": round(c.availability_overlap, 4),
                "venue_popularity": round(c.venue_popularity_score, 4),
                "mutual_vote": round(c.mutual_vote_score, 4),
                "time_vote": round(c.time_vote_score, 4),
            }

            logger.debug(
                "评分 %-20s | dist=%.2f rat=%.2f fair=%.2f radius=%.2f avail=%.2f "
                "vote=%.2f tVote=%.2f pop=%.2f iso=%+.1f web(%+.2f/-%0.2f) crowd=-%.2f delta=%.1fmin -> %.3f",
                c.name[:20],
                dist_component,
                rating_component,
                pref_component,
                c.radius_tolerance_score,
                c.availability_overlap,
                c.mutual_vote_score,
                c.time_vote_score,
                c.venue_popularity_score,
                iso_bonus,
                web_bonus,
                web_penalty,
                crowd_penalty,
                c.fairness_delta_minutes,
                c.final_score,
            )

        candidates.sort(key=lambda x: x.final_score, reverse=True)
        return candidates

    # -----------------------------------------------------------------------
    # AI 推荐文本生成
    # -----------------------------------------------------------------------
    def generate_recommendation_text(
        self,
        a: Location,
        b: Location,
        center: Location,
        top_items: List[CandidateRestaurant],
        budget: float,
        cuisine: str,
        time_slot: str = "今晚 19:00",
        party_size: int = 2,
        venue_type: str = "restaurant",
    ) -> str:
        venue_display = VENUE_TYPES.get(venue_type, {}).get("display", "场所")
        if not top_items:
            return f"未找到合适候选{venue_display}，请扩大搜索范围或调整关键词。"

        structured = [
            {
                "name": x.name,
                "address": x.place_name,
                "score": round(x.final_score, 4),
                "distance_to_center_km": round(x.distance_to_center_km, 2),
                "fairness_delta_km": round(x.fairness_delta_km, 2),
                "fairness_delta_minutes": round(x.fairness_delta_minutes, 1),
                "best_time_slot": x.best_time_slot,
                "availability_overlap": round(x.availability_overlap, 2),
                "radius_tolerance_score": round(x.radius_tolerance_score, 2),
                "venue_popularity_score": round(x.venue_popularity_score, 2),
                "mutual_vote_score": round(x.mutual_vote_score, 2),
                "time_vote_score": round(x.time_vote_score, 2),
                "in_isochrone_zone": x.in_isochrone_intersection,
                "status": x.web_signals.get("status"),
                "queue": x.web_signals.get("queue_level"),
                "crowd_index": x.web_signals.get("crowd_index", 0.5),
                "wait_minutes": x.web_signals.get("estimated_wait_minutes", 0),
                "promo": x.web_signals.get("promo_bonus", 0),
                "confidence": x.web_signals.get("confidence", "low"),
                "web_reason": x.web_signals.get("reason", ""),
            }
            for x in top_items
        ]

        # 无 OpenAI 或关闭 LLM 摘要时，使用本地模板汇总
        if not self.use_llm_summary or not self._openai_ok or not self.openai_key:
            lines = [
                f"推荐类型: {venue_display}（{cuisine}），预算: 人均{budget}元。",
                f"目标场景: {time_slot}，{party_size}人。",
                f"均衡中心点: ({center.lat:.6f}, {center.lon:.6f})",
                "Top 推荐:",
            ]
            for i, item in enumerate(structured, start=1):
                iso_tag = "等时线内" if item["in_isochrone_zone"] else "边缘区"
                lines.append(
                    f"{i}. [{iso_tag}] {item['name']} | 分数 {item['score']} "
                    f"| 时间差 {item['fairness_delta_minutes']} 分 | 状态 {item['status']} "
                    f"| 协商时间 {item['best_time_slot']} | 等位 {item['wait_minutes']} 分"
                )
            return "\n".join(lines)

        prompt = {
            "task": f"根据候选{venue_display}结构化数据，输出简洁可执行的双人见面推荐（中文）",
            "constraints": [
                f"优先推荐 in_isochrone_zone=true 的{venue_display}（双方均在合理通勤时间内）",
                "剔除 status=closed 或 risk_penalty>0.5 的候选",
                "强调时间公平性（fairness_delta_minutes 越小越好，差值>20分钟须警告）",
                "指出是否有优惠活动和排队风险",
                "明确该时间段与人数下的人流量与等位风险（低/中/高 + 预计分钟）",
                "给出最终 Top3，每条附一句理由和具体行动建议",
            ],
            "input": {
                "person_a": {"lat": a.lat, "lon": a.lon},
                "person_b": {"lat": b.lat, "lon": b.lon},
                "center": {"lat": center.lat, "lon": center.lon},
                "budget_per_person": budget,
                "cuisine": cuisine,
                "time_slot": time_slot,
                "party_size": party_size,
                "candidates": structured,
            },
        }
        try:
            client = self._get_sync_openai_client()
            if client is None:
                raise RuntimeError("openai_client_unavailable")
            resp = client.chat.completions.create(
                model=self.openai_model,
                messages=[
                    {
                        "role": "system",
                        "content": "你是城市约会顾问，回答用简体中文，直观、简洁、有行动建议。",
                    },
                    {
                        "role": "user",
                        "content": json.dumps(prompt, ensure_ascii=False),
                    },
                ],
                temperature=0.4,
                max_tokens=220 if self.low_cost_mode else 600,
            )
            return resp.choices[0].message.content or "模型未返回文本。"
        except Exception as exc:
            logger.error("OpenAI 推荐生成失败: %s", exc)
            return f"AI 推荐生成失败（{exc}），请检查 OpenAI 配置。"

    def build_explanations(
        self,
        candidates: List[CandidateRestaurant],
        top_k: int = 5,
    ) -> Dict[str, str]:
        """
        为 Top 候选生成“为什么推荐这个时间+地点”的自然语言解释。

        解释逻辑与评分项一一对应：
        - radius_tolerance_score
        - availability_overlap
        - venue_popularity_score
        - mutual_vote_score
        - time_vote_score
        """
        explanations: Dict[str, str] = {}
        selected = candidates[: max(1, top_k)]

        for idx, c in enumerate(selected, start=1):
            iso_text = "位于双方可达交集内" if c.in_isochrone_intersection else "位于可达边缘区"

            radius_level = (
                "高" if c.radius_tolerance_score >= 0.75 else "中" if c.radius_tolerance_score >= 0.45 else "低"
            )
            overlap_level = (
                "高" if c.availability_overlap >= 0.75 else "中" if c.availability_overlap >= 0.35 else "低"
            )
            popularity_level = (
                "高" if c.venue_popularity_score >= 0.7 else "中" if c.venue_popularity_score >= 0.45 else "低"
            )
            vote_level = (
                "高" if c.mutual_vote_score >= 0.7 else "中" if c.mutual_vote_score >= 0.45 else "低"
            )
            t_vote_level = (
                "高" if c.time_vote_score >= 0.7 else "中" if c.time_vote_score >= 0.45 else "低"
            )

            slot_text = c.best_time_slot or "灵活时段"
            wait_text = int(float(c.web_signals.get("estimated_wait_minutes", 0) or 0))
            queue_text = str(c.web_signals.get("queue_level", "unknown"))

            lines = [
                f"Top{idx} 推荐 {c.name}（建议时间：{slot_text}）。",
                f"空间公平性：{iso_text}，双方通勤容忍匹配度{radius_level}（{c.radius_tolerance_score:.2f}），当前时间差约{c.fairness_delta_minutes:.1f}分钟。",
                f"时间协商：双方可用时间重叠度{overlap_level}（{c.availability_overlap:.2f}），该时段联合投票偏好{t_vote_level}（{c.time_vote_score:.2f}）。",
                f"偏好一致性：地点互选偏好{vote_level}（{c.mutual_vote_score:.2f}）。",
                f"场地状态：热度/密度适配度{popularity_level}（{c.venue_popularity_score:.2f}），排队{queue_text}，预计等位{wait_text}分钟。",
                f"综合结论：该候选在“地点+时间”双维度上更平衡，因此进入前列（总分 {c.final_score:.3f}）。",
            ]
            explanations[c.name] = "\n".join(lines)

        return explanations

    # -----------------------------------------------------------------------
    # Surprise Me 惊喜模式
    # -----------------------------------------------------------------------
    def pick_surprise(
        self, candidates: List[CandidateRestaurant]
    ) -> Optional[CandidateRestaurant]:
        """
        从高分候选中随机挑选一家，绕过 Top1，打破信息茧房。
        条件：score > 0.5，在等时线内，非停业状态。
        """
        eligible = [
            c
            for c in candidates
            if c.final_score > 0.5
            and c.in_isochrone_intersection
            and c.web_signals.get("status") not in ("closed",)
        ]
        if not eligible:
            eligible = [c for c in candidates if c.final_score > 0.3]
        return random.choice(eligible) if eligible else None

    # -----------------------------------------------------------------------
    # 交互式地图（folium）
    # -----------------------------------------------------------------------
    def generate_map(
        self,
        a: Location,
        b: Location,
        center: Location,
        candidates: List[CandidateRestaurant],
        intersection: Optional[Any] = None,
        output_path: str = "meethalfway_map.html",
        surprise: Optional[CandidateRestaurant] = None,
        top_k: int = 5,
        show_user_points: bool = True,
    ) -> str:
        if not HAS_FOLIUM:
            logger.warning("folium 未安装 — 跳过地图生成。")
            return ""

        m = folium.Map(location=[center.lat, center.lon], zoom_start=14)

        # 等时线交集覆盖层
        if HAS_SHAPELY and intersection is not None and not intersection.is_empty:
            try:
                folium.GeoJson(
                    mapping(intersection),
                    style_function=lambda _: {
                        "fillColor": "#3399ff",
                        "color": "#0066cc",
                        "weight": 2,
                        "fillOpacity": 0.15,
                    },
                    tooltip="等时线交集（A、B 双方均可合理到达的区域）",
                ).add_to(m)
            except Exception as exc:
                logger.warning("等时线渲染失败: %s", exc)

        # 出发地 A / B
        if show_user_points:
            for loc, label, color in [(a, "出发地 A", "blue"), (b, "出发地 B", "red")]:
                folium.Marker(
                    [loc.lat, loc.lon],
                    tooltip=label,
                    icon=folium.Icon(color=color, icon="user", prefix="fa"),
                ).add_to(m)

        # 均衡中心点
        folium.Marker(
            [center.lat, center.lon],
            tooltip="均衡中心点",
            icon=folium.Icon(color="purple", icon="map-marker", prefix="fa"),
        ).add_to(m)

        # 候选餐厅
        top_names = {c.name for c in candidates[:top_k]}
        surprise_name = surprise.name if surprise else ""
        for c in candidates:
            if c.name == surprise_name:
                color, icon_name = "orange", "star"
            elif c.name in top_names:
                color, icon_name = "green", "cutlery"
            else:
                color, icon_name = "gray", "cutlery"

            iso_tag = "✓" if c.in_isochrone_intersection else "△"
            tip = (
                f"{iso_tag} {c.name}<br>"
                f"分数: {c.final_score:.3f}<br>"
                f"时间差: {c.fairness_delta_minutes:.1f} 分钟<br>"
                f"状态: {c.web_signals.get('status', '?')}<br>"
                f"{c.web_signals.get('reason', '')}"
            )
            if c.name == surprise_name:
                tip = "Surprise Pick!<br>" + tip

            folium.Marker(
                [c.lat, c.lon],
                tooltip=folium.Tooltip(tip),
                icon=folium.Icon(color=color, icon=icon_name, prefix="fa"),
            ).add_to(m)

        m.save(output_path)
        logger.info("交互地图已保存: %s", output_path)
        return output_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MeetHalfway AI v2 — 等时线交集 · LLM语义提取 · 零足迹",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--a-lat", type=float, help="出发地 A 纬度")
    parser.add_argument("--a-lon", type=float, help="出发地 A 经度")
    parser.add_argument("--b-lat", type=float, help="出发地 B 纬度")
    parser.add_argument("--b-lon", type=float, help="出发地 B 经度")
    parser.add_argument("--a-address", type=str, default="", help="出发地 A 地址（可替代坐标）")
    parser.add_argument("--b-address", type=str, default="", help="出发地 B 地址（可替代坐标）")
    parser.add_argument("--cuisine", type=str, default="hotpot", help="菜系关键词")
    parser.add_argument("--budget", type=float, default=80, help="人均预算（元）")
    parser.add_argument(
        "--venue-type",
        type=str,
        default="restaurant",
        choices=sorted(VENUE_TYPES.keys()),
        help="场所类型（restaurant/cafe/park/mall/cinema/...）",
    )
    parser.add_argument(
        "--transport",
        type=str,
        default="transit",
        choices=["drive", "walk", "transit"],
        help="出行方式",
    )
    parser.add_argument("--top-k", type=int, default=5, help="最终推荐数量")
    parser.add_argument(
        "--weight-a", type=float, default=1.0, help="A 的优先权重（越高中心越靠近 A）"
    )
    parser.add_argument("--weight-b", type=float, default=1.0, help="B 的优先权重")
    parser.add_argument(
        "--tired",
        type=str,
        default="",
        choices=["", "a", "b"],
        help="疲劳方（自动调高其权重使中心点向其倾斜）",
    )
    parser.add_argument(
        "--isochrone-minutes", type=int, default=20, help="等时线时间阈值（分钟）"
    )
    parser.add_argument("--city", type=str, default="", help="城市名称（用于搜索上下文）")
    parser.add_argument("--time-slot", type=str, default="今晚 19:00", help="目标就餐时间段")
    parser.add_argument("--party-size", type=int, default=2, help="就餐人数")
    parser.add_argument("--low-cost", action="store_true", help="免费额度测试模式：减少候选数和模型调用")
    parser.add_argument("--enable-yelp", action="store_true", help="启用 Yelp 增强（会更慢）")
    parser.add_argument("--enable-llm-summary", action="store_true", help="启用 LLM 最终总结")
    parser.add_argument(
        "--max-enriched-candidates",
        type=int,
        default=0,
        help="仅增强前 N 个候选（0 表示按模式自动决定）",
    )
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--map", action="store_true", help="生成 folium 交互地图 HTML")
    parser.add_argument(
        "--map-output", type=str, default="meethalfway_map.html", help="地图输出路径"
    )
    parser.add_argument(
        "--surprise", action="store_true", help="惊喜模式：推荐一家高分冷门餐厅"
    )
    parser.add_argument("--verbose", action="store_true", help="输出 DEBUG 级别日志")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# 主入口（async）
# ---------------------------------------------------------------------------
async def async_main(args: argparse.Namespace) -> None:
    load_dotenv()

    if args.verbose:
        logging.getLogger("meethalfway").setLevel(logging.DEBUG)

    mapbox_token = os.getenv("MAPBOX_ACCESS_TOKEN", "")
    ors_api_key = (
        os.getenv("OPENROUTESERVICE_API_KEY")
        or os.getenv("ORS_API_KEY")
        or ""
    )
    yelp_api_key = os.getenv("YELP_API_KEY", "").strip() or None
    tavily_key = os.getenv("TAVILY_API_KEY", "")
    openai_key = os.getenv("OPENAI_API_KEY")
    openai_base = os.getenv("OPENAI_API_BASE", "").strip() or None
    openai_model = (
        os.getenv("MODEL_NAME")
        or os.getenv("OPENAI_MODEL")
        or "gpt-4o-mini"
    )

    if not tavily_key:
        logger.info("未配置 TAVILY_API_KEY：将自动使用 DuckDuckGo 无 Key 搜索降级。")

    engine = MeetHalfwayRecommender(
        mapbox_token=mapbox_token,
        ors_api_key=ors_api_key,
        yelp_api_key=yelp_api_key,
        tavily_key=tavily_key,
        openai_key=openai_key,
        openai_model=openai_model,
        openai_base=openai_base,
        transport=args.transport,
        isochrone_minutes=args.isochrone_minutes,
        low_cost_mode=args.low_cost,
        use_yelp=args.enable_yelp,
        use_llm_extraction=True,
        use_llm_summary=args.enable_llm_summary,
        max_enriched_candidates=(args.max_enriched_candidates or None),
    )

    def _resolve_location(
        lat: Optional[float],
        lon: Optional[float],
        address: str,
        person_name: str,
    ) -> Location:
        if lat is not None and lon is not None:
            return Location(lat, lon)

        if address.strip():
            loc = engine.geocode_address(address=address, city_hint=args.city.strip())
            if loc is not None:
                return loc
            raise RuntimeError(f"{person_name} 地址解析失败，请补充更具体地址或改用经纬度。")

        raise RuntimeError(
            f"{person_name} 缺少位置信息：请提供经纬度(--{person_name.lower()}-lat/--{person_name.lower()}-lon)"
            f"或地址(--{person_name.lower()}-address)。"
        )

    a = _resolve_location(args.a_lat, args.a_lon, args.a_address, "A")
    b = _resolve_location(args.b_lat, args.b_lon, args.b_address, "B")

    # Step 1: 加权中点（初始种子）
    center = engine.compute_weighted_midpoint(a, b, args.weight_a, args.weight_b)
    logger.info("加权中心点: (%.6f, %.6f)", center.lat, center.lon)

    # Step 2: 等时线（A 和 B 各一个多边形）
    logger.info("获取等时线多边形 (%d 分钟, %s) ...", args.isochrone_minutes, args.transport)
    iso_a = engine.get_isochrone(a)
    iso_b = engine.get_isochrone(b)
    intersection = engine.compute_intersection(iso_a, iso_b)

    # Step 2b: 从交集中剔除水体、森林等不可实际抵达的自然地物
    intersection = engine.subtract_natural_barriers(intersection)

    # Step 3: 搜索附近餐厅
    limit = engine.recommend_search_limit(args.top_k)
    raw_candidates = engine.search_nearby_venues(
        center=center,
        venue_type=args.venue_type,
        keyword=args.cuisine,
        limit=limit,
    )

    if not raw_candidates:
        venue_display = VENUE_TYPES.get(args.venue_type, {}).get("display", "场所")
        print(f"未找到任何候选{venue_display}，请检查坐标或关键词。")
        return

    # Step 4: 等时线交集标记
    engine.tag_with_isochrone(raw_candidates, intersection)

    # Step 4b: 人流密度 Hard Filter — 剔除 POI 密度不足的偏僻候选
    raw_candidates = engine.filter_by_poi_density(raw_candidates)
    if not raw_candidates:
        venue_display = VENUE_TYPES.get(args.venue_type, {}).get("display", "场所")
        print(f"POI 密度过滤后无候选{venue_display}，请调整地点或关键词。")
        return

    # Step 5: 并发 Web 采集 + LLM 语义提取（零足迹：数据不落盘）
    city_hint = args.city.strip() or "本地"
    await engine.enrich_all_async(
        raw_candidates,
        city_hint=city_hint,
        year_hint=2026,
        time_slot=args.time_slot,
        party_size=max(1, args.party_size),
    )

    # Step 6: 评分排序
    tired_person = args.tired.lower() if args.tired else None
    scored = engine.score_candidates(
        a, b, center, raw_candidates,
        w_dist=0.35, w_rating=0.30, w_pref=0.35,
        tired_person=tired_person,
    )
    top_items = scored[: args.top_k]

    # Step 7: 惊喜推荐
    surprise_pick = engine.pick_surprise(scored) if args.surprise else None

    # Step 8: AI 推荐文本
    summary = engine.generate_recommendation_text(
        a,
        b,
        center,
        top_items,
        args.budget,
        args.cuisine,
        time_slot=args.time_slot,
        party_size=max(1, args.party_size),
        venue_type=args.venue_type,
    )

    # Step 9: 地图
    map_path = ""
    if args.map:
        map_path = engine.generate_map(
            a, b, center, scored, intersection,
            output_path=args.map_output,
            surprise=surprise_pick,
            top_k=args.top_k,
        )

    # 构建结果字典
    result = {
        "meta": {
            "version": "2.0",
            "algorithm": "Mapbox Isochrone Intersection + GPT-4o-mini semantic extraction + exponential fairness penalty",
            "privacy": "零足迹设计：用户 GPS 坐标仅存活于内存计算栈，函数返回后即销毁，不做任何形式的持久化存储。",
        },
        "inputs": {
            "a": {"lat": a.lat, "lon": a.lon},
            "b": {"lat": b.lat, "lon": b.lon},
            "cuisine": args.cuisine,
            "venue_type": args.venue_type,
            "budget": args.budget,
            "transport": args.transport,
            "isochrone_minutes": args.isochrone_minutes,
            "time_slot": args.time_slot,
            "party_size": max(1, args.party_size),
            "tired_person": tired_person,
        },
        "center": {"lat": center.lat, "lon": center.lon},
        "isochrone_intersection_active": intersection is not None,
        "top_candidates": [
            {
                "name": x.name,
                "place_name": x.place_name,
                "lat": x.lat,
                "lon": x.lon,
                "final_score": round(x.final_score, 4),
                "distance_to_center_km": round(x.distance_to_center_km, 3),
                "fairness_delta_km": round(x.fairness_delta_km, 3),
                "fairness_delta_minutes": round(x.fairness_delta_minutes, 1),
                "best_time_slot": x.best_time_slot,
                "availability_overlap": round(x.availability_overlap, 3),
                "radius_tolerance_score": round(x.radius_tolerance_score, 3),
                "venue_popularity_score": round(x.venue_popularity_score, 3),
                "mutual_vote_score": round(x.mutual_vote_score, 3),
                "time_vote_score": round(x.time_vote_score, 3),
                "score_breakdown": x.score_breakdown,
                "in_isochrone_zone": x.in_isochrone_intersection,
                "web_signals": x.web_signals,
            }
            for x in top_items
        ],
        "surprise_pick": (
            {
                "name": surprise_pick.name,
                "place_name": surprise_pick.place_name,
                "score": round(surprise_pick.final_score, 4),
            }
            if surprise_pick
            else None
        ),
        "summary": summary,
        "map_file": map_path,
    }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    # 人类可读输出
    print("=" * 72)
    print("MeetHalfway AI v2 — 推荐结果")
    print("=" * 72)
    venue_display = VENUE_TYPES.get(args.venue_type, {}).get("display", "场所")
    print(f"场所类型    : {venue_display}")
    print(f"均衡中心点  : ({center.lat:.6f}, {center.lon:.6f})")
    print(
        f"等时线交集  : {'已启用 (Mapbox Isochrone)' if intersection is not None else '降级为半径近似模式'}"
    )
    print(f"Web 采集    : {'LLM 语义提取 (GPT-4o-mini)' if engine._openai_ok else '关键词匹配降级'}")
    print(f"候选总数    : {len(scored)}，展示 Top {len(top_items)}")
    print("-" * 72)
    for i, item in enumerate(result["top_candidates"], start=1):
        iso_tag = "等时线内" if item["in_isochrone_zone"] else "边缘区"
        ws = item["web_signals"]
        print(
            f"{i}. [{iso_tag}] {item['name']}\n"
            f"   score={item['final_score']}  时间差={item['fairness_delta_minutes']}分  "
            f"状态={ws.get('status')}  置信={ws.get('confidence', '?')}  "
            f"原因={ws.get('reason', '-')}"
        )
    if surprise_pick:
        print("-" * 72)
        print(
            f"[Surprise Pick] {surprise_pick.name}  (score={round(surprise_pick.final_score, 4)})"
        )
    print("-" * 72)
    print("AI 推荐摘要:")
    print(result["summary"])
    if map_path:
        print(f"\n交互地图已保存: {map_path}")
    print("\n[零足迹声明] 本次计算已完成，用户位置数据未做任何持久化存储。")


def main() -> None:
    args = parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
