# MeetHalfway AI — ACM SIGSPATIAL 2026 Student Research Competition

## The Project
MeetHalfway AI studies a common but under-examined spatial decision: **where two people should meet so that travel effort is fair to both**. Most tools answer with a geometric midpoint, which can place one person across a river, behind a highway, or far longer in travel time than the other. This project reframes the question as one of *spatial fairness*.

The system computes a shared feasible region as the intersection of two travel-time isochrones (OpenRouteService / Mapbox, intersected with Shapely), subtracts natural barriers such as rivers and highways, and measures the **travel-time fairness gap** between the two participants for every candidate venue. Candidates are retrieved from real map data (Mapbox / OpenStreetMap) and ranked by an explainable score that rewards balanced travel time and venue quality while penalizing closures, crowding, and queue risk. The result is a transparent, fairness-aware recommendation rather than an opaque "best guess."

## Why It Fits the Student Research Competition
- **It poses a clear research question:** can isochrone-intersection fairness produce meeting points that are measurably more equitable in travel effort than geometric-midpoint baselines, without sacrificing venue quality?
- **It is student-led and self-contained.** The spatial engine, the fairness formulation, and the interface are my own implementation, making it well suited to the individual-contribution focus of the competition.
- **It sits squarely in SIGSPATIAL's research space:** isochrones, reachability, spatial fairness, and location-based recommendation are core topics of the conference and of urban computing more broadly.
- **It suits the poster-and-talk format**, where a focused problem, a clear method, and visual results (isochrone-overlap maps and fairness comparisons) communicate effectively.

## Why Submit
Fairness in everyday spatial decisions is an important and approachable research direction, and meeting-place selection is a clean setting to study it. Presenting this work at the SIGSPATIAL Student Research Competition places the project in front of researchers who work directly on reachability, accessibility, and spatial recommendation, and offers the chance to receive expert feedback on both the fairness formulation and the broader question of how to make spatial recommendations more equitable and explainable.
