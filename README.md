# 尚香书苑自动签到

基于 GitHub Actions（也可以放 VPS）的尚香书苑 / SXSY 每日自动签到，适配 Discuz! X3.5 的 `sx_signin` 1.1.5 签到插件。

> 旧版脚本用的是 `k_misign` 插件。该插件已经下线，新版签到接口是
> `plugin.php?id=sx_signin:ajax`，并且**签到本身也要过一张 4 位图形验证码**。
> 所以：**即使只用 Cookie 登录，也需要 OCR 服务**。

## 工作流程

```text
https://sxsy.org/ 发布页
        │  读取 JS 里的 latestDomain:"sxsy33.com"
        ▼
  当前可用域名 ──► Cookie / 邮箱密码登录
        │
        ▼
plugin.php?id=sx_signin:index
        │  解析 window.SIGNIN（formhash / ajax / captchaUrl）
        ▼
  取签到验证码 ──► OCR 服务 ──► POST op=signin
        │
        ▼
  code=0 成功 / code=2 已签到 / code=11 验证码错误（换一张重试）
```

## 快速开始

### 1. Fork 仓库

点右上角 `Fork`，把仓库复制到自己的 GitHub 账号。

### 2. 部署 OCR 服务（必须）

新版签到需要识别 4 位图形验证码。仓库里的 [`ocr-shim/`](ocr-shim/) 是一个可以直接用 Docker 跑在 VPS 上的 ddddocr 封装：

- 输入：`POST /ocr`，form 字段 `image=<base64>`（也支持 JSON / 文件上传）
- 输出：`{"code":200,"data":"CJET"}`

详细步骤见 **[ocr-shim/README.md](ocr-shim/README.md)**。最短版本：

```fish
# 在本机把目录传到 VPS（改成你的用户和 IP）
scp -r ocr-shim user@VPS_IP:~/sxsy-ocr

ssh user@VPS_IP
cd ~/sxsy-ocr
printf 'OCR_TOKEN=%s\n' (openssl rand -hex 24) > .env
chmod 600 .env
docker compose up -d --build
curl http://127.0.0.1:8000/health
```

> 如果 `docker` 提示 permission denied，用 `sudo docker compose ...`，或把当前用户加入 `docker` 组后重新登录。

然后在 VPS 防火墙放行 8000 端口，并从**本机**验证公网可达：

```fish
curl http://VPS_IP:8000/health
```

> 建议给 OCR 服务配一个长随机 `OCR_TOKEN`。如果 VPS 有域名，最好再用 Caddy / Nginx 套一层 HTTPS。

### 3. 配置 GitHub Secrets

进入 Fork 后的仓库：`Settings` → `Secrets and variables` → `Actions` → `New repository secret`。

| Secret | 必填 | 说明 |
| --- | :---: | --- |
| `SXSY` | ✅ | Cookie，或 `邮箱&密码`；多个账号用换行分隔 |
| `DDDD_OCR_URL` | ✅（签到必需） | OCR 地址，例如 `http://1.2.3.4:8000/ocr` |
| `DDDD_OCR_TOKEN` | 建议 | 与 OCR 服务的 `OCR_TOKEN` 保持一致 |
| `SXSY_HOST` | 可选 | 强制域名，例如 `sxsy33.com`；自动检测失败时用 |

`SXSY` 支持的格式：

```text
# 单账号 Cookie
u52q_2132_saltkey=xxx; u52q_2132_auth=yyy; u52q_2132_sid=zzz

# 单账号邮箱密码
your_email@example.com&your_password

# 多账号：换行分隔，Cookie 和邮箱密码可以混用
user1@example.com&password1
user2@example.com&password2
u52q_2132_saltkey=...; u52q_2132_auth=...
```

> 密码里如果包含 `&`，只会按**第一个** `&` 分割，后面的 `&` 会保留在密码里。

### 4. 启用并运行

1. 进入仓库的 `Actions` 标签页；
2. 点 `I understand my workflows, go ahead and enable them`；
3. 选择左侧的「尚香书苑自动签到」；
4. 点右侧 `Run workflow` 手动跑一次，先看日志。

手动触发时可以选择模式：

- `run`：正常签到（默认）；
- `diagnose`：只体检域名、登录态、插件、验证码和 OCR，不签到；
- `dry-run`：识别签到验证码但不提交，适合验证 OCR。

日志里正常的关键行：

```text
[获取host] 发布页解析到候选域名: ['sxsy33.com']
[账号1] Cookie 有效，uid=12345
[签到] uid=12345，状态=今日未签到，签到按钮=有，签到验证码=有
[OCR] 识别结果: CJET
[签到] 成功：金钱 +10，连续 2 天，累计 5 天
```

如果当天已经签过：

```text
[签到] 今日已签到，无需重复签到
```

## Cookie 怎么取

1. 浏览器登录**当前域名**（现在通常是 `https://sxsy33.com/`）；
2. 按 `F12` 打开开发者工具 → `Network`；
3. 刷新页面，点第一个 `sxsy33.com` 的文档请求；
4. 在 `Request Headers` 里找到 `Cookie:`，整行复制；
5. 粘贴到 `SXSY` Secret。

注意：

- 一定要在**当前域名**重新登录后再复制。网站换域名后，旧域名下复制的 Cookie 可能不再适用。
- 至少要有 `u52q_2132_auth`、`u52q_2132_saltkey`、`u52q_2132_sid` 这几个 Cookie。
- Cookie 等于账号密码，不要提交到仓库、不要发到公开 issue。

## 定时任务

`.github/workflows/main.yml` 默认：

```yaml
schedule:
  - cron: '5 20 * * *'   # UTC 20:05 = 北京时间次日 04:05
```

GitHub 的定时任务可能延迟几分钟到几十分钟，这是正常现象。想改时间就改 cron（UTC）：

| 北京时间 | UTC cron |
| --- | --- |
| 每天 08:05 | `5 0 * * *` |
| 每天 20:05 | `5 12 * * *` |
| 每天 09:00 和 21:00 | `0 1,13 * * *` |

## 本地运行 / 调试

需要 Python 3.9+ 和 `requests`：

```fish
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 静默读取 Cookie，避免留在 shell 历史里
read -s -P "Cookie: " sxsy
set -x sxsy
set -x DDDD_OCR_URL http://127.0.0.1:8000/ocr
set -x DDDD_OCR_TOKEN your-token

# 只体检：域名、登录态、插件、验证码、OCR 健康状态
.venv/bin/python 尚香书苑.py --diagnose

# 识别验证码但不提交签到（不会消耗当天签到）
.venv/bin/python 尚香书苑.py --dry-run

# 正常签到
.venv/bin/python 尚香书苑.py
```

其他参数：

```text
--host sxsy33.com     强制域名
--max-attempts 8      签到验证码最多尝试次数（默认 6）
--verbose             输出调试日志
```

## 常见问题

### 日志显示「Cookie 无效（discuz_uid=0）」

- Cookie 过期：重新登录后复制；
- Cookie 是从旧域名复制的：到当前域名重新登录再复制；
- 只复制了部分 Cookie：要复制整行 `Cookie:`；
- Cookie 里的 `auth` 被 GitHub 的 Secret 换行/空格截断：重新粘贴。

### 日志显示「需要图形验证码，但未配置 DDDD_OCR_URL」

新版签到必须过验证码，Cookie 登录也一样。按上面的步骤部署 OCR，把 `DDDD_OCR_URL` 配好。

### `[OCR] 识别失败: HTTP 401`

`DDDD_OCR_TOKEN` 与 VPS 上 `.env` 里的 `OCR_TOKEN` 不一致。重新 `cat .env` 对一下。

### 一直「验证码错误」

- 先确认 OCR 服务本身能识别：在 VPS 上 `curl -F image=@某张验证码.png http://127.0.0.1:8000/ocr`；
- 确认 OCR 服务返回的 `data` 是 4 位、大写字母/数字；
- 默认会重试 6 次，每失败一次都会换一张验证码；如果还是不稳，把 `--max-attempts` 调大，或在 `SXSY` 里换用 Cookie；
- 服务器时间偏差太大也会影响验证码（验证码 10 分钟有效），检查 VPS 时间：`timedatectl`。

### 日志出现 Cloudflare / `Just a moment`

GitHub Actions 的出口 IP 可能被 Cloudflare 风控。这是 IP 问题，不是 Cookie 问题。解决办法：

- 把签到脚本也放到 VPS 上跑（推荐，OCR 服务已经在 VPS 上了）；
- 或者给仓库配 self-hosted runner；
- 或者在 `SXSY` 的 Cookie 里带上有效的 `cf_clearance`（但它和 IP 绑定，runner IP 变了通常也没用）。

### 页面没有签到按钮 / 没有 `window.SIGNIN`

- 当前账号的用户组可能不允许签到；
- 插件可能又被换掉了；
- 先用 `--diagnose` 看 `uid`、`status_text`、`has_button`，再把日志发出来。

### 多账号被 IP 限制

插件有「同一 IP 每日最多 N 个账号签到」的限制。GitHub Actions 的 runner IP 是共享的，多账号可能触发限制；这种情况只能分开时间跑，或者改用不同出口 IP。

## 目录结构

```text
shangxiang/
├── 尚香书苑.py                 # 签到脚本（sx_signin + OCR + 多账号）
├── requirements.txt            # 仅 requests
├── .github/workflows/main.yml  # GitHub Actions
├── ocr-shim/                   # ddddocr HTTP 封装（Docker）
│   ├── app.py
│   ├── Dockerfile
│   ├── docker-compose.yml
│   ├── requirements.txt
│   └── README.md
└── README.md
```

## 安全说明

- 脚本不会把 Cookie 写入文件，也不会打印 Cookie；
- OCR 服务建议设置 `OCR_TOKEN`，否则任何知道 IP 的人都能调用你的 OCR；
- `DDDD_OCR_URL` 如果走公网 HTTP，token 是明文传输的；有条件就用 HTTPS；
- 请遵守网站用户协议，控制请求频率，不要拿脚本做签到以外的事情。
