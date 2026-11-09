# 配置参数参考

.env.example 是部署示例，两个 config.example.toml 是应用示例。实际使用的 .env、config.toml 和 state/ 均由 Git 忽略。Compose 显式设置的环境变量优先于 TOML，修改后需重新创建服务。

## 媒体和设备环境变量

| 参数 | 默认示例 | 含义 |
| --- | --- | --- |
| PICSYNC_PHOTO_ROOT | /srv/photos | 必填：源与目标的宿主机共同父目录，必须事先存在。 |
| PICSYNC_SOURCE_SUBDIR | source | 源相对路径，禁止绝对路径、..、符号链接及与目标重叠。 |
| PICSYNC_TARGET_SUBDIR | target | 专用目标相对路径，只有此目录进入 Syncthing。 |
| PICSYNC_MEDIA_FS | ext4 | 按实际修改，如 nfs4、xfs、btrfs；留空只验证容器挂载存在，不适合 NAS 丢挂载检查。 |
| PICSYNC_UID / PICSYNC_GID | 1000 / 1000 | 控制器、看板和 Syncthing 的文件访问身份，匹配媒体权限。 |
| PICSYNC_SERIAL | 空 | 必填：ADB 手机序列号，不自动选择第一台设备。 |
| PICSYNC_SYNCTHING_DEVICE | 空 | 必填：完整手机 Syncthing 设备 ID，也兼容唯一首组 ID。 |
| TZ | Asia/Shanghai | 可改为自己的 IANA 时区。 |

控制器容器内的安全根固定为 /photos/源子目录、/photos/目标子目录，启动时确定，不能通过清理命令放宽。

## MySQL 环境变量

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| PICSYNC_MYSQL_HOST | 127.0.0.1 | 外部库地址；内置库保持回环地址。 |
| PICSYNC_MYSQL_PORT | 3306 | 客户端连接端口，同时用于内置库宿主机端口映射。 |
| PICSYNC_MYSQL_DATABASE | picsync | 业务库名，自定义时同步修改外部授权。 |
| PICSYNC_MYSQL_USER | picsync | 控制器账号，在业务库内建表、迁移、读写。 |
| PICSYNC_MYSQL_PASSWORD | 空 | 必填：控制器账号密码。 |
| PICSYNC_DASHBOARD_MYSQL_USER | picsync_reader | 独立看板账号，只能 SELECT，不得与控制器相同。 |
| PICSYNC_DASHBOARD_MYSQL_PASSWORD | 空 | 必填：看板只读账号密码。 |
| MYSQL_ROOT_PASSWORD | 空 | 仅内置库必填：初始化管理员密码，不传给应用。 |

内置 MySQL 仅在首次初始化时创建数据库和账号。修改 .env 不会更改已有账号。名称只允许字母、数字和下划线，最长 64 字符。

## 可选功能环境变量

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| PICSYNC_CONTROLLER_IMAGE | picsync-controller:local | 可填 Docker Hub 预构建镜像；同时用于 preflight 和可选容器 ADB。 |
| PICSYNC_DASHBOARD_IMAGE | picsync-dashboard:local | 可填 Docker Hub 看板镜像，不填则沿用本地构建。 |
| PICSYNC_BARK_KEY | 空 | 私有推送密钥，还需在控制器 TOML 启用 Bark。 |
| PICSYNC_PIXEL_URL | 空 | 手机控制入口；为空隐藏，只允许 HTTP(S) 绝对 URL。 |
| PICSYNC_SYNCTHING_URL | 空 | Syncthing 入口；为空隐藏，不得含凭据。 |
| PIXEL_WEB_PORT | 8001 | 可选手机网页的本机端口。 |
| SYNCTHING_IMAGE | syncthing/syncthing:latest | 可改为自己验证过的固定版本或摘要。 |
| PIXEL_IMAGE | ghcr.io/bilbospocketses/ws-scrcpy-web:stable | 可选网页镜像，更新后重测登录和投屏。 |

ADB 固定本机 5037，看板固定本机 8027，Syncthing GUI 固定本机 8384。Syncthing 同步协议还使用自身默认传输端口，host 网络不会限制这些端口，应按网络策略配置防火墙。

## controller/config.toml

单位默认为秒，字节字段单独标注。

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| scan.interval | 3600 | 源扫描间隔；扫描异常按扫描轮次连续计数。 |
| scan.progress_every | 100 | 每多少项发布扫描进度。 |
| mapping.max_bytes | 16106127360 | 每批挑选字节预算，约 15 GiB，过大单文件可能跳过，需查看报告。 |
| mapping.order | mtime_asc | 从旧到新，目前只允许此排序。 |
| monitor.interval | 60 | 活动批次及异常轮询间隔，包含本轮调用耗时。 |
| monitor.idle_interval | 300 | 没有待处理批次时的间隔。 |
| observation.confirmations | 3 | 稳定观察次数，不能低于 3。 |
| observation.interval | 300 | 稳定观察间隔，不能低于 300 秒。 |
| syncthing.config | 示例路径 | XML 配置路径，Compose 覆盖为 /etc/syncthing/config.xml。 |
| syncthing.folder | photos | 已配对文件夹 ID，不是目录名。 |
| syncthing.device | 空 | 手机设备 ID，Compose 从 .env 覆盖。 |
| syncthing.timeout | 10 | API 超时，目前要求带密钥的本机 127.0.0.1:8384 接口。 |
| pixel.container | pixel-web | 兼容容器后端名称；host 后端仅作为设备锁标识。 |
| pixel.adb_backend | host | 默认共享 ADB；container 是源码开发兼容模式，默认镜像不包含 Docker CLI。 |
| pixel.adb_path | adb | 客户端路径；Compose 覆盖为保护共享服务生命周期的包装器。 |
| pixel.serial | 空 | 手机序列号，Compose 从 .env 覆盖。 |
| pixel.adb_timeout | 40 | 单次 ADB 操作超时。 |
| mysql.host/port/user/password/database | 示例值 | 数据库字段，Compose 环境变量优先；端口范围 1–65535。 |
| bark.enabled | false | 是否启用推送，还需要非空密钥。 |
| bark.key | 空 | 私有密钥，可由环境变量覆盖。 |
| bark.timeout | 15 | 单次推送超时。 |
| bark.retries | 2 | 首次请求失败后的重试次数，0 为仅一次。 |
| bark.error_threshold | 2 | 连续 X 次异常才通知；成功一次清零，主循环和源扫描独立统计。 |
| bark.error_interval | 3600 | 同批次、同类通知的重复间隔，不同类型独立节流。 |
| bark.blocked_after | 1200 | 兼容保留字段，目前不参与异常门槛；应使用 error_threshold。 |
| logging.path | /state/logs/controller.log | 持久日志路径，相对路径按控制器源码目录解析。 |
| logging.level | INFO | DEBUG、INFO、WARNING、ERROR、CRITICAL。 |
| logging.max_bytes | 10485760 | 单个滚动日志最大字节数。 |
| logging.backups | 5 | 滚动日志保留份数。 |

可添加 directories.source/target，但它们必须与进程安全根完全一致，不能用于绕过保护。主控制器会自动执行工作流，不提供预览模式。

## dashboard/config.toml

| 参数 | 含义 |
| --- | --- |
| mysql.host/port/user/password/database | 只读账号连接，Compose 环境变量覆盖，不能误配控制器账号。 |
| dashboard.telemetry | 状态 JSON 路径，默认 /data/status/runtime.json，只读挂载。 |
| dashboard.refresh_seconds | 默认 10；目前是页面刷新提示值，实际前端轮询仍固定 10 秒，修改它不会改变采集频率。 |
| links.pixel_url / links.syncthing_url | 可选入口，环境变量优先，不支持脚本 URL。 |

电量随控制器轮询采集，两次采集至少间隔 60 秒，空闲时可能间隔 300 秒。读取失败或数据超过 10 分钟未更新时，显示上次值；电量未知时不显示为 0%。电量不参与清理判断。

## Profile 和持久化

| Profile | 内容 |
| --- | --- |
| 默认 | preflight、controller、dashboard、syncthing |
| database | 额外启用内置 MySQL；不启用即可使用外部库。 |
| usb | 额外启用容器 ADB，不能与宿主机 ADB 并行。 |
| phone | 额外启用手机控制网页，需先准备 state/adb-client。 |

state/mysql、state/syncthing、state/adb-auth、state/pixel、state/logs、state/status 分别保存数据库、同步状态、授权、网页状态、日志和遥测数据。这些内容均应保持私有，不提交到 Git。更改 UID/GID 后核查已有数据权限。

源码/systemd 模式另外设置 PICSYNC_SOURCE、PICSYNC_TARGET、PICSYNC_MEDIA_MOUNT，以及本机 PICSYNC_CONFIG、PICSYNC_TELEMETRY、PICSYNC_ADB_PATH、PICSYNC_SYNCTHING_CONFIG；日志路径也需可写。Compose 已自动设置这些变量，不需额外写入 .env。

### 低电量与 Syncthing 资源预算

新增硬链接仅在有效手机电量至少 10% 时执行；低于 10%（包括充电时）或电量不可读时暂停，达到 10% 自动继续。电量每 60 秒读取一次，映射循环逐文件检查缓存，保留冻结候选和已处理进度。已有文件传输及清理仍使用各自原有判定条件。

Compose 默认限制 Syncthing 为 1 CPU，`GOMAXPROCS=1`，`GOMEMLIMIT=512MiB`（Go 内存软目标，不是容器硬上限）。可用 `SYNCTHING_CPUS`、`SYNCTHING_GOMAXPROCS`、`SYNCTHING_GOMEMLIMIT` 调整。预算较低时传输和扫描可能变慢。参考 https://docs.syncthing.net/users/tuning.html 。

低电量阈值现可在控制器 TOML 中设置，重启控制器生效：

```toml
[mapping]
min_battery_percent = 10 # 低于此值触发暂停
resume_battery_percent = 20 # 已暂停后，达到此值才恢复
```

沿用 `[bark]` 的启用状态、密钥、超时与重试配置。每次低电量周期发送一次通知（即使当前不在映射阶段），通知记录保存到数据库，重启不会重复推送；有效电量达到恢复阈值后重新允许下一次低电量通知。推送失败不标记成功，下次电量检查继续尝试；电量未知不会误报低电量或清除去重记录。通知失败不解除硬链接暂停。


恢复阈值必须满足 `1 <= min_battery_percent <= resume_battery_percent <= 100`。例如 9% 触发暂停后，10%–19% 仍暂停，20% 恢复；尚未触发暂停时 10%–19% 可继续。暂停状态独立于 Bark 成功记录保存在数据库，通知失败、Bark 禁用或控制器重启都不会绕过恢复阈值。电量未知时暂停新增链接并保留原暂停状态。升级时也识别旧低电量通知记录。
