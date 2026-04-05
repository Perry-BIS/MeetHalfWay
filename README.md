<div align="right">

[English](./README.md) | [中文](./README.zh-CN.md)

</div>

# MeetHalfway 

A fair, real-time, and explainable meeting-place recommendation engine for two people.

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)
![Streamlit](https://img.shields.io/badge/Streamlit-App-FF4B4B?logo=streamlit&logoColor=white)
![Status](https://img.shields.io/badge/Status-Competition%20Ready-0A7B83)

<img width="1199" height="1199" alt="image" src="https://github.com/user-attachments/assets/727e6737-c900-47cb-9d3d-e92fe9bb6d35" />


## Why This Project Stands Out

MeetHalfway AI does more than compute a geometric midpoint. It enforces fairness through isochrone intersection (both users can reasonably reach the area), then combines live status signals, crowding/queue risk, and reputation factors into a practical and explainable recommendation.

 not only considers the common factors like "where to meet" and "when to meet," but also incorporates additional criteria such as:  
- Travel radius tolerance,  
- Availability overlap,  
- Venue popularity and density,  
- Mutual voting preferences.  

## Key Highlights

- Fairness by travel time: optimize the balance, not just map distance.
- Real-world decision quality: detect closures, high queue risk, and busy periods.
- End-to-end demo readiness: interactive map, ranked recommendations, and surprise mode.
- Privacy-first workflow: location data is processed in-memory only.

## Core Capabilities

| Module | Design | Value |
|---|---|---|
| Geo Fairness Constraint | Isochrone intersection | Keeps candidates reachable for both sides |
| Intelligent Scoring | Fairness + rating + preference + risk penalties | Produces robust recommendations |
| Live Signals | Web retrieval + semantic extraction | Captures current open/crowd conditions |
| Engineering Resilience | Async concurrency + retries + fallbacks | Stays available under API instability |
| Visual Presentation | Streamlit + interactive map | Easy for judges to understand quickly |

## Product Flow

1. Collect two locations (address / map click / privacy-separated upload).
2. Build isochrones and compute overlap area.
3. Retrieve restaurant candidates and enrich with live signals.
4. Output explainable scores, top picks, and map visualization.

## Tech Stack

- Frontend: Streamlit
- Geo layer: OpenRouteService / Mapbox + Shapely
- Place retrieval: Mapbox / OSM Overpass (fallback chain)
- Live signal retrieval: Tavily / DuckDuckGo (fallback chain)
- Semantic reasoning: OpenAI-compatible model (keyword fallback available)
- Concurrency: asyncio + httpx

## Quick Start

### 1) Install dependencies

```powershell
pip install -r requirements.txt
```

### 2) Configure environment variables

Copy `.env.example` to `.env`, then fill your own keys.

Recommended:
- `OPENROUTESERVICE_API_KEY`

Optional enhancements:
- `MAPBOX_ACCESS_TOKEN`
- `TAVILY_API_KEY`
- `YELP_API_KEY`
- `OPENAI_API_KEY`
- `OPENAI_API_BASE`
- `MODEL_NAME`

### 3) Run the app

```powershell
streamlit run app_streamlit.py
```

## Repository Structure (Competition Core)

- `app_streamlit.py`: main visual app entry
- `app_streamlit_new.py`: new experimental app entry
- `meethalfway.py`: core algorithm and scoring
- `requirements.txt`: dependency list
- `.env.example`: environment template

## Privacy & Security Notes

- No real API keys are committed (`.env` / secrets files are ignored).
- Local runtime traces and personal artifacts are excluded from upload.
- User coordinates are used for in-session computation only and are not persisted.

## Deployment

Recommended target: Streamlit Community Cloud

1. Connect this repository.
2. Set entry file to `app_streamlit.py`.
3. Configure secrets in the platform settings.
