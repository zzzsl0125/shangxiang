

## 项目介绍

本项目实现了尚香书苑的自动签到功能，通过 GitHub Actions 实现定时自动签到，支持多账号管理，可灵活选择邮箱密码或 Cookie 进行登录验证。

## 功能特点

-   自动登录：支持邮箱密码登录（需配合 OCR 服务）和 Cookie 直接登录两种方式
-   自动签到：每日定时完成签到操作
-   多账号支持：通过换行分隔即可管理多个账号
-   域名自适应：自动获取网站最新域名，适配域名变更场景
-   日志记录：详细输出运行日志，便于问题排查
-   灵活触发：支持手动触发和定时自动执行

## 依赖环境

-   GitHub 账号
-   尚香书苑账号（至少一个）
-   验证码识别服务（使用邮箱密码登录时必需，如`ddddocr`服务）

## 快速使用步骤

### 1. 复制仓库

点击仓库右上角的`Fork`按钮，将本项目复制到你的 GitHub 账号中。

### 2. 配置敏感信息（Secrets）

在 Fork 后的仓库中，进入`Settings` → `Secrets and variables` → `Actions`，添加以下环境变量：
#### 1. `SXSY`（**必须配置**）

用于存储账号信息，支持「邮箱密码登录」和「Cookie 登录」两种格式，**多账号需用换行分隔**：

-   **邮箱密码登录**：格式为 `邮箱&密码`，示例：
    
    ```plaintext
    your_email@example.com&your_password
    ```
    
-   **Cookie 登录**：直接填入 Cookie 字符串（从浏览器开发者工具中获取）。

若需配置多个账号，按如下格式（换行分隔每个账号）：

```plaintext
user1@example.com&123456
user2@example.com&abcdef
cookie_string_for_user3
```

#### 2. `DDDD_OCR_URL`（**可选**）

仅当使用「邮箱密码登录」时需要，填写**验证码识别服务的接口地址**（如 `ddddocr` 类 OCR 服务的部署地址）。


### 3. 启用工作流

1.  进入仓库的`Actions`标签页
2.  点击`I understand my workflows, go ahead and enable them`启用工作流

## 定时任务配置

默认配置为每天 UTC 时间 20:05 执行（对应北京时间次日 04:05），如需修改执行时间，可编辑`.github/workflows/main.yml`中的`cron`表达式：

```yaml
schedule:
  - cron: '5 20 * * *'  # 格式：分 时 日 月 周（UTC时间）
```

时间转换说明：UTC 时间 + 8 小时 = 北京时间，例如：

-   北京时间 09:00 → UTC 时间 01:00 → 表达式：`0 1 * * *`
-   每天两次执行（09:00 和 21:00）→ 表达式：`0 1,13 * * *`

## 多账号配置示例

在`SXSY`环境变量中通过换行分隔多个账号，支持混合登录方式：


```plaintext
user1@example.com&password1  # 邮箱密码登录
user2@example.com&password2  # 邮箱密码登录
cookie_string_for_user3      # Cookie登录
```

## 运行日志查看

1.  进入仓库的`Actions`标签页
2.  选择左侧的`尚香书苑自动签到`工作流
3.  点击最新的运行记录
4.  选择`check-in-job`查看详细执行日志

## 项目结构说明


```plaintext
shangxiang/
├── 尚香书苑.py        # 核心签到脚本
├── requirements.txt   # 依赖库清单（仅需requests）
└── .github/workflows/
    └── main.yml       # GitHub Actions工作流配置
```

## 注意事项

1.  Cookie 登录方式无需验证码服务，推荐优先使用
2.  邮箱密码登录必须配置有效的`DDDD_OCR_URL`，否则无法完成验证码验证
3.  网站域名可能不定期变更，程序会自动获取最新域名，若出现异常可手动检查
4.  常见失败原因：账号信息错误、OCR 服务不可用、网络波动、网站维护
5.  请遵守网站用户协议，合理使用自动签到功能

## 手动触发签到

除定时任务外，可手动触发签到：

1.  进入仓库的`Actions`标签页
2.  选择`尚香书苑自动签到`工作流
3.  点击`Run workflow`按钮，在弹出框中再次点击`Run workflow`即可
