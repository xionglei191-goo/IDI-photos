# 本机开发与 246 服务部署

自 2026-10-21 起，本机 Windows 用于开发、构建和隔离测试，246 暂时作为正式服务器。前后端使用同一份源码和同一个 Dockerfile；服务器地址只属于部署配置，不写入业务逻辑。

| 环境 | 用途 | 入口与数据 |
|---|---|---|
| 本机 Windows Docker | 开发和测试 | `http://127.0.0.1:18767/`，Compose 项目 `idi-dev`，独立测试卷 |
| 246（`192.168.31.246`） | 正式服务及业务档案 | 公网 `https://idi.xionglei.online/`，局域网 `http://192.168.31.246:8766/` |
| 本机 `127.0.0.1:18766` | 通过 SSH 管理 246 | 是服务器的管理入口，数据仍在 246，不是本机开发环境 |

246 部署目录为 `/home/xionglei/Project/IDI`，正式 Compose 项目为 `idi246`。本机原 `data/` 目录及备份作为历史副本保留，旧 Windows 正式服务已停止；日常业务以 246 的档案为准。实际版本、恢复方式和服务器操作记录见本机 `artifacts/deploy-246-2026-10-20/DEPLOYMENT.md`。

## 启动本机开发环境

开启 Docker Desktop 的 Linux 容器模式，然后双击仓库根目录的 `start-dev.cmd`。也可在 PowerShell 中运行：

```powershell
.\scripts\dev.ps1 -Action start -OpenBrowser
```

启动脚本固定使用本机 `desktop-linux` Docker context，构建 `idi-dev:local` 并启动 `compose.dev.yaml`。同一标签重复用于日常开发，不为每次检查增加镜像标签。源码修改后再次执行启动命令，镜像会重新构建并应用变更。

开发服务仅监听本机 `18767`，使用 `idi-dev_data` 和 `idi-dev_key` 两个独立 Docker 卷；不挂载仓库 `data/`、正式服务器目录或生产凭据。首次启动为空库，可自行建立测试管理员。容器停止后测试数据保留，Docker 重启时不会自动启动此开发服务。

默认关闭官网写入和后台任务，不自动收图、同步台账或发送群消息。测试照片导入、OCR 等异步流程时，可在使用空库或脱敏测试资料的前提下启用开发任务：

```powershell
$env:IDI_DEV_WORKER = '1'
.\scripts\dev.ps1 -Action start
```

恢复默认并重新启动：

```powershell
Remove-Item Env:IDI_DEV_WORKER -ErrorAction SilentlyContinue
.\scripts\dev.ps1 -Action start
```

官网写入仍由开发配置固定关闭；联调使用专用测试账号或模拟接口，不复制正式飞书机器人、官网账号和模型密钥。Windows 原生 Python 启动方式保留用于兼容与专项调试，日常默认使用上述 Docker 开发入口。

查看状态、日志和停止：

```powershell
.\scripts\dev.ps1 -Action status
.\scripts\dev.ps1 -Action logs
.\scripts\dev.ps1 -Action stop
```

也可双击 `stop-dev.cmd`。停止操作只作用于本机 `idi-dev` 项目，保留测试卷。

## 测试与发布

1. 在本机修改共用源码，使用隔离数据验证受影响的流程；正式档案不作为测试输出目录。
2. 测试通过后，为发布生成明确版本及镜像摘要。246 运行本次已验证的同一镜像，不在服务器重新安装一套业务依赖。
3. 在发布任务中核对 246 的运行任务和备份，将镜像传入服务器，再更新服务。日常开发启动不会更新、重启或停止 246。
4. 发布后核对健康接口、登录与权限、页面资源以及受影响的业务流程。保留上一版镜像和升级前的档案备份。

测试数据不会自动同步到正式环境。数据库、原件、主密钥和部署配置独立于镜像；更新程序不覆盖这些持久文件。主密钥与加密凭据应成套备份，不能只搬运加密文件。
