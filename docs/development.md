# 开发、源码部署与验证

本文面向修改 PicSync 源码、调试部署或维护发布流程的开发者。只使用已发布镜像的用户请阅读 [用户部署教程](deployment.md)。

## 1. 准备开发环境

需要 Git、Python（建议与 Dockerfile 一致，使用 3.12）、Docker Engine 和 Docker Compose v2。在独立开发目录获取源码并安装依赖：

```sh
git clone https://github.com/wylyeak/sync_pic.git
cd sync_pic
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

仓库结构：

| 目录 | 职责 |
| --- | --- |
| `controller/` | 扫描库存、批次归属、备份确认、目标清理、通知和遥测 |
| `phone/` | ADB 操作与 Google 相册 UI 状态读取 |
| `dashboard/` | 只读 API、页面和数据库查询 |
| `deploy/` | ADB 服务、手机网页适配和数据库初始化模板 |
| `scripts/` | 配置准备、测试、目录预检和可移植性检查 |
| `tests/` | 配置边界与 MySQL 初始化等部署相关测试 |

真实 `.env`、应用 `config.toml`、照片、数据库、设备信息、密钥和 `state/` 均不提交 Git。仓库不包含第三方 APK、ADB、scrcpy 二进制或上游服务源码，按各自许可另行获取。

## 2. 运行默认测试

在已激活的虚拟环境中执行：

```sh
sh scripts/test.sh
docker compose --env-file .env.example config --quiet
```

测试脚本依次运行控制器、手机模块、看板和部署测试，最后执行可移植性扫描。它不读取部署 `.env`，会清除脚本列出的生产连接环境变量，并使用示例配置。默认测试不访问真实手机、照片或生产数据库。

需要显式启用的 MySQL 集成测试会显示 skipped。默认测试通过不代表这些测试已经执行，也不能证明真实相册备份、释放空间或手机重启重连成功。

可单独运行可移植性检查：

```sh
python3 scripts/check-portability.py
git diff --check
```

扫描只检查 Git 跟踪文件中的运行数据、私钥和明显本机地址；提交前仍需人工检查 `git diff --cached`，不能把扫描通过当作敏感信息不存在的保证。

## 3. 可选：隔离 MySQL 集成测试

集成测试会创建并删除 `file_sync_test_<随机标识>` 数据库，需要具备相应建库、删库权限的测试账号。只连接专用、可丢弃的测试服务器，不使用生产实例或生产账号。

先准备独立 MySQL，并在当前终端设置以下变量，将示例地址、端口、账号和密码替换为测试环境值：

```sh
export FILE_SYNC_TEST_MYSQL=1
export FILE_SYNC_MYSQL_HOST=127.0.0.1
export FILE_SYNC_MYSQL_PORT=13306
export FILE_SYNC_MYSQL_USER=picsync_test
read -r -s -p '测试数据库密码: ' FILE_SYNC_MYSQL_PASSWORD
printf '\n'
export FILE_SYNC_MYSQL_PASSWORD
export PYTHONPATH="$PWD/controller:$PWD/phone:$PWD/dashboard"
python -m unittest discover -s controller -p 'test_*.py'
unset FILE_SYNC_TEST_MYSQL FILE_SYNC_MYSQL_PASSWORD
```

密码输入命令使用 Bash 语法，请在 Bash 中执行本段。不要将密码直接写进文档、仓库或 shell 历史。这里直接调用 unittest，因为 `scripts/test.sh` 会主动取消 `FILE_SYNC_TEST_MYSQL`，以保持默认测试隔离。

检查输出确认集成测试实际运行；连接失败不能记为通过。随机测试库不等于服务器隔离，必须在执行前核实连接目标。

## 4. 在独立环境构建和部署源码

使用独立开发主机或虚拟机、专用数据库、测试手机及少量测试照片。仅更换 Compose 项目名不能完成隔离：多个服务使用 host 网络，ADB 5037、看板 8027、Syncthing GUI 8384 等端口可能与现有服务冲突。不要在运行中的生产部署目录构建或调试。

准备本地配置：

```sh
sh scripts/prepare.sh
```

按 [用户部署教程](deployment.md) 填写媒体路径、实际文件系统类型、数据库及设备信息。测试源和目标必须是同一挂载下互不重叠的专用目录。将开发环境 `.env` 中的应用镜像地址改为：

```dotenv
PICSYNC_CONTROLLER_IMAGE=picsync-controller:dev
PICSYNC_DASHBOARD_IMAGE=picsync-dashboard:dev
```

如果目录中已有禁用构建的私有 `compose.override.yml`，先检查它是否适合开发环境；不要复制生产配置来解决构建问题。

```sh
docker compose config --quiet
docker compose build controller dashboard
docker compose run --rm --pull never preflight
```

构建控制器也提供了 `preflight` 和可选 `adb` 共用的镜像。先按用户教程启动测试数据库和 ADB，再启动 Syncthing 并完成配对。上游服务需先按教程 pull；本地应用镜像无需 pull。

```sh
docker compose up -d --no-build --pull never syncthing
docker compose run --rm --pull never controller python controller/file_sync.py dry-run
```

`preflight` 只读检查目录；`dry-run` 检查目录边界及 Syncthing 状态，不扫描入库、不释放空间、不删除照片。它们通过后，仍需确认测试手机和 Google 相册设置正确。

下面的命令会启动真实自动处理，仅在上述独立测试环境执行：

```sh
docker compose up -d --no-build --pull never controller dashboard
docker compose ps -a
docker compose logs --tail 100 controller dashboard
curl -f http://127.0.0.1:8027/health
```

修改源码后重新构建对应镜像并重新创建服务；普通 Compose 部署不会因为编辑宿主机源码而更新容器中的应用。`preflight` 成功后退出是正常状态，看板应变为 healthy，控制器应持续运行且无未处理异常。

## 5. 按改动范围验证

| 改动范围 | 需要确认的行为 |
| --- | --- |
| 文档 | 命令、相对链接、配置名称和实际 Compose 一致；用户流程与开发流程分开 |
| 库存和批次 | 路径唯一归属、指纹变化、数据库重连及恢复后的状态一致；涉及 SQL 时运行隔离集成测试 |
| 目标清理 | 源目录始终受保护；路径穿越、符号链接、重叠、挂载别名及重试不能绕过目标边界 |
| 手机自动化 | UI 状态解析、设备锁、异常及离线处理；使用测试手机做只读采集和小批量实际验收 |
| 看板 | API 与模板测试、只读账号、未知及过期状态、桌面和手机布局 |
| Compose 或镜像 | 默认及受影响 profiles 的配置解析、镜像构建、目录预检、容器状态与健康接口 |

需要检查所有可选组件的配置时执行：

```sh
docker compose --env-file .env.example --profile database --profile usb --profile phone config --quiet
```

端到端验收顺序：挂载和权限 → 数据库两个账号 → ADB 识别 → Syncthing 配对 → dry-run → 专用测试文件双向删除 → 小批真实备份 → 手机重启重连 → 宿主机重启恢复。

每一步记录提交号、镜像、配置条件、通过项、跳过项及尚未验证的行为。网页健康不代表整条备份流程成功，镜像构建也不代表生产部署完成。源照片保护、备份证据和稳定观察条件在测试、重试及异常处理中都不能放宽。测试文件删除只在专用隔离环境进行，不对真实源照片做删除测试。

## 6. 提交、发布和上线验证

提交前完成相关测试、Compose 校验和可移植性扫描，检查暂存区只包含预期改动：

```sh
git diff --check
git diff --cached
```

维护者的发布配置见 [镜像构建与发布](docker-hub.md)。推送后等待对应提交的 GitHub Actions 测试、两个镜像构建及发布校验全部成功，再部署该提交。

生产升级使用远端镜像：核对两个镜像的 OCI revision、固定各自摘要，备份私有配置并保留旧镜像，通过 Compose 更新后验证目录检查、运行镜像、控制器状态、数据库读取和看板健康。具体命令见 [用户部署教程的升级与回退](deployment.md#9-镜像版本升级和回退)。不要用本地构建或直接修改运行容器代替正式发布。

[验证范围](validation.md) 保存已有验证记录及其边界；它不是每次提交自动更新的验收结论。新的验证结果应注明对应版本和环境，不把历史通过项直接套用于当前改动。
