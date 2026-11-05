# ddddocr OCR shim（尚香书苑签到用）

把 [ddddocr](https://github.com/sml2h3/ddddocr) 包一层 HTTP，专门适配尚香书苑 / Discuz 的 4 位图形验证码。

## 为什么需要这层封装

签到脚本原本这样调用 OCR：

```python
requests.post(url, data={"image": img_base64}).json()
# 期望 {"code": 200, "data": "CJET"}
```

而 `python -m ddddocr api` 的官方接口是：

```text
POST /ocr
Content-Type: application/json
{"image": "<base64>"}

{"result": "CJET", "processing_time": 0.05}
```

两者字段和提交方式都不一样。这个 shim 同时兼容两种输入/输出，所以：

- 脚本里的 `DDDD_OCR_URL` 直接填 `http://VPS_IP:8000/ocr` 即可；
- 以后换回官方 ddddocr API，脚本也能识别 `result` 字段。

## 接口

### `POST /ocr`

支持三种提交方式：

```text
# 1. form + base64（脚本使用的方式）
image=<base64>&token=<可选>

# 2. JSON + base64
{"image": "<base64>", "token": "<可选>"}

# 3. multipart 文件上传（方便 curl 测试）
-F image=@captcha.png
```

成功返回：

```json
{"code": 200, "data": "CJET", "message": "success", "elapsed_ms": 6.7}
```

失败返回（HTTP 4xx/5xx，同时带 `code`）：

```json
{"code": 401, "message": "unauthorized"}
```

### `GET /health`

```json
{"status": "ok", "engine": "ddddocr", "beta": false, "uppercase": true, "error": null}
```

## 用 Docker 部署（推荐）

VPS 要求：Linux x86_64 / arm64、Docker + Docker Compose 插件、至少 1 GB 内存（模型常驻约 300–500 MB）。

### 1. 上传目录

在**本机**执行（改成你的 VPS 用户和 IP）：

```fish
scp -r ocr-shim user@VPS_IP:~/sxsy-ocr
```

或者先把仓库推到 GitHub，再在 VPS 上 `git clone` 你的 fork。

### 2. 启动

```fish
ssh user@VPS_IP
cd ~/sxsy-ocr

# 生成一个随机 token，写入 .env（compose 会自动读取）
printf 'OCR_TOKEN=%s\nOCR_BETA=0\nOCR_UPPERCASE=1\n' (openssl rand -hex 24) > .env
chmod 600 .env

docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8000/health
```

第一次构建要下载 onnxruntime / opencv 的 wheel，通常 2–5 分钟；镜像大约 1 GB。

> 如果 `docker` 提示 permission denied，用 `sudo docker compose ...`，或把当前用户加入 `docker` 组后重新登录。


### 3. 放行端口

```fish
# 以 ufw 为例；如果你用云厂商安全组，也要在控制台放行 8000/tcp
sudo ufw allow 8000/tcp
```

从**本机**验证公网可达：

```fish
curl http://VPS_IP:8000/health
```

如果访问不了，检查：

- 云厂商安全组 / 防火墙是否放行；
- `docker compose ps` 里容器是否 `Up`；
- `docker compose logs -f` 有没有报错；
- 端口是否被占用（改 `docker-compose.yml` 左边的端口）。

### 4. 填到 GitHub Secrets

```text
DDDD_OCR_URL   = http://VPS_IP:8000/ocr
DDDD_OCR_TOKEN = .env 里的 OCR_TOKEN
```

查看 token：

```fish
cd ~/sxsy-ocr
cat .env
```

### 5. 测一张真实验证码

在 VPS 上保存一张验证码图片（从浏览器签到页右键保存，或让脚本用 `SXSY_CAPTCHA_DIR` 保存），然后：

```fish
cd ~/sxsy-ocr
set -x OCR_TOKEN (sed -n 's/^OCR_TOKEN=//p' .env)
curl -sS -X POST -H "X-OCR-Token: $OCR_TOKEN" -F image=@captcha.png http://127.0.0.1:8000/ocr
```

正常输出：

```json
{"code":200,"data":"CJET","message":"success","elapsed_ms":6.7}
```

也可以直接用 base64：

```fish
set B64 (base64 -w0 captcha.png)
curl -sS -X POST -H "X-OCR-Token: $OCR_TOKEN" --data-urlencode "image=$B64" http://127.0.0.1:8000/ocr
```

## 配置项

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `OCR_TOKEN` | 空 | 共享令牌。设置后必须带 `X-OCR-Token` / `Authorization: Bearer` / `token` 字段 / `?token=` |
| `OCR_BETA` | `0` | `1` 使用 ddddocr 的 beta 模型 |
| `OCR_UPPERCASE` | `1` | 结果转大写（Discuz 验证码是大写，校验通常不区分大小写） |
| `OCR_MAX_SIDE` | `0` | 图片最长边超过该值时缩放；0 表示不缩放 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

改完 `.env` 后：

```fish
docker compose up -d
```

## HTTPS（可选，建议）

如果 VPS 有域名（比如 `ocr.example.com` 已解析到 VPS），用 Caddy 反代最省事：

1. 把 `docker-compose.yml` 的端口改成只监听本机：

   ```yaml
   ports:
     - "127.0.0.1:8000:8000"
   ```

2. 安装 Caddy，`/etc/caddy/Caddyfile`：

   ```caddyfile
   ocr.example.com {
       reverse_proxy 127.0.0.1:8000
   }
   ```

3. `sudo systemctl reload caddy`。

然后 Secret 改成：

```text
DDDD_OCR_URL = https://ocr.example.com/ocr
```

没有域名也可以用 Cloudflare Tunnel，但配置比直接开 8000 端口麻烦；只在 HTTP 上跑时，至少把 `OCR_TOKEN` 设长一点。

## 不用 Docker（bare metal）

以下示例把目录放在 `/opt/sxsy-ocr`，按你的实际路径修改。

```fish
sudo apt update
sudo apt install -y python3-venv
cd /opt/sxsy-ocr
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

set -x OCR_TOKEN (openssl rand -hex 24)
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

要长期跑，写一个 systemd unit（`/etc/systemd/system/sxsy-ocr.service`）：

```ini
[Unit]
Description=sxsy ddddocr shim
After=network.target

[Service]
WorkingDirectory=/opt/sxsy-ocr
Environment=OCR_TOKEN=把这里换成你的token
Environment=OCR_UPPERCASE=1
ExecStart=/opt/sxsy-ocr/.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```fish
sudo systemctl daemon-reload
sudo systemctl enable --now sxsy-ocr
sudo systemctl status sxsy-ocr
```

## 常见问题

### `docker compose` 提示 command not found

安装 Docker Compose 插件：

```fish
sudo apt install -y docker-compose-plugin
```

或者把命令换成 `docker-compose up -d --build`。

### `/health` 返回 503

模型没加载成功。看日志：

```fish
docker compose logs -f
```

常见原因是内存不足（`Killed`）或 CPU 架构没有对应的 onnxruntime wheel。至少给 1 GB 内存；ARM 机器确认 `uname -m` 后看构建日志。

### 返回 401

请求没带 token 或 token 不对：

```fish
curl -H "X-OCR-Token: 你的token" ...
```

### 识别率低

- 确认 `OCR_UPPERCASE=1`；
- 试试 `OCR_BETA=1` 后重启；
- 验证码图片太小/太糊时，可以设 `OCR_MAX_SIDE=200` 放大（会先按最长边缩放，仅对超大图有效；小图不会自动放大）；
- 签到脚本本身有重试：一次识别错会换一张，默认 6 次。

### 端口被占用

改 `docker-compose.yml`：

```yaml
ports:
  - "0.0.0.0:18000:8000"
```

然后 `DDDD_OCR_URL` 也跟着改成 `http://VPS_IP:18000/ocr`。

## 安全提醒

- 不设 `OCR_TOKEN` 等于把 OCR 服务公开给全网，可能被人白嫖；
- HTTP 明文传输 token，条件允许就上 HTTPS；
- 这个服务只做图片识别，不保存上传的图片（`SXSY_CAPTCHA_DIR` 是签到脚本侧的调试选项，不在 OCR 服务里）；
- 建议用 `docker compose logs` 观察异常调用量。
