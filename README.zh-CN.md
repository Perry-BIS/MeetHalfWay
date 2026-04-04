<div align="right">

[English](./README.md) | [中文](./README.zh-CN.md)

</div>

# MeetHalfway AI

一个面向双人场景的公平、实时、可解释的约饭选址推荐引擎。

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)
![Streamlit](https://img.shields.io/badge/Streamlit-App-FF4B4B?logo=streamlit&logoColor=white)
![Status](https://img.shields.io/badge/Status-Competition%20Ready-0A7B83)

## 项目亮点

MeetHalfway AI 不只是算地理中点，而是先用等时线交集保证双方都“可达”，再融合营业状态、拥挤与排队风险、口碑评价等信号，输出更接近真实决策场景的推荐结果。

## 为什么有竞争力

- 公平可量化：以双方通勤时间差衡量公平，而不是“看起来居中”。
- 决策更贴近现实：可识别闭店、高峰排队与临时风险。
- 展示完整度高：支持交互地图、Top 推荐、惊喜模式。
- 隐私策略克制：位置数据只在内存中处理，不落盘持久化。

## 核心能力

| 模块 | 设计方式 | 价值 |
|---|---|---|
| 地理公平约束 | 等时线求交 | 候选地点对双方都可达 |
| 智能评分 | 公平性 + 口碑 + 偏好 + 风险惩罚 | 推荐更稳定、更可解释 |
| 实时信号 | 网页检索 + 语义抽取 | 感知营业状态与拥挤变化 |
| 工程韧性 | 异步并发 + 重试 + 降级链 | API 波动下仍可用 |
| 可视化表达 | Streamlit + 交互地图 | 评委更容易快速理解 |

## 产品流程

1. 输入双方位置（地址 / 地图点选 / 隐私分离上传）。
2. 构建双方等时线并计算交集区域。
3. 检索候选餐厅并融合实时状态信号。
4. 输出可解释评分、Top 推荐与地图展示。

## 技术架构

- 前端：Streamlit
- 地理计算：OpenRouteService / Mapbox + Shapely
- 场所检索：Mapbox / OSM Overpass（降级链）
- 实时信号：Tavily / DuckDuckGo（降级链）
- 语义分析：OpenAI 兼容模型（支持关键词降级）
- 并发框架：asyncio + httpx

## 快速开始

### 1）安装依赖

```powershell
pip install -r requirements.txt
```

### 2）配置环境变量

复制 `.env.example` 为 `.env`，并填入你自己的密钥。

推荐配置：
- `OPENROUTESERVICE_API_KEY`

可选增强：
- `MAPBOX_ACCESS_TOKEN`
- `TAVILY_API_KEY`
- `YELP_API_KEY`
- `OPENAI_API_KEY`
- `OPENAI_API_BASE`
- `MODEL_NAME`

### 3）启动应用

```powershell
streamlit run app_streamlit.py
```

## 仓库结构（比赛提交主干）

- `app_streamlit.py`：主可视化入口
- `app_streamlit_new.py`：新版实验入口
- `meethalfway.py`：核心算法与评分逻辑
- `requirements.txt`：依赖清单
- `.env.example`：环境变量模板
- `matlab_python_converted/`：算法迁移与实验模块

## 隐私与安全说明

- 不提交任何真实 API Key（`.env` / secrets 文件已忽略）。
- 不上传本地运行状态与个人痕迹文件。
- 用户坐标仅用于会话内计算，不持久化保存。

## 部署建议

推荐部署到 Streamlit Community Cloud：

1. 连接仓库。
2. 入口文件选择 `app_streamlit.py`。
3. 在平台 Secrets 中配置环境变量。
