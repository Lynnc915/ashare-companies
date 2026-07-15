# A股上市企业信息查询网页

一个纯前端静态网页，用于查看 **2022 年至今 A 股上市企业详细信息**，支持：

- 按股票代码、企业名称、所属行业搜索
- 按**细分板块**筛选（沪市主板、科创板、深市主板、创业板、北交所等）
- 按上市日期范围筛选
- 展示企业 **报告期（上市前 3 年）及最新年度（2025 年）营业总收入和净利润**
- 点击企业行展开详情，查看招股书链接
- 选择板块/时间范围后，页面顶部显示**同期获得受理的 IPO 企业**（来源：东方财富 IPO 数据中心）
- 点击表头排序、分页浏览

## 项目结构

```
ashare-companies/
├── index.html          # 前端网页
├── update_data.py      # 数据抓取脚本
├── data/
│   ├── data.json       # 生成的上市企业数据
│   └── ipo_accepted.json  # 生成的 IPO 获得受理企业数据
├── requirements.txt    # Python 依赖
└── README.md           # 本文件
```

## 使用步骤

### 1. 安装 Python 依赖

在项目目录下执行：

```bash
cd /Users/lynn/ashare-companies
python3 -m pip install -r requirements.txt
```

### 2. 生成数据

默认抓取 2022 年 1 月 1 日至今上市的企业，并抓取 2019–2025 年度财务数据（覆盖报告期及最新年度）：

```bash
python3 update_data.py
```

> 由于需要抓取多年度财务数据，首次运行可能需要 **3–8 分钟**，请耐心等待。

抓取全部 A 股企业（不限上市日期）：

```bash
python3 update_data.py --all
```

指定起始日期：

```bash
python3 update_data.py --since 2023-01-01
```

仅更新企业基础信息，不抓取财务数据（速度更快）：

```bash
python3 update_data.py --no-finance
```

### 3. 打开网页

由于浏览器安全策略，建议通过本地 HTTP 服务器打开：

```bash
python3 -m http.server 8080
```

然后浏览器访问：

```text
http://localhost:8080
```

**不要**直接双击 `index.html` 文件打开，否则浏览器会因安全限制无法读取 `data.json`。

## 网页功能说明

| 功能 | 说明 |
|---|---|
| 搜索 | 股票代码、企业名称、所属行业实时过滤 |
| 板块筛选 | 沪市主板 / 科创板 / 深市主板 / 创业板 / 北交所 / 新三板 |
| 日期筛选 | 按上市日期起止范围过滤 |
| 财务数据 | 表格默认显示最新年度营收和净利润，点击行展开查看报告期（上市前 3 年）及 2025 年最新年度数据 |
| 招股书链接 | 展开企业行后，点击“查找招股说明书”按钮跳转巨潮资讯网搜索页 |
| IPO 受理企业 | 选择板块或时间范围后，页面顶部显示同期获得受理的 IPO 企业，数据来源为东方财富 IPO 数据中心 |
| 排序 | 点击表头按代码、名称、上市日期等排序 |
| 分页 | 每页 50/100/200/500 条可选 |
| 刷新 | 右上角“刷新数据”按钮重新加载 data.json |

## 自动更新数据

### macOS / Linux（crontab）

每天上午 9 点自动更新：

```bash
crontab -e
```

添加一行：

```bash
0 9 * * * cd /Users/lynn/ashare-companies && python3 update_data.py
```

### Windows

使用“任务计划程序”创建一个每天执行一次 `python update_data.py` 的任务。

## 数据来源与注意事项

- 数据来源：[akshare](https://www.akshare.xyz/)
- 财务数据单位为原始人民币元，页面自动换算为“亿元”或“万元”展示
- 部分新上市公司可能没有完整报告期或最新年度财务数据，页面会显示“—”
- 招股说明书链接为巨潮资讯网搜索页跳转，不是直接 PDF 链接
- 本页面仅供学习参考，不构成投资建议

## 技术栈

- Python 3 + akshare（数据抓取）
- HTML5 + CSS3 + Vanilla JavaScript（前端展示）
- 纯静态页面，可部署到 GitHub Pages、Nginx 等任意静态托管服务
