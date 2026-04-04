# MeetHalfway AI

公平、实时、可解释的双人约饭选址引擎。

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)
![Streamlit](https://img.shields.io/badge/Streamlit-App-FF4B4B?logo=streamlit&logoColor=white)
![Status](https://img.shields.io/badge/Status-Competition%20Ready-0A7B83)

## 一句话亮点

MeetHalfway AI 不是简单算几何中点，而是以双方等时线交集为公平约束，结合营业状态、拥挤度、排队风险和口碑信号，给出可落地、可解释、兼顾隐私的见面地点推荐。

## 为什么评委会喜欢

- 公平可量化：用双方通勤时间差衡量公平，不是“看起来居中”。
- 决策更真实：引入实时网页信号，识别闭店、排队高峰和临时风险。
- 体验更完整：支持可视化地图、Top 推荐和惊喜随机餐厅。
- 隐私更克制：位置仅在内存处理，不写库，不落盘。

## 核心能力

| 能力模块 | 设计方式 | 实际价值 |
|---|---|---|
| 地理公平约束 | 等时线求交（Isochrone Intersection） | 候选点对双方都“可达” |
| 智能评分 | 通勤公平 + 口碑 + 偏好 + 风险惩罚 | 推荐结果更平衡、更稳健 |
| 实时信号 | 网页检索 + 语义抽取 | 识别营业与排队变化 |
| 工程韧性 | 并发抓取 + 限流重试 + 多级降级 | API 波动下仍可用 |
| 可视化呈现 | Streamlit + 交互地图 | 结果直观、便于演示 |

## 产品流程

1. 输入双方位置（地址 / 地图点选 / 隐私分离上传）。
2. 生成双方等时线并计算交集区域。
3. 拉取候选餐厅并融合实时状态信息。
4. 输出可解释评分、Top 推荐和地图展示。

## 技术架构

- 前端：Streamlit
- 地理计算：OpenRouteService / Mapbox + Shapely
- 场所检索：Mapbox / OSM Overpass（降级链）
- 实时信息：Tavily / DuckDuckGo（降级链）
- 语义分析：OpenAI 兼容模型（可降级关键词策略）
- 并发框架：asyncio + httpx

## 快速启动

### 1) 安装依赖

```powershell
pip install -r requirements.txt
```

### 2) 配置环境变量

将 .env.example 复制为 .env，并填入你自己的密钥。

必填/推荐项：
- OPENROUTESERVICE_API_KEY（推荐）

可选增强：
- MAPBOX_ACCESS_TOKEN
- TAVILY_API_KEY
- YELP_API_KEY
- OPENAI_API_KEY
- OPENAI_API_BASE
- MODEL_NAME

### 3) 启动可视化应用

```powershell
streamlit run app_streamlit.py
```

## 目录说明（比赛提交主干）

- app_streamlit.py：主可视化入口
- app_streamlit_new.py：新版实验入口
- meethalfway.py：核心算法与评分逻辑
- requirements.txt：依赖清单
- .env.example：环境变量模板
- matlab_python_converted/：算法迁移与实验模块

## 隐私与安全声明

- 不提交任何真实密钥（.env、secrets 文件默认忽略）。
- 不上传本地运行状态与个人痕迹文件。
- 用户坐标仅用于会话内计算，不持久化保存。

## 部署建议

可直接部署到 Streamlit Community Cloud：

1. 连接仓库。
2. 入口文件选择 app_streamlit.py。
3. 在平台 Secrets 中配置环境变量。

## 比赛演示建议

- 演示场景 1：双方距离较远，系统仍给出公平可达的区域。
- 演示场景 2：高峰时段同菜系下，系统自动规避高排队风险门店。
- 演示场景 3：开启 Surprise Me，展示探索型推荐能力。

## 许可证

MIT# MeetHalfway AI — v2.0

> **双人公平约饭推荐引擎**：等时线交集 · LLM 语义分析 · 异步并发 · 零足迹隐私设计

---

## 核心亮点

| 维度 | v1（旧版） | v2（本版） |
|------|-----------|-----------|
| **算法** | 几何中点（经纬度加权平均） | **ORS/Mapbox 等时线交集**，推荐必须落在双方可达区域重叠内 |
| **公平性度量** | 直线距离差（km） | **通行时间差（分钟）**，指数惩罚：$\text{penalty} = e^{\Delta t / 10} - 1$ |
| **AI 提取** | 硬编码关键词匹配 | **GPT-4o-mini 结构化 JSON 提取**，识别委婉停业表达 |
| **并发** | 串行循环（10 家 ≈ 10× 延迟） | **asyncio + httpx 并发 + 限流与指数退避**，速度与稳定性兼顾 |
| **降级** | 无 | Overpass/OSM 场所搜索；DDGS 网页采集；OpenAI → 关键词匹配 |
| **演示** | 纯文本输出 | **folium 交互地图**（HTML）+ Surprise Me 随机推荐 |
| **隐私** | 未说明 | **隐私分离上传 + 零足迹声明**：A/B 各自上传，位置仅保存在服务器内存 |

---

## 快速开始

### 1. 安装依赖

```powershell
pip install -r requirements.txt
```

> `shapely` 和 `folium` 为可选增强依赖。缺失时程序自动降级，不会报错退出。

### 2. 配置 API Keys

复制 `.env.example` 为 `.env` 并填入：

```
MAPBOX_ACCESS_TOKEN=pk.eyJ1...   # 可选，不填将降级 OSM 检索
OPENROUTESERVICE_API_KEY=...
TAVILY_API_KEY=tvly-...         # 可选，不填将降级 DuckDuckGo Search
YELP_API_KEY=...
OPENAI_API_KEY=gsk-...           # 可选，缺失时降级为关键词匹配
OPENAI_API_BASE=https://api.groq.com/openai/v1
MODEL_NAME=llama-3.3-70b-versatile
```

### 2.1 密钥安全（重要）

- 不要在 `app_streamlit.py` / `meethalfway.py` 里硬编码任何 Key 或 Token。
- 本项目已支持优先从 Streamlit Secrets 读取，未配置时再回退 `.env`。
- `.env` 与 `.streamlit/secrets.toml` 应仅用于本地或部署平台密钥注入，勿上传到公开仓库。

Streamlit Cloud 推荐在 `Secrets` 中配置：

```toml
MAPBOX_ACCESS_TOKEN="your_mapbox_token"
OPENROUTESERVICE_API_KEY="your_openrouteservice_key"
TAVILY_API_KEY="your_tavily_key"
YELP_API_KEY="your_yelp_key"
OPENAI_API_KEY="your_openai_key"
OPENAI_API_BASE="https://api.groq.com/openai/v1"
MODEL_NAME="llama-3.3-70b-versatile"
```

### 3. 运行示例

### 可视化平台（推荐，用户只需在界面输入）

```powershell
streamlit run app_streamlit.py
```

启动后你可以：
- 先看到可视化平台界面
- 选择 `隐私分离上传`：A/B 使用同一个房间 ID，各自授权 GPS 上传
- 选择 `地址输入`（两人填地址）
- 或选择 `地图点选`（点击地图分别设置 A/B）
- 配置 `目标时间段` 和 `人数`，系统会自动检索该时段人流量与等位信息
- 点击“开始推荐”后在同一页面看到结果表格与地图

说明：
- 用户不需要在终端输入位置或偏好参数。
- 位置、喜好、时间段、场景都在可视化界面完成。

### CLI（仅开发调试可选）

**基础用法（上海两点间找火锅）**
```powershell
python meethalfway.py `
	--a-lat 31.2304 --a-lon 121.4737 `
	--b-lat 31.2200 --b-lon 121.4500 `
	--cuisine "hotpot" --budget 80 --transport transit --top-k 5
```

**地址输入（无需手动查经纬度）**
```powershell
python meethalfway.py `
	--a-address "上海市静安区南京西路1038号" `
	--b-address "上海市徐汇区肇嘉浜路1111号" `
	--city 上海 --cuisine "火锅" --transport transit --top-k 5 --map
```

**生成交互地图 + 惊喜推荐**
```powershell
python meethalfway.py `
	--a-lat 31.2304 --a-lon 121.4737 `
	--b-lat 31.2200 --b-lon 121.4500 `
	--cuisine "日料" --budget 120 --transport drive `
	--map --surprise --city 上海
```

**疲劳度参数（B 今天更累，中心点向 B 倾斜）**
```powershell
python meethalfway.py `
	--a-lat 31.2304 --a-lon 121.4737 `
	--b-lat 31.2200 --b-lon 121.4500 `
	--cuisine "粤菜" --tired b --isochrone-minutes 25
```

**机器可读 JSON 输出**
```powershell
python meethalfway.py ... --json | ConvertFrom-Json
```

---

## 所有 CLI 参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--a-lat` / `--a-lon` | `` | 出发地 A 的纬度 / 经度（与 `--a-address` 二选一） |
| `--b-lat` / `--b-lon` | `` | 出发地 B 的纬度 / 经度（与 `--b-address` 二选一） |
| `--a-address` / `--b-address` | `` | 出发地 A / B 的地址（可替代经纬度，程序自动解析） |
| `--cuisine` | `hotpot` | 菜系关键词 |
| `--budget` | `80` | 人均预算（元） |
| `--transport` | `transit` | 出行方式：`drive` / `walk` / `transit` |
| `--top-k` | `5` | 最终展示候选数 |
| `--weight-a` / `--weight-b` | `1.0` | 手动权重，越高中心越靠近该方 |
| `--tired` | `` | 疲劳方 `a` 或 `b`，自动调权 |
| `--isochrone-minutes` | `20` | 等时线时间阈值（分钟） |
| `--city` | `` | 城市名（Tavily 搜索上下文） |
| `--time-slot` | `今晚 19:00` | 目标就餐时间段（用于人流量/排队检索） |
| `--party-size` | `2` | 就餐人数（用于等位时长估计） |
| `--map` | `false` | 生成 folium 交互地图 HTML |
| `--map-output` | `meethalfway_map.html` | 地图输出路径 |
| `--surprise` | `false` | 惊喜模式：随机推荐一家高分冷门餐厅 |
| `--json` | `false` | 输出机器可读 JSON |
| `--verbose` | `false` | 输出 DEBUG 级日志（含逐餐厅评分明细） |

---

## 架构说明

### 算法层：等时线交集（GIS）

```
出发地 A ──► ORS/Mapbox Isochrone API ──► 多边形 A (20 min 可达范围)
出发地 B ──► ORS/Mapbox Isochrone API ──► 多边形 B (20 min 可达范围)
																							│
																		Shapely 空间求交
																							│
																		交集多边形（双方均可合理到达）
																							│
													Overpass API 查询 bbox 内水体/森林
													（natural=water/wood, landuse=forest）
																							│
													Shapely.difference() 剔除不可抵达区域
																							│
							候选餐厅 ──► 是否在净交集内？──► 是 → +0.4 分 / 否 → -0.6 分
```

**降级链**：ORS/Mapbox Isochrone 失败 → Shapely 圆形缓冲近似 → 全部通过（保守模式）  
**障碍降级链**：Overpass 查询失败 → 保留原交集 → 差集为空 → 保留原交集（保守模式）

### 场所搜索层：Overpass（无 Key）

- 默认优先 Mapbox；未配置或失败时自动切到 Overpass QL（单次请求）
- Overpass 再失败时，降级 OSM Nominatim
- 遵循公共节点规范：避免高频并发和短时间大量请求

### 网页采集层：DuckDuckGo（无 Key）

- 默认优先 Tavily（配置了 `TAVILY_API_KEY`）
- 未配置或 Tavily 失败时自动切到 `ddgs`
- 搜索结果统一进入 LLM/关键词提取流程，不影响评分链路

### 限流与重试

- Overpass 与 Yelp 增加指数退避重试（429/5xx）
- Yelp 查询增加并发闸门，降低瞬时请求峰值
- 结果表格新增 `web_source`、`fallback_reason`、`fetch_error` 字段，便于演示降级原因

### AI 层：LLM 语义提取

Tavily 搜索结果 → GPT-4o-mini（`response_format: json_object`）→ 结构化字段：

```json
{
	"status": "open | closed | uncertain",
	"promo_bonus": 0.0,
	"queue_level": "low | medium | high | unknown",
	"crowd_index": 0.0,
	"estimated_wait_minutes": 0,
	"risk_penalty": 0.0,
	"confidence": "high | medium | low",
	"reason": "简短说明"
}
```

委婉停业表达（如"老板回老家结婚了，暂别一个月"）会被正确识别为 `closed`。

**降级链**：OpenAI 不可用 → 关键词匹配（中英文双语）

### 工程层：异步并发

```python
async with httpx.AsyncClient() as client:
		tasks = [self._fetch_tavily(client, c, ...) for c in candidates]
		results = await asyncio.gather(*tasks)   # 全部并发，5-10x 提速
```

### 评分公式

$$
	ext{score} = w_{\text{dist}} \cdot d_{\text{center}} + w_{\text{rating}} \cdot r + w_{\text{pref}} \cdot \max\!\left(0,\ 1 - \frac{e^{\Delta t/10}-1}{5}\right) + 0.25 \cdot b_{\text{promo}} - 0.5 \cdot p_{\text{risk}} - p_{\text{crowd}} + b_{\text{iso}}
$$

- $\Delta t$：双方行程时间差（分钟），分母 10 控制惩罚陡度
- $p_{\text{crowd}}$：人流量与等位惩罚（高峰期和多人聚餐惩罚更高）
- $b_{\text{iso}} = +0.4$（等时线交集内）或 $-0.6$（交集外）
- 默认权重：$w_{\text{dist}}=0.35,\ w_{\text{rating}}=0.30,\ w_{\text{pref}}=0.35$

---

## 隐私承诺

本系统采用**零足迹设计**：
- 用户实时 GPS 坐标**不写入任何文件、数据库或日志**
- 所有坐标数据仅存活于 Python 函数调用栈（内存），程序退出后立即回收
- 唯一写入磁盘的是可选的 `meethalfway_map.html`（不含原始坐标，仅含地图瓦片 URL）

隐私分离上传模式：
- A 与 B 在不同设备输入同一房间 ID，各自授权 GPS 上传
- 服务端仅显示上传状态（已上传/未上传），不在前端公开双方原始坐标
- 结果地图可隐藏 A/B 原始点位，仅展示中心与候选餐厅

---

## 所需 API Keys

| Key | 用途 | 是否必须 |
|-----|------|---------|
| `OPENROUTESERVICE_API_KEY` | Isochrone 等时线（主路径） | 推荐 |
| `MAPBOX_ACCESS_TOKEN` | POI 搜索 + 地址解析 + 等时线备选 | 可选 |
| `TAVILY_API_KEY` | 实时网页搜索（营业状态/优惠/排队），未配置则自动降级 DuckDuckGo | 可选 |
| `YELP_API_KEY` | Yelp Fusion 评分与评论量，增强场所评分 | 可选（未配置则不增强） |
| `OPENAI_API_KEY` | Groq/OpenAI 兼容 LLM 语义提取 + 推荐文本 | 可选（降级为关键词） |
| `OPENAI_API_BASE` | OpenAI 兼容网关地址（如 Groq） | 可选 |
| `MODEL_NAME` | 模型名（优先于 `OPENAI_MODEL`） | 可选 |
