# Web 构建与热补丁部署（本实例）

本实例的 web 静态产物**只能在开发机上构建**（服务器没有 node），再 `docker cp` 进
`dramaclaw-web-1`。这里记的是"构建 → 部署 → 校验"的固定动作，重点是别再把实例级
UI 隐藏开关弄丢（2026-10-21 出过一次：用不带 `.env.ce.local` 的产物热补丁，右下
「PiKo一下」、右上「公告中心」、官方渠道、配置管理引导、进站更新弹窗全部回归）。

## 1. 实例级隐藏开关（构建期，写死在产物里）

来源：`frontend/src/lib/ui-hide-flags.ts`。上游未设置时保持官方原样，本实例把全部
开关设为 `1`，集中放在 **`frontend/.env.ce.local`**（`frontend/.gitignore` 的
`*.local` 已忽略；只在 `vite build --mode ce` 时加载，dev / vitest 不受影响）：

| 开关 | 作用 |
| --- | --- |
| `VITE_HIDE_PIKO_ENTRY=1` | 去掉状态栏「PiKo一下」 |
| `VITE_HIDE_NOTIFICATION_CENTER=1` | 去掉公告中心（应用内右上 + 登录页顶栏铃铛） |
| `VITE_HIDE_RELEASE_POPUP=1` | 不自动弹更新公告 |
| `VITE_HIDE_OFFICIAL_CHANNELS=1` | 设置里只留自定义/本地网关 |
| `VITE_HIDE_MODEL_GUIDE=1` | 去掉「配置手册」引导链接 |
| `VITE_HIDE_GITHUB_STARS=1` | 不请求 `api.github.com` 拿星标数 |
| `VITE_LOGIN_ANNOUNCEMENTS_URL=/announcements.empty.json` | 公告改读本站空列表（完全静默，不出网） |

它们是**编译期常量**：改了必须重新构建，只换产物文件不重建无效。服务器
`/root/dramaclaw/.env` 第 370 行附近也留着同一组值（供源码 `docker compose build web`
使用）；从服务器源码构建出来的镜像天然带这些开关，而本机热补丁产物**不会**自动继承
它们 —— 这就是 `.env.ce.local` 存在的意义。

## 2. 构建（在开发机）

```powershell
cd frontend
$env:VITE_APP_VERSION="dev"    # 与现网 version.json 的 version 保持一致
npm run build:ce               # tsc -b && vite build --mode ce（自动加载 .env.ce.local）
```

构建后先离线自检（应当 0 命中）：

```powershell
Select-String -Path dist\assets\*.js -Pattern "api\.github\.com","nfg-web\.cdnfg\.com" -List
```

## 3. 部署（热补丁，绝不 `docker compose up -d`）

```bash
# 回滚点：把现网静态目录整体打包到宿主
docker exec dramaclaw-web-1 sh -c 'cd /usr/share/nginx/html && tar czf - .' \
  > /root/.deploy/web_<change>_<ts>.tgz

# 覆盖同名文件；旧 chunk 保留，不打断在飞客户端（该目录无宿主挂载，只能 docker cp）
docker cp frontend/dist/. dramaclaw-web-1:/usr/share/nginx/html/
```

部署前确认服务器空闲；部署后不要重启 api（`docker restart dramaclaw-api-1` 与本流程无关）。

## 4. 校验

```bash
docker exec dramaclaw-web-1 sh -c 'cat /usr/share/nginx/html/version.json'
docker exec dramaclaw-web-1 sh -c 'ls -la /usr/share/nginx/html/assets | head'
# index.html / 新 chunk / announcements.empty.json / version.json 均应 HTTP 200
```

浏览器侧用**无痕窗口**硬刷新确认：

1. 状态栏无「PiKo一下」；2. 应用内右上无公告中心；3. 登录页无公告铃铛；
4. 进站不弹更新公告；5. 设置 → 模型与渠道无官方渠道、无「配置手册」；
6. 登录页星标数不再变化（且 DevTools 里没有对 `api.github.com` 的请求）。

## 5. 回滚

```bash
docker exec dramaclaw-web-1 sh -c 'cd /usr/share/nginx/html && tar xzf -' < /root/.deploy/web_<change>_<ts>.tgz
```

保留旧 chunk 的另一个好处：回滚只需覆盖回旧文件即可，无需清空目录。
