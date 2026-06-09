// Build editable MeetHalfway system architecture as PPTX
// Mirrors the RAGTrip-style figure the user shared, but mapped to MeetHalfway
// code (meethalfway.py engine + app_streamlit_new.py UI).
//
// Layout: LAYOUT_WIDE (13.33" x 7.5").
//   - Left:  User icon  ->  bi-arrows  ->  UI box (Streamlit)  ->  Map icon (visualization)
//   - Right: large BACKEND rounded container with 3 colored module boxes
//             [ Spatial | POI | Ranking ]
//   - Labeled arrows: top = forward dataflow, bottom = return path
//
// Everything is native PPT shapes/text so it is fully editable.

const path = require("path");
const React = require("react");
const ReactDOMServer = require("react-dom/server");
const sharp = require("sharp");
const pptxgen = require("pptxgenjs");

const {
  FaUserFriends,
  FaDesktop,
  FaMap,
  FaDrawPolygon,
  FaMapMarkerAlt,
  FaSearchLocation,
  FaBalanceScale,
  FaListOl,
  FaRobot,
} = require("react-icons/fa");

// ---------- icon helpers ----------
function renderIconSvg(IconComponent, color, size) {
  return ReactDOMServer.renderToStaticMarkup(
    React.createElement(IconComponent, { color: color, size: String(size) })
  );
}
async function iconPng(IconComponent, color, size = 384) {
  const svg = renderIconSvg(IconComponent, color, size);
  const buf = await sharp(Buffer.from(svg)).png().toBuffer();
  return "image/png;base64," + buf.toString("base64");
}

// ---------- palette ----------
const C = {
  bgPage:        "FFFFFF",
  title:         "1F2733",
  subtitle:      "6B7686",
  // UI (Streamlit)
  uiFill:        "DCE9F5",
  uiStroke:      "3B6FB6",
  uiText:        "1F3A5F",
  // Backend container
  beFill:        "FBE6CC",
  beStroke:      "D69146",
  beLabel:       "8A5A18",
  // Spatial module (the standout = isochrone intersection, our contribution)
  spatialFill:   "8E7DB3",
  spatialStroke: "5F4D87",
  // POI module
  poiFill:       "EE9C4E",
  poiStroke:     "B86A1C",
  // Ranking module
  rankFill:      "EE9C4E",
  rankStroke:    "B86A1C",
  moduleText:    "FFFFFF",
  moduleSub:     "FDF1E1",
  // Arrows + labels
  arrow:         "3D434D",
  arrowLabel:    "4B5563",
  arrowSub:      "8A93A3",
  // Icon tints
  iconDark:      "263043",
  iconWhite:     "FFFFFF",
  iconUI:        "245293",
  iconMap:       "3E7A4F",
};

(async () => {
  // ---- pre-render icons ----
  const icUser    = await iconPng(FaUserFriends,    C.iconDark);
  const icUI      = await iconPng(FaDesktop,        C.iconUI);
  const icMap     = await iconPng(FaMap,            C.iconMap);
  const icPoly    = await iconPng(FaDrawPolygon,    C.iconWhite);
  const icPin     = await iconPng(FaMapMarkerAlt,   C.iconWhite);
  const icSearch  = await iconPng(FaSearchLocation, C.iconWhite);
  const icScale   = await iconPng(FaBalanceScale,   C.iconWhite);
  const icList    = await iconPng(FaListOl,         C.iconWhite);
  const icBot     = await iconPng(FaRobot,          C.iconWhite);

  // ---- presentation ----
  const pres = new pptxgen();
  pres.layout  = "LAYOUT_WIDE";    // 13.33 x 7.5
  pres.title   = "MeetHalfway system architecture";
  pres.author  = "MeetHalfway demo";

  const slide = pres.addSlide();
  slide.background = { color: C.bgPage };

  // ====================================================================
  // Slide title + caption
  // ====================================================================
  slide.addText("MeetHalfway System Architecture", {
    x: 0.4, y: 0.18, w: 12.5, h: 0.45,
    fontFace: "Calibri", fontSize: 22, bold: true, color: C.title,
    align: "left", margin: 0,
  });
  slide.addText(
    "Participant query  →  isochrones · shared region  →  POI retrieval  →  fairness-aware ranking  →  interactive map",
    {
      x: 0.4, y: 0.62, w: 12.5, h: 0.32,
      fontFace: "Calibri", fontSize: 13, italic: true, color: C.subtitle,
      align: "left", margin: 0,
    }
  );

  // ====================================================================
  // LEFT ZONE: User -> UI -> (Map icon for visualization)
  // ====================================================================

  // User icon + label
  slide.addImage({ data: icUser, x: 0.55, y: 2.95, w: 0.85, h: 0.85 });
  slide.addText("User(s)", {
    x: 0.20, y: 3.85, w: 1.55, h: 0.32,
    fontFace: "Calibri", fontSize: 13, bold: true, color: C.title,
    align: "center", margin: 0,
  });

  // Bi-directional arrows between User and UI
  slide.addShape(pres.shapes.RIGHT_ARROW, {
    x: 1.65, y: 3.18, w: 0.65, h: 0.28,
    fill: { color: C.arrow }, line: { color: C.arrow, width: 0 },
  });
  slide.addShape(pres.shapes.LEFT_ARROW, {
    x: 1.65, y: 3.54, w: 0.65, h: 0.28,
    fill: { color: C.arrow }, line: { color: C.arrow, width: 0 },
  });

  // UI box (Streamlit frontend)
  const uiX = 2.45, uiY = 1.45, uiW = 2.30, uiH = 4.55;
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
    x: uiX, y: uiY, w: uiW, h: uiH,
    fill: { color: C.uiFill },
    line: { color: C.uiStroke, width: 1.75 },
    rectRadius: 0.18,
    shadow: { type: "outer", color: "000000", blur: 8, offset: 2, angle: 90, opacity: 0.10 },
  });
  // UI title inside top
  slide.addText("Streamlit UI", {
    x: uiX + 0.10, y: uiY + 0.18, w: uiW - 0.20, h: 0.40,
    fontFace: "Calibri", fontSize: 16, bold: true, color: C.uiText,
    align: "center", margin: 0,
  });
  // UI icon (desktop) center
  slide.addImage({ data: icUI, x: uiX + (uiW - 1.10) / 2, y: uiY + 0.80, w: 1.10, h: 1.10 });
  // UI body lines
  slide.addText(
    [
      { text: "Inputs",
        options: { bold: true, color: C.uiText, breakLine: true, fontSize: 12 } },
      { text: "locations  ·  travel modes",
        options: { color: C.uiText, breakLine: true, fontSize: 11 } },
      { text: "venue preference  ·  budgets",
        options: { color: C.uiText, breakLine: true, fontSize: 11 } },
      { text: " ",
        options: { breakLine: true, fontSize: 6 } },
      { text: "Outputs",
        options: { bold: true, color: C.uiText, breakLine: true, fontSize: 12 } },
      { text: "ranked venue cards",
        options: { color: C.uiText, breakLine: true, fontSize: 11 } },
      { text: "interactive Folium map",
        options: { color: C.uiText, fontSize: 11 } },
    ],
    {
      x: uiX + 0.18, y: uiY + 2.05, w: uiW - 0.36, h: 2.30,
      fontFace: "Calibri", align: "center", valign: "top", margin: 0,
    }
  );

  // UI tag at the bottom
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
    x: uiX + (uiW - 1.0) / 2, y: uiY + uiH - 0.42, w: 1.0, h: 0.30,
    fill: { color: C.uiStroke }, line: { color: C.uiStroke, width: 0 },
    rectRadius: 0.06,
  });
  slide.addText("FRONTEND", {
    x: uiX + (uiW - 1.0) / 2, y: uiY + uiH - 0.42, w: 1.0, h: 0.30,
    fontFace: "Calibri", fontSize: 10, bold: true, color: "FFFFFF",
    align: "center", valign: "middle", margin: 0, charSpacing: 2,
  });

  // Map icon below UI (for the "Visualization" return path)
  const mapY = 6.30;
  slide.addImage({ data: icMap, x: uiX + (uiW - 0.85) / 2, y: mapY, w: 0.85, h: 0.70 });
  slide.addText("Map visualization", {
    x: uiX - 0.10, y: mapY + 0.72, w: uiW + 0.20, h: 0.28,
    fontFace: "Calibri", fontSize: 10.5, italic: true, color: C.subtitle,
    align: "center", margin: 0,
  });

  // ====================================================================
  // RIGHT ZONE: BACKEND container
  // ====================================================================
  const beX = 5.30, beY = 1.10, beW = 7.70, beH = 5.50;
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
    x: beX, y: beY, w: beW, h: beH,
    fill: { color: C.beFill },
    line: { color: C.beStroke, width: 1.75 },
    rectRadius: 0.20,
    shadow: { type: "outer", color: "000000", blur: 8, offset: 2, angle: 90, opacity: 0.10 },
  });
  // BACKEND label, bottom-right inside the container (matches RAGTrip placement)
  slide.addText("BACKEND  ·  meethalfway.py", {
    x: beX + beW - 3.30, y: beY + beH - 0.50, w: 3.10, h: 0.32,
    fontFace: "Calibri", fontSize: 11, bold: true, italic: true, color: C.beLabel,
    align: "right", margin: 0, charSpacing: 1.5,
  });

  // ----- Module boxes (3 across) -----
  const modY = beY + 0.85;
  const modH = 3.80;
  const modW = 2.10;
  const gap  = 0.40;
  const startX = beX + 0.30;
  const mod = [
    {
      x: startX,
      title: "Spatial module",
      sub:   "Isochrones  +  Intersection",
      bullets: [
        "Per-user reachable area",
        "Shared feasible region",
        "Natural-barrier subtraction",
      ],
      svc: "ORS  ·  Mapbox",
      fill: C.spatialFill, stroke: C.spatialStroke,
      icons: [icPoly],
    },
    {
      x: startX + (modW + gap),
      title: "POI module",
      sub:   "Retrieval  +  Enrichment",
      bullets: [
        "Foursquare / Overpass / Nominatim",
        "Live signals (Tavily / DDG)",
        "LLM semantic extraction",
      ],
      svc: "Foursquare  ·  OSM  ·  LLM",
      fill: C.poiFill, stroke: C.poiStroke,
      icons: [icPin, icSearch],
    },
    {
      x: startX + 2 * (modW + gap),
      title: "Ranking module",
      sub:   "Fairness  +  Explanation",
      bullets: [
        "Travel-time fairness gap",
        "Isochrone-membership reward",
        "Score breakdown + reason",
      ],
      svc: "MCDM  ·  LLM summary",
      fill: C.rankFill, stroke: C.rankStroke,
      icons: [icScale, icList],
    },
  ];

  mod.forEach((m) => {
    // module body
    slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
      x: m.x, y: modY, w: modW, h: modH,
      fill: { color: m.fill },
      line: { color: m.stroke, width: 1.5 },
      rectRadius: 0.14,
      shadow: { type: "outer", color: "000000", blur: 5, offset: 1.5, angle: 90, opacity: 0.12 },
    });
    // title
    slide.addText(m.title, {
      x: m.x + 0.10, y: modY + 0.18, w: modW - 0.20, h: 0.35,
      fontFace: "Calibri", fontSize: 14, bold: true, color: C.moduleText,
      align: "center", margin: 0,
    });
    // subtitle (smaller, lighter)
    slide.addText(m.sub, {
      x: m.x + 0.10, y: modY + 0.55, w: modW - 0.20, h: 0.30,
      fontFace: "Calibri", fontSize: 10.5, italic: true, color: C.moduleSub,
      align: "center", margin: 0,
    });
    // icons row
    const iconSize = 0.55;
    const iconsTotalW = m.icons.length * iconSize + (m.icons.length - 1) * 0.15;
    let ix = m.x + (modW - iconsTotalW) / 2;
    const iconsY = modY + 0.95;
    m.icons.forEach((ic) => {
      slide.addImage({ data: ic, x: ix, y: iconsY, w: iconSize, h: iconSize });
      ix += iconSize + 0.15;
    });
    // bullet body
    slide.addText(
      m.bullets.map((b, i) => ({
        text: b,
        options: { bullet: { code: "25AA" }, breakLine: i < m.bullets.length - 1 },
      })),
      {
        x: m.x + 0.18, y: modY + 1.70, w: modW - 0.30, h: 1.55,
        fontFace: "Calibri", fontSize: 10.5, color: C.moduleText,
        align: "left", valign: "top", margin: 0, paraSpaceAfter: 4,
      }
    );
    // external-services strip at bottom of the module
    slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
      x: m.x + 0.15, y: modY + modH - 0.55, w: modW - 0.30, h: 0.38,
      fill: { color: "FFFFFF", transparency: 15 },
      line: { color: "FFFFFF", width: 0.5 },
      rectRadius: 0.06,
    });
    slide.addText(m.svc, {
      x: m.x + 0.15, y: modY + modH - 0.55, w: modW - 0.30, h: 0.38,
      fontFace: "Calibri", fontSize: 9.5, bold: true, color: C.moduleText,
      align: "center", valign: "middle", margin: 0,
    });
  });

  // ====================================================================
  // ARROWS (UI <-> backend, inter-module, return paths, visualization)
  // ====================================================================

  // ---- 1. Forward arrow: UI -> backend top entry (lands on Spatial module)
  const fwdY = modY + 0.30;                // entry near top of Spatial
  const fwdX1 = uiX + uiW + 0.02;
  const fwdX2 = mod[0].x - 0.02;
  slide.addShape(pres.shapes.RIGHT_ARROW, {
    x: fwdX1, y: fwdY, w: fwdX2 - fwdX1, h: 0.30,
    fill: { color: C.arrow }, line: { color: C.arrow, width: 0 },
  });
  slide.addText("Query", {
    x: fwdX1, y: fwdY - 0.32, w: fwdX2 - fwdX1, h: 0.24,
    fontFace: "Calibri", fontSize: 11, bold: true, color: C.arrowLabel,
    align: "center", margin: 0,
  });
  slide.addText("locations · modes", {
    x: fwdX1 - 0.10, y: fwdY + 0.32, w: (fwdX2 - fwdX1) + 0.20, h: 0.22,
    fontFace: "Calibri", fontSize: 9.5, italic: true, color: C.arrowSub,
    align: "center", margin: 0,
  });

  // ---- 2. Internal arrows between modules (short single-word labels — gap is narrow)
  function interModuleArrow(fromIdx, toIdx, topLabel) {
    const x1 = mod[fromIdx].x + modW;
    const x2 = mod[toIdx].x;
    const yA = modY + 1.95;
    slide.addShape(pres.shapes.RIGHT_ARROW, {
      x: x1 + 0.005, y: yA, w: x2 - x1 - 0.01, h: 0.24,
      fill: { color: C.arrow }, line: { color: C.arrow, width: 0 },
    });
    if (topLabel) {
      slide.addText(topLabel, {
        x: x1 - 0.05, y: yA - 0.32, w: (x2 - x1) + 0.10, h: 0.22,
        fontFace: "Calibri", fontSize: 10, bold: true, color: C.arrowLabel,
        align: "center", margin: 0,
      });
    }
  }
  interModuleArrow(0, 1, "Region");
  interModuleArrow(1, 2, "POIs");

  // ---- 3. Return arrow: backend -> UI (bottom of backend back to UI)
  const retY = modY + modH - 0.95;
  const retX1 = mod[0].x - 0.02;
  const retX2 = uiX + uiW + 0.02;
  slide.addShape(pres.shapes.LEFT_ARROW, {
    x: retX2, y: retY, w: retX1 - retX2, h: 0.30,
    fill: { color: C.arrow }, line: { color: C.arrow, width: 0 },
  });
  slide.addText("Ranked venues", {
    x: retX2, y: retY - 0.32, w: retX1 - retX2, h: 0.24,
    fontFace: "Calibri", fontSize: 11, bold: true, color: C.arrowLabel,
    align: "center", margin: 0,
  });
  slide.addText("JSON  ·  scores  ·  reasons", {
    x: retX2 - 0.10, y: retY + 0.32, w: (retX1 - retX2) + 0.20, h: 0.22,
    fontFace: "Calibri", fontSize: 9.5, italic: true, color: C.arrowSub,
    align: "center", margin: 0,
  });

  // ---- 4. Visualization arrow: from Spatial module bottom -> Map icon under UI
  // Approx route: short vertical down + long horizontal left
  const vizSrcX = mod[0].x + 0.4;
  const vizSrcY = modY + modH + 0.08;
  const vizDstX = uiX + uiW / 2;
  const vizDstY = mapY + 0.30;
  // vertical down
  slide.addShape(pres.shapes.LINE, {
    x: vizSrcX, y: vizSrcY, w: 0, h: (vizDstY - vizSrcY),
    line: { color: C.arrow, width: 2 },
  });
  // horizontal left (with arrowhead)
  slide.addShape(pres.shapes.LEFT_ARROW, {
    x: vizDstX, y: vizDstY, w: (vizSrcX - vizDstX) + 0.02, h: 0.26,
    fill: { color: C.arrow }, line: { color: C.arrow, width: 0 },
  });
  // Label on the visualization arrow
  slide.addText("Visualization", {
    x: vizDstX + 0.30, y: vizDstY - 0.30, w: (vizSrcX - vizDstX), h: 0.22,
    fontFace: "Calibri", fontSize: 10.5, bold: true, italic: true, color: C.arrowLabel,
    align: "center", margin: 0,
  });
  slide.addText("reachable areas · shared region · pins", {
    x: vizDstX + 0.10, y: vizDstY + 0.28, w: (vizSrcX - vizDstX) + 0.20, h: 0.22,
    fontFace: "Calibri", fontSize: 9, italic: true, color: C.arrowSub,
    align: "center", margin: 0,
  });

  // ====================================================================
  // Figure caption (bottom of slide)
  // ====================================================================
  slide.addText("Figure: The MeetHalfway system architecture", {
    x: 0.4, y: 7.22, w: 12.5, h: 0.28,
    fontFace: "Calibri", fontSize: 12, bold: true, color: C.title,
    align: "center", margin: 0,
  });

  // ---- write file ----
  const outPath = "D:/Meet halfway/MeetHalfwayAI/paper/sigspatial2026_demo/figures/system_architecture.pptx";
  await pres.writeFile({ fileName: outPath });
  console.log("WROTE", outPath);
})().catch((e) => { console.error("ERROR", e); process.exit(1); });
