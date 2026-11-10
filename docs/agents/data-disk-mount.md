# 数据盘挂载与 CE 数据迁移（nn48）

一台自建服务器（`aistry` / 主机名 `ecsn1VZQ`，时区 `Asia/Shanghai`）新挂了一块
100G 数据盘，用于把 46G 的 CE 数据卷从系统盘迁出，缓解根分区占用。本文记录当时的
处置方式，供后续同类操作参考。

## 现状与结论

| 项 | 值 |
| --- | --- |
| 系统盘 | `/dev/vda`（100G），`vda2` → `/` |
| 新数据盘 | `/dev/vdb`（100G），单分区 `vdb1` |
| 文件系统 | `ext4`，label `nndata`，UUID `9f252eeb-060c-4d8e-8531-9ec886178a03` |
| 挂载点 | 宿主 **`/data`**（`/etc/fstab`：`UUID=… /data ext4 defaults,nofail 0 2`） |
| CE 数据卷 | 命名卷 `dramaclaw_ce-data`（约 46G），已迁到 `/data` |
| compose | `/root/dramaclaw/docker-compose.release.yml`，api 的 `/data` 由命名卷改为绑定 `- /data:/data` |

> 关键现象：新盘刚挂上时 `lsblk` 能看到 `vdb1`，但对它做真实扇区 I/O（`blkid` /
> `wipefs` / `dd`）会陷入 D 状态卡死，连 `timeout` 都杀不掉。**整机重启后设备重新
> 初始化即恢复正常**，所以流程是「先重启、再挂载」。

> 该盘交付时已带一个**空的 ext4**；脚本检测到现有文件系统即不格式化，避免误清数据。

## 用到的 systemd 单元（服务器上）

| 单元 | 作用 |
| --- | --- |
| `nn48-reboot.timer` + `.service` | 定时（本例 `2026-11-10 02:00 CST`）整机 `systemctl reboot`，一次性；执行后停用 |
| `nn48-data-setup.service` | 开机自启一次性服务，幂等：挂载 + 迁移；成功打 `setup.done` / `migrated.done` 标记，之后开机直接早退 |
| `nn48-cleanup.timer` + `.service` | 观察约 24h 后（`2026-11-11 03:00 CST`）带守卫地删除旧卷 `dramaclaw_ce-data`，释放系统盘空间 |

脚本：`/usr/local/bin/nn48-data-setup.sh`、`/usr/local/bin/nn48-cleanup-old-volume.sh`；
日志：`/var/log/nn48-data-setup.log`、`/var/log/nn48-cleanup.log`；备份与标记：
`/root/.deploy/nn48/`。

## `nn48-data-setup.sh` 流程

1. 等 `/dev/vdb1` 出现；`wipefs -n` 做 **I/O 健康检查**（30s 超时；卡死则记日志退出，
   **绝不触碰磁盘、不阻断开机**）。
2. 无文件系统才 `mkfs.ext4 -F -L nndata`（有标记则跳过）。
3. 写 `/etc/fstab`（`nofail`）+ `daemon-reload` + `mount /data`。
4. 迁移（仅一次）：`docker compose stop api` → `rsync -aHAX --delete` 卷数据到 `/data`
   → 把 compose 的 `- ce-data:/data` 改为 `- /data:/data`（改前备份、改后 `config -q` 校验，
   失败则还原并恢复旧卷）→ `docker compose up -d`。
5. **保留旧卷**做回滚，打 `migrated.done`。

## 回滚

- 恢复 compose：用 `/root/.deploy/nn48/docker-compose.release.yml.bak.*` 覆盖回去，
  `docker compose -f … up -d`（api 会重新挂旧命名卷）。
- 单元：`systemctl disable nn48-data-setup.service`。
- `/etc/fstab` 备份同样在 `/root/.deploy/nn48/`。

## 清理

旧卷由 `nn48-cleanup.timer` 在观察期后删除；也可手动：
`docker volume rm dramaclaw_ce-data`（删除后系统盘占用从 ~89% 降到 ~42%）。
取消定时删除：`systemctl disable --now nn48-cleanup.timer`。

## 验证命令

```bash
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,LABEL /dev/vdb   # vdb1 ext4 /data
df -h / /data
docker inspect dramaclaw-api-1 | grep -i -A1 source          # Source=/data Destination=/data
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8780/api/v1/projects
systemctl list-timers 'nn48-*' --all --no-pager
tail -n 50 /var/log/nn48-data-setup.log
```
