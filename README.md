# seed

用批量 Outlook OAuth 账号在 [Flora](https://app.flora.ai) 上跑 Seedance 2.0,自动生成视频并下载到本地。

支持两种运行模式:
- **UI 模式**(默认):Patchright 启动本地 Chrome,以人化操作走完登录 → 上传参考图 → 连线 → 生成 → 下载
- **API 模式**(`--api`):仅用浏览器完成首次登录拿 cookies,后续通过 Flora 内部 HTTP API 直接上传/生成/轮询/下载

## 目录结构

```
.
├── run.py                 # 入口脚本
├── src/
│   ├── accounts.py        # outlook txt 账号文件解析
│   ├── api_client.py      # Flora 内部 HTTP API 封装
│   ├── flora_bot.py       # Patchright 浏览器自动化主体
│   ├── materials.py       # 素材目录扫描:每子目录 = 一个任务
│   ├── proxy_pool.py      # 出口 IP 池(按账号哈希挑)
│   └── runner_api.py      # API 模式 runner
├── tools/                 # HAR 分析、Chrome 启动参数探测等调试脚本
├── outlook/               # 账号文件 / csv→txt 转换(具体 csv/txt 已 gitignore)
├── ceshi/                 # 自留测试笔记
├── .env.example
└── requirements.txt
```

> 视频产物默认输出到 `output/<任务名>/segment-XX.mp4`,日志在 `runs/` 和 `logs/`,均已 gitignore。

## 快速开始

### 1. 安装依赖

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
patchright install chromium
```

### 2. 配置

复制 `.env.example` 为 `.env`,按需修改:

| 变量 | 说明 |
| --- | --- |
| `FLORA_PROJECT_URL` | Flora 项目 URL |
| `ACCOUNTS_FILE` | 账号文件路径(每行 `email----password----clientID----refreshToken`) |
| `MATERIALS_DIR` | 素材根目录,默认 `./素材` |
| `USER_DATA_DIR_PREFIX` | 每个账号独立 Chrome profile 前缀 |
| `HEADLESS` | 是否无头,首次跑务必 `false` |
| `SLOW_MO_MS` | 每步操作延迟毫秒 |
| `GENERATION_TIMEOUT_S` | 单个视频生成最长等待秒数 |
| `PER_ACCOUNT_QUOTA` | 每账号最多生成多少个视频后切换下一个 |

### 3. 准备素材

```
素材/
├── segment-01/
│   ├── ref1.png         # 多张参考图,按自然排序上传
│   ├── ref2.jpg
│   └── prompt.txt       # UTF-8 提示词
├── segment-02/
│   └── ...
```

### 4. 运行

```powershell
# UI 模式 跑全部
python run.py

# 只跑某账号 + 某个任务
python run.py --account 0 --task segment-01

# 列出账号 / 任务
python run.py --list-accounts
python run.py --list-tasks

# API 模式(推荐,稳定)
python run.py --api --account 0

# dry-run:只登录不生成,验证账号可用
python run.py --dry-run --account 0
```

## 常用命令行参数

| 参数 | 说明 |
| --- | --- |
| `--account` | 账号下标(数字)或邮箱前缀 |
| `--task` | 仅跑名字包含该子串的任务 |
| `--api` | API 模式:UI 拿 cookies 后走 HTTP |
| `--grab-key` | 登录后抓 API key 并拦截 technique slug |
| `--record` | 录制模式:手动操作,所有 API 流量录到 HAR + jsonl |
| `--download-existing` | 把项目画布上现存视频下载到 `output/` |
| `--keep-open SEC` | 任务完成后浏览器保持 N 秒供人工操作 |
| `--dry-run` | 只登录不生成 |

## 注意事项

- 登录入口走 `/sign-up?redirect_url=...` 而非 `/sign-in`,新账号才能正常落项目
- 启动 Chrome 必须忽略 `--enable-automation`,否则 Flora 黑屏
- 每个账号在 Flora 是独立 workspace,生成产物在各自项目里
- 同一 Outlook 账号短期内重复登录会被 MS 暂时锁定,建议轮换
- 视频文件、登录态、日志、密钥均已 gitignore,不会上传仓库
