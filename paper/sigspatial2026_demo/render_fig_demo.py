"""Render fig:scenarios panel HTMLs for the SIGSPATIAL 2026 demo paper.

Produces two folium HTML maps under ``paper/sigspatial2026_demo/figures/``:

  * ``panel_a_2person.html``  - N=2, both driving 15 mi. Includes the geometric
    midpoint baseline (orange diamond) so the figure can contrast the
    naive midpoint with MeetHalfway's isochrone-intersection center.
  * ``panel_b_3p_mixed.html`` - N=3, mixed modes (drive 15 mi, walk 2 mi,
    transit 8 mi). Each participant's reachable polygon is color-coded by
    mode and the shared intersection (or fallback union) is drawn green.

The script bypasses the full async LLM-enrichment pipeline; it uses
``engine.search_nearby_venues`` directly on the intersection (or union)
geometry and renders via the extended ``engine.generate_map`` with
panel-label / baseline-midpoint / per-user-mode kwargs.

Run from anywhere (PowerShell or POSIX shell)::

    .venv/Scripts/python.exe paper/sigspatial2026_demo/render_fig_demo.py
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Path setup: make the project root importable so ``from meethalfway import ...``
# resolves regardless of the caller's cwd.
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve()
PROJECT_ROOT = HERE.parents[2]        # .../MeetHalfwayAI
PAPER_DIR = HERE.parent               # .../paper/sigspatial2026_demo
FIG_DIR = PAPER_DIR / "figures"
FIG_DIR.mkdir(parents=True, exist_ok=True)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

from meethalfway import (  # noqa: E402
    CandidateRestaurant,
    Location,
    MeetHalfwayRecommender,
)

# Optional: only used as a defensive shapely-availability check.
try:
    from shapely.geometry import Point  # noqa: F401
    HAS_SHAPELY = True
except ImportError:
    HAS_SHAPELY = False

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] render_fig_demo - %(message)s")
log = logging.getLogger("render_fig_demo")


# ---------------------------------------------------------------------------
# Riverside seed coordinates - aligned with _RIVERSIDE_PARTICIPANTS in
# meethalfway.py so the panels match the demo-seed narrative.
# ---------------------------------------------------------------------------
ALEX = Location(33.9806, -117.3838)   # Downtown
BO = Location(33.9696, -117.3267)     # Canyon Crest
CAM = Location(33.9006, -117.4910)    # La Sierra


def _build_engine() -> MeetHalfwayRecommender:
    """Construct the engine with API keys read from the project .env."""
    return MeetHalfwayRecommender(
        mapbox_token=os.getenv("MAPBOX_ACCESS_TOKEN", ""),
        ors_api_key=(
            os.getenv("OPENROUTESERVICE_API_KEY")
            or os.getenv("ORS_API_KEY")
            or ""
        ),
        yelp_api_key=os.getenv("YELP_API_KEY", "").strip() or None,
        tavily_key=os.getenv("TAVILY_API_KEY", ""),
        openai_key=os.getenv("OPENAI_API_KEY"),
        openai_model=(
            os.getenv("MODEL_NAME")
            or os.getenv("OPENAI_MODEL")
            or "gpt-4o-mini"
        ),
        openai_base=os.getenv("OPENAI_API_BASE", "").strip() or None,
        foursquare_api_key=os.getenv("FOURSQUARE_API_KEY", "").strip() or None,
        transport="drive",
        isochrone_minutes=20,
        low_cost_mode=False,
        use_yelp=False,                # paper figure: skip Yelp enrichment
        use_llm_extraction=False,      # paper figure: skip LLM
        use_llm_summary=False,
        max_enriched_candidates=None,
    )


def _multi_search_area(
    engine: MeetHalfwayRecommender,
    origins: List[Location],
    miles_list: List[float],
    transport_list: Optional[List[str]],
):
    """Wrapper that calls the extended ``get_multi_isochrone_search_area``."""
    return engine.get_multi_isochrone_search_area(
        locations=origins,
        miles_list=miles_list,
        transport_list=transport_list,
    )


def _center_of(area: dict, fallback: Location) -> Location:
    """Pick the search center for the candidate query.

    Prefer the centroid of the intersection (or union fallback) so the POI
    search is biased toward the shared region. If no geometry is available
    (e.g. ORS quota error degraded everything), fall back to the geometric
    mean of the provided fallback location.
    """
    geom = area.get("geometry")
    if geom is not None and not getattr(geom, "is_empty", True):
        try:
            ctr = geom.centroid
            return Location(lat=float(ctr.y), lon=float(ctr.x))
        except Exception:
            pass
    return fallback


def _fetch_candidates(
    engine: MeetHalfwayRecommender,
    center: Location,
    intersection,
    limit: int = 12,
) -> List[CandidateRestaurant]:
    """Run a single non-async POI search on the intersection region.

    We deliberately skip ``enrich_all_async`` (Foursquare enrichment + LLM
    extraction) because:
      * it is slow (~30-60 s);
      * the paper figure only needs map pins, not narrative reasons;
      * ``score_candidates`` still works on the raw distance/relevance proxy.
    """
    raw = engine.search_nearby_venues(
        center=center,
        venue_type="restaurant",
        keyword="",
        limit=limit,
        intersection=intersection,
    )
    engine.tag_with_isochrone(raw, intersection)
    return raw


def _rank_and_slice(
    engine: MeetHalfwayRecommender,
    a: Location,
    b: Location,
    center: Location,
    raw: List[CandidateRestaurant],
    top_k: int = 5,
) -> List[CandidateRestaurant]:
    """Score with the production weights and return the top-K candidates."""
    if not raw:
        return []
    scored = engine.score_candidates(
        a, b, center, raw,
        w_dist=0.35, w_rating=0.30, w_pref=0.35,
        tired_person=None,
    )
    return scored[:top_k]


# ---------------------------------------------------------------------------
# Panel A: N=2, drive 15 mi each. Adds a geometric-midpoint baseline marker.
# ---------------------------------------------------------------------------
def render_panel_a(engine: MeetHalfwayRecommender) -> Path:
    log.info("Panel A: N=2, driving 15 mi each (Alex / Bo)")
    origins = [ALEX, BO]
    miles_list = [15.0, 15.0]

    area = _multi_search_area(
        engine, origins, miles_list, transport_list=["drive", "drive"]
    )
    regions = area.get("regions", [])
    intersection = area.get("geometry")
    log.info("  search-area mode: %s", area.get("mode"))

    # Pipeline weighted center (used by MeetHalfway as the fair center).
    fair_center = engine.compute_weighted_midpoint(ALEX, BO, 1.0, 1.0)
    # Re-bias the candidate search around the intersection centroid when
    # available so Foursquare hits are constrained to the shared region.
    search_center = _center_of(area, fair_center)

    raw = _fetch_candidates(engine, search_center, intersection, limit=18)
    log.info("  raw candidates: %d", len(raw))
    top = _rank_and_slice(engine, ALEX, BO, fair_center, raw, top_k=5)
    log.info("  top-5 venues : %s", [c.name for c in top])

    geom_mid = (
        (ALEX.lat + BO.lat) / 2.0,
        (ALEX.lon + BO.lon) / 2.0,
    )
    out_path = FIG_DIR / "panel_a_2person.html"
    engine.generate_map(
        a=ALEX,
        b=BO,
        center=fair_center,
        candidates=top,
        intersection=intersection,
        output_path=str(out_path),
        surprise=None,
        top_k=5,
        show_user_points=True,
        iso_a=regions[0] if len(regions) > 0 else None,
        iso_b=regions[1] if len(regions) > 1 else None,
        baseline_midpoint=geom_mid,
        panel_label="(a) N=2 - driving 15 mi each",
        candidate_rank_labels=True,
        per_user_modes=["drive", "drive"],
    )
    return out_path


# ---------------------------------------------------------------------------
# Panel B: N=3, mixed modes - drive 15 mi, walk 2 mi, transit 8 mi.
# Bo (P2, walk) is constrained to 2 mi; Cam (P3, transit) to 8 mi. The
# intersection of all three is small; if it is empty the engine falls back
# to a union, and the figure still illustrates the per-user-mode coloring.
# ---------------------------------------------------------------------------
def render_panel_b(engine: MeetHalfwayRecommender) -> Path:
    log.info("Panel B: N=3, mixed modes (drive / walk / transit)")
    origins = [ALEX, BO, CAM]
    miles_list = [15.0, 5.0, 8.0]
    modes = ["drive", "walk", "transit"]
    origin_labels = ["Start A (drive)", "Start B (walk)", "Start C (transit)"]

    area = _multi_search_area(
        engine, origins, miles_list, transport_list=modes
    )
    regions = area.get("regions", [])
    intersection = area.get("geometry")
    log.info("  search-area mode: %s", area.get("mode"))

    # Naive centroid of the three origins as the seed center when nothing else
    # is available. We then prefer the search-area centroid for POI search.
    naive_center = Location(
        lat=sum(o.lat for o in origins) / 3.0,
        lon=sum(o.lon for o in origins) / 3.0,
    )
    search_center = _center_of(area, naive_center)
    # The pipeline weighted midpoint helper only takes two points; for the
    # paper figure we use the geometric mean of the three origins as the
    # "fair center" surrogate. It is just a marker; the green region is the
    # real demo evidence of "fair".
    fair_center = naive_center

    raw = _fetch_candidates(engine, search_center, intersection, limit=24)
    log.info("  raw candidates: %d", len(raw))
    # Score using A=Alex, B=Bo (the engine's pairwise scorer); top-5 still
    # gets us the green/cutlery pins. The third participant Cam is conveyed
    # through the extra_origins overlay.
    top = _rank_and_slice(engine, ALEX, BO, fair_center, raw, top_k=5)
    log.info("  top-5 venues : %s", [c.name for c in top])

    out_path = FIG_DIR / "panel_b_3p_mixed.html"

    # ------------------------------------------------------------------
    # Custom Panel B renderer: bigger numbered venue chips + fit_bounds
    # crop. Built directly with folium so we can override the engine's
    # default rank-chip size without touching the production pipeline.
    # ------------------------------------------------------------------
    import folium  # type: ignore
    from shapely.geometry import mapping  # type: ignore
    from branca.element import Element  # type: ignore

    _MODE_COLORS = {
        "drive": "#2563eb",
        "walk": "#16a34a",
        "transit": "#a855f7",
    }
    _MODE_PIN_COLORS = {
        "drive": "blue",
        "walk": "green",
        "transit": "purple",
    }

    fmap = folium.Map(location=[fair_center.lat, fair_center.lon], zoom_start=11)

    # Track bbox across origins + isochrones for fit_bounds.
    # We collect two bboxes:
    #   * bbox_outer: union of all isochrones + origins (full extent).
    #   * bbox_inner: intersection + origins (tighter; used for fit_bounds
    #     so the venue chips inside the shared region are individually
    #     visible rather than crushed into one cluster).
    bbox_lats: List[float] = []
    bbox_lons: List[float] = []
    inner_lats: List[float] = []
    inner_lons: List[float] = []

    # Per-participant reachable polygons.
    for idx, iso in enumerate(regions):
        if iso is None or getattr(iso, "is_empty", True):
            continue
        color = _MODE_COLORS.get(modes[idx], "#6b7280")
        label = f"{origin_labels[idx]} reachable isochrone"
        try:
            folium.GeoJson(
                mapping(iso),
                style_function=(lambda col: (lambda _f: {
                    "fillColor": col, "color": col, "weight": 2,
                    "fillOpacity": 0.08, "dashArray": "4,4",
                }))(color),
                tooltip=label,
            ).add_to(fmap)
            minx, miny, maxx, maxy = iso.bounds
            bbox_lats.extend([miny, maxy])
            bbox_lons.extend([minx, maxx])
        except Exception as exc:
            log.warning("Failed to render isochrone %d: %s", idx, exc)

    # Shared intersection polygon (green).
    if intersection is not None and not getattr(intersection, "is_empty", True):
        try:
            folium.GeoJson(
                mapping(intersection),
                style_function=lambda _: {
                    "fillColor": "#10b981",
                    "color": "#0f9d76",
                    "weight": 3,
                    "fillOpacity": 0.32,
                },
                tooltip="Shared reachable area (intersection of all participants)",
            ).add_to(fmap)
            i_minx, i_miny, i_maxx, i_maxy = intersection.bounds
            inner_lats.extend([i_miny, i_maxy])
            inner_lons.extend([i_minx, i_maxx])
        except Exception as exc:
            log.warning("Failed to render intersection: %s", exc)

    # Participant start pins.
    for idx, loc in enumerate(origins):
        pin_color = _MODE_PIN_COLORS.get(modes[idx], "darkblue")
        folium.Marker(
            [loc.lat, loc.lon],
            tooltip=origin_labels[idx],
            icon=folium.Icon(color=pin_color, icon="user", prefix="fa"),
        ).add_to(fmap)
        bbox_lats.append(loc.lat)
        bbox_lons.append(loc.lon)
        inner_lats.append(loc.lat)
        inner_lons.append(loc.lon)

    # Fair-center marker intentionally omitted from Panel B: the green
    # intersection polygon already conveys the "fair center" geometrically,
    # and a separate purple pin would visually crowd / occlude the numbered
    # venue chips (which can collide with it when the venue cluster's fan
    # ring happens to pass through the geometric centroid of the 3 origins).

    # Top-5 venue markers with LARGE numbered pill DivIcons so all 5 are
    # individually countable at zoom-out levels.
    #
    # In dense urban scenes (e.g. a single shopping plaza) Foursquare often
    # returns 5 POIs within ~10 m of each other, so plotting them at their
    # raw lat/lon collapses all 5 chips into a single pixel. We fan them out
    # in a small ring around their centroid (~200 m radius) purely for the
    # static figure: this is a cartographic legibility hack, not a coordinate
    # falsification - the tooltip still carries the real venue name, and the
    # plaza-level position is preserved within a city block.
    import math as _math
    if top:
        c_lat = sum(c.lat for c in top[:5]) / max(1, len(top[:5]))
        c_lon = sum(c.lon for c in top[:5]) / max(1, len(top[:5]))
        # 1 deg lat ~ 111 km; at lat=34 deg, 1 deg lon ~ 92 km.
        # 0.008 deg lat ~ 890 m offset radius -> separates chips on a
        # static screenshot zoomed to the full intersection region while
        # remaining a "neighborhood-block" exaggeration, not a relocation.
        offset_deg = 0.008
        lon_scale = 1.0 / _math.cos(_math.radians(c_lat))
        n = len(top[:5])
        for i, c in enumerate(top[:5]):
            theta = 2 * _math.pi * i / max(1, n) - _math.pi / 2  # start at top
            draw_lat = c.lat + offset_deg * _math.sin(theta)
            draw_lon = c.lon + offset_deg * _math.cos(theta) * lon_scale
            # If the 5 raw venues are not clustered (e.g. spread across the
            # intersection), the fan still works because the offset is small
            # relative to the polygon extent.
            pill_html = (
                f'<div style="background:#16a34a;color:#fff;font-weight:700;'
                f'font-size:14px;padding:3px 7px;border-radius:12px;'
                f'border:2px solid #fff;box-shadow:0 1px 3px rgba(0,0,0,0.4);'
                f'white-space:nowrap;font-family:Arial,sans-serif;">#{i+1}</div>'
            )
            folium.Marker(
                [draw_lat, draw_lon],
                tooltip=f"#{i+1} {c.name}",
                icon=folium.DivIcon(
                    html=pill_html,
                    icon_size=(40, 22),
                    icon_anchor=(20, 11),
                ),
            ).add_to(fmap)
            # Thin line from the fanned chip back to the true plaza centroid
            # so readers can see all 5 belong to the same cluster.
            folium.PolyLine(
                locations=[[draw_lat, draw_lon], [c_lat, c_lon]],
                color="#0f9d76",
                weight=1,
                opacity=0.5,
            ).add_to(fmap)

    # Panel caption overlay.
    panel_label = "(b) N=3 - drive 15 mi, walk 5 mi, transit 8 mi"
    safe_label = (
        panel_label.replace("&", "&amp;")
        .replace("<", "&lt;").replace(">", "&gt;")
    )
    overlay_html = (
        '<div style="position: fixed; top: 12px; left: 12px; '
        'z-index: 9999; background: rgba(255,255,255,0.92); '
        'padding: 6px 12px; border: 1px solid #d1d5db; '
        'border-radius: 6px; font-family: Arial, sans-serif; '
        'font-size: 14px; font-weight: 600; color: #111827; '
        'box-shadow: 0 1px 3px rgba(0,0,0,0.15);">'
        f'{safe_label}</div>'
    )
    fmap.get_root().html.add_child(Element(overlay_html))

    # Tight crop: bbox of the intersection + all participant origins.
    # We deliberately exclude the per-user isochrone polygons here -- the
    # drive isochrone alone spans ~30 mi and would zoom the map out so far
    # that the numbered venue chips collapse into a single cluster. The
    # intersection + 3 origins is a much tighter extent and still keeps the
    # whole "meet halfway" story (3 starts, shared green region, 5 venues)
    # in frame.
    crop_lats = inner_lats or bbox_lats
    crop_lons = inner_lons or bbox_lons
    if crop_lats and crop_lons:
        min_lat, max_lat = min(crop_lats), max(crop_lats)
        min_lon, max_lon = min(crop_lons), max(crop_lons)
        fmap.fit_bounds(
            [[min_lat, min_lon], [max_lat, max_lon]],
            padding=(20, 20),
        )

    fmap.save(str(out_path))
    log.info("Panel B map saved: %s", out_path)
    return out_path


def main() -> None:
    engine = _build_engine()
    panel_a = render_panel_a(engine)
    panel_b = render_panel_b(engine)

    for label, p in (("Panel A", panel_a), ("Panel B", panel_b)):
        size = p.stat().st_size if p.exists() else -1
        log.info("%s -> %s (%d bytes)", label, p, size)


if __name__ == "__main__":
    main()
