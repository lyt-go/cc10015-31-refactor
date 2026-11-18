# 多维分析实验平台

面向实验设计、指标计算、样本分析、效果评估与结论发布的数据分析实验平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 实验设计 | `experiment` | 实验方案 | 方案编号、实验名称、对照组说明 |
| 样本分群 | `sample_group` | 样本分群 | 分群编号、分群名称、筛选条件 |
| 指标定义 | `metric_def` | 指标定义 | 指标编号、指标名称、计算公式 |
| AB结果分析 | `ab_result` | 分析报告 | 报告编号、实验编号、分析指标 |
| 分群对比 | `segment_compare` | 对比任务 | 任务编号、分群A、分群B |
| 多维下钻 | `drill_down` | 下钻维度 | 维度编号、维度名称、下钻层级 |
| 数据筛选 | `data_filter` | 筛选规则 | 规则编号、数据来源、筛选字段 |
| 归因分析 | `attr_analysis` | 归因任务 | 任务编号、目标指标、候选因子 |
| 异常检测 | `anomaly_detect` | 检测规则 | 规则编号、监控指标、检测算法 |
| 预测模型 | `forecast_model` | 模型配置 | 模型编号、预测对象、历史窗口 |
| 数据导出 | `data_export` | 导出任务 | 任务编号、数据来源、导出格式 |
| 同期群追踪 | `cohort_track` | 同期群定义 | 群组编号、群组名称、首次行为 |
| 可视化配置 | `chart_config` | 图表配置 | 图表编号、图表类型、数据来源 |
| 洞察报告 | `insight_report` | 洞察报告 | 报告编号、分析主题、数据来源 |
| 数据新鲜度 | `data_freshness` | 新鲜度监控 | 监控编号、数据表名、最新分区 |
| 口径注册 | `metric_alias` | 口径版本 | 版本编号、指标编号、变更说明 |
| 实验因子 | `expr_factor` | 因子配置 | 因子编号、因子名称、因子层级 |
| 评估看板 | `evaluate_board` | 看板配置 | 看板编号、看板名称、关联实验 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 指标定义公式的统一计算路径（V031）

指标定义（`metric_def`）与口径注册（`metric_alias`）不再各自解析公式、精度位数和
版本字段，统一收敛到三处：

| 职责 | 实现 |
| --- | --- |
| 公式解析 / 精度解析 / 版本选择 | `backend/app/services/metric_formula.py` |
| V031 核对留档（整批事务） | `backend/app/services/metric_ledger.py` |
| 列表 / 详情 / 消费端读取 | `metric_def`、`metric_alias` 两个服务 |

- **公式**：规范化后编译为受限 AST，只允许四则、取模、乘方与 `abs/min/max/round`；
  历史栏位里的中文描述或非法表达式按原文保留，不覆盖、不阻断列表和留档。
- **精度**：兼容 `4`、`4.0`、`精度4`、`4位` 等历史写法，统一按四舍五入
  （half-up）取位；无法识别时保留原文并回退，保证旧记录仍按原口径读取。
- **版本冲突**：同一指标同时存在历史公式与当前公式时，两种策略二选一：
  - `历史优先`（默认）：历史样例继续得到原口径结果，当前结果仅作对照；
  - `按版本时间`：当前样例使用新口径结果，历史差异单独列出。
- **V031 留档**：`POST /api/metric_def/confirmation` 按策略核对全部指标定义，结果
  整批写入内部表 `_metric_def_v031`；写入过程中任一条复算不一致会整体回滚，
  不会留下半批残留。`GET /api/metric_def/confirmation`、`/confirmation/history`
  供列表页、详情页与口径注册消费端（`GET /api/metric_alias/consumer`）读取同一份
  核对结果，历史批次与当前结果都可回查。

领域：数据工程
子方向：数据分析与数据应用
