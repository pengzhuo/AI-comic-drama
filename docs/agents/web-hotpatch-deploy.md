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
| `VITE_BRAND_ATTRIBUTION=基于(Dramaclaw)开发` | **内容型**（不是隐藏开关）：底部状态栏右端的署名文案；未设置则不渲染 |

其中 `VITE_BRAND_ATTRIBUTION` 与其余「=1 即隐藏」的开关不同，它读的是**任意字符串**
（`frontend/src/lib/brand-attribution.ts` 的 `brandAttribution()`，刻意不放进
`ui-hide-flags.ts` —— 那是「只做减法」的布尔开关，而这条是上游永远不该出现的内容），
未设置或只有空白时返回 `null`、整个元素不渲染，所以上游/其他部署的产物一个字节都不会变。

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

构建后先离线自检：

```powershell
# 1) 星标请求必须 0 命中（漏载 .env.ce.local 时会出现）
Select-String -Path dist\assets\*.js -Pattern "api\.github\.com" -List

# 2) 公告必须指向本站空列表（覆盖生效）
Select-String -Path dist\assets\*.js -Pattern "announcements\.empty\.json" -List
Test-Path dist\announcements.empty.json        # 应为 True（26 B）
```

> `nfg-web.cdnfg.com` 会有 **1 处**命中（`DEFAULT_ANNOUNCEMENTS_URL` 兜底常量），
> 线上官方产物同样如此；它只在 `VITE_LOGIN_ANNOUNCEMENTS_URL` 未设置时才会被用到，
> **不是**构建失败信号。同理 `dramaclaw-dl.cdnfg.com`（桌面端下载）、
> `nfg-web-assets.cdnfg.com`（社区/更新素材）、`relayclaw.cdnfg.com`（设置里的外链）
> 等官方静态链接常量会一直留在产物中，与本实例的隐藏开关无关 —— 所以**不能**再用
> 「`cdnfg.com` 0 命中」做判据。

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
6. 登录页星标数不再变化（DevTools 里既没有对 `api.github.com` 的请求，也没有对
   `nfg-web.cdnfg.com/dramaclaw/announcements.json` 的请求）。

## 5. 回滚

```bash
docker exec dramaclaw-web-1 sh -c 'cd /usr/share/nginx/html && tar xzf -' < /root/.deploy/web_<change>_<ts>.tgz
```

保留旧 chunk 的另一个好处：回滚只需覆盖回旧文件即可，无需清空目录。

## 6. 产物形态：双入口（现网形态）

本实例线上跑的是**双入口产物**：一套 `npm run build:ce` 产物里同时有内部树与客户
工作室，根地址归客户，内部端挂 `/console`。开关在 `.env.ce.local`：

```ini
VITE_INTERNAL_ENTRY_PATH=/console   # 留空 = 传统内部端（`/` 就是内部首页）
```

| 地址 | 内容 |
| --- | --- |
| `/` | 自动跳 `/studio`（客户入口） |
| `/studio`、`/studio/<项目>` | 客户工作室：上传剧本 → 一键成片 → 进度 → 每集费用 |
| `/console` | **内部入口**：内部首页（项目列表）；内部深链 `/projects/<id>/...` 照旧可用 |
| `/login` | 登录（CE 实例 `auth_required:false`，直接可用） |

形态与路由的对应关系：`src/lib/app-mode.ts` 的 `internalEntryPath()` / `internalHomePath()`
（内部壳的品牌 logo、返回项目列表、项目不存在回退、算力页都指向 `internalHomePath()`），
以及 `src/routes/_app/console.tsx`（内部入口的真身，路径要与开关值一致）。

> **它只是路径分区，不是访问控制**：同一份 bundle、同域、无鉴权。要真隔离得上
> nginx 的 `auth_basic` 或来源 IP 白名单（见「两份产物 + 子路径」的备选方案）。

这套形态下内部页**确实在产物里**（内部用户要用它），所以下面 6.2 的「内部页 0 命中」
不适用于它 —— 那条检查只用于纯客户端产物。

### 6.1 双入口产物自检

```powershell
# 形态生效：内部入口路由进了路由树
Select-String -Path src\routeTree.gen.ts -Pattern "'/console'" -SimpleMatch -List   # ≥1

# 两个入口的代码都在产物里（本次内部页**是**该在的）
Select-String -Path dist\assets\*.js -Pattern "拿捏漫剧工作台" -SimpleMatch -List    # ≥1（工作室标题）
Select-String -Path dist\assets\*.js -Pattern "AI 短剧工作台" -SimpleMatch -List     # 必须 0（已改名）
Select-String -Path dist\assets\*.js -Pattern "/_app" -SimpleMatch -List            # ≥1（内部路由树）
Get-ChildItem dist\assets | Where-Object { $_.Name -match "FreezoneCanvasRoot|ThreeDDirectorDialog" }

# 实例配置仍生效（与第 2 节同一组证据）
Select-String -Path dist\assets\*.js -Pattern "api\.github\.com" -SimpleMatch -List  # 必须 0
Select-String -Path dist\assets\*.js -Pattern "announcements\.empty\.json" -SimpleMatch -List  # 必须 ≥1
```

部署后（无痕窗口）确认：`/` 自动进 `/studio`；`/console` 是内部首页且能一路点到
捏画/捏集；内部壳的品牌 logo、返回项目列表、项目不存在回退都留在 `/console`
（不会把人甩进工作室）；内部深链 `/projects/<id>/reports` 直接可开。

### 6.2 客户端专用产物（`build:client`，给纯客户实例）

「这台机器只服务客户」的实例用这个（这也是本实例在双入口之前的形态）：
内部页（捏料/捏塘/捏镜/捏画/捏条/捏账）**不在产物里**，不是靠权限或开关藏的：
`vite build --mode client` 时路由生成器直接不进 `_app` 子树（`vite.config.ts` 的
`CLIENT_ROUTE_IGNORE_PATTERN`），产物里连内部页的文案都搜不到。

```powershell
cd frontend
$env:VITE_APP_VERSION="dev"    # 与现网 version.json 的 version 保持一致
npm run build:client           # = vite build --mode client，**故意不跑 tsc**
```

三个容易踩的点：

1. **`build:client` 不跑 `tsc -b`**，而 `build:ce` 跑。两棵路由树的 `FileRoutesByPath`
   声明同名不同型，放进同一个 tsc 程序必然冲突，所以 `src/client/` 整目录被
   `tsconfig.app.json` 排除、默认 `tsc -b` 永远看不到它。类型安全由 `build:ce`
   （内部树，含全部路由）把关：**客户端产物不额外增加检查，但改完仍要先跑
   `build:ce` 所在的 `tsc -b` 确认无类型错误。**
2. **客户端构建复用 CE 的实例配置**：`vite.config.ts` 在 `--mode client` 下把
   `.env.ce[.local]` 的 `VITE_*` 回灌进 `process.env`（Vite 的 `import.meta.env`
   只认「本次 mode 的 env 文件 + `process.env`」，光在配置里读一遍 `.env.ce*`
   只能拿到 dev 代理用的后端地址，隐藏开关不会生效 —— 症状就是产物里又出现
   `api.github.com`）。所以隐藏开关与署名文案不需要在 `.env.client` 里重复，
   `.env.client` 只放 `VITE_APP_MODE=client`。
3. `src/client/routeTree.client.gen.ts` 是构建时生成、**不入库**（已在 `.gitignore`）。
   内部树 `src/routeTree.gen.ts` 仍是入库文件，客户端构建**不会**改它。

客户端产物的自检（与第 2 节的隐藏开关自检一起跑）：

```powershell
# 内部页必须 0 命中：路由 id、页面文案、专用大 chunk
Select-String -Path dist\assets\*.js -Pattern "/_app" -SimpleMatch -List
Select-String -Path dist\assets\*.js -Pattern "捏料","捏账" -SimpleMatch -List
Get-ChildItem dist\assets | Where-Object { $_.Name -match "FreezoneCanvasRoot|ThreeDDirectorDialog" }

# 实例配置必须生效（第 2 节的反向证据）
Select-String -Path dist\assets\*.js -Pattern "api.github.com" -SimpleMatch -List   # 必须 0
Select-String -Path dist\assets\*.js -Pattern "announcements.empty.json" -SimpleMatch -List  # 必须 ≥1

# 工作室必须在产物里
Select-String -Path dist\assets\*.js -Pattern "拿捏漫剧工作台" -SimpleMatch -List

# 路由树只有 __root__ / login / studio
Select-String -Path src\client\routeTree.client.gen.ts -Pattern "id: '" | Select-Object -First 12
```

> `/projects/$project/...` 这类**路径字符串**会命中 1~2 处，这是共享常量不是内部页：
> 它们来自 `components/layout/project-navigation-routes.ts`，经 `themed-toaster`
> 从 `__root` 进图（toast 要按当前栏目算底距）。判定标准是「内部页的**组件与文案**
> 不在产物里、路由树里没有 `_app`」，不是「搜不到任何内部路径字面量」。

部署后（无痕窗口）确认：`/` 自动进 `/studio`；直接敲 `/projects/<id>/reports` 之类的
内部地址得到的是工作室 404 → 自动回 `/studio`；`/login` 仍可正常登录；工作室外壳
**底部**能看到「基于(Dramaclaw)开发」（署名原本挂在内部状态栏上，而状态栏属于 `_app`，
所以客户端外壳自己渲染一次 —— 见 `src/routes/studio.tsx`）。

## 7. 后端改动热补丁

API 的 `/app/src` **不是挂载**，代码烤在镜像里，所以后端改动要单独铺：

```bash
docker cp src/novelvideo/<changed>.py dramaclaw-api-1:/app/src/novelvideo/<same path>
docker restart dramaclaw-api-1
```

`docker restart` 只影响后端（在飞任务由 `/data/state` 恢复）；web 静态产物与它无关，
两边可以独立发布。部署前先 `docker exec dramaclaw-api-1 sh -c 'cat /app/src/novelvideo/...'`
记一下旧文件，回滚就是再 `docker cp` 回去 + 重启。

**行尾**：工作区里 `api/deps.py`、`chat/director_auto.py` 是 CRLF，直接 `scp` 会把
CRLF 带进容器、`md5` 对不上 `git show HEAD:<path>`。要传的是 **LF 字节**：

```powershell
git show HEAD:src/novelvideo/chat/director_auto.py > $env:TEMP\director_auto.py   # PowerShell 会写成 CRLF，别用
```

PowerShell 的重定向会再转成 CRLF，所以用一段脚本落盘（`git show` 的字节 → 写文件），
传完在服务器上 `md5sum` 与 `git show HEAD:<path>` 对一遍再 `docker cp`。判断「容器里的
版本是不是我改之前的那版」也靠这个 `md5sum` 对比：和 `HEAD~1` 一致才说明这是一次干净
的增量补丁（否则说明镜像里已有别人的改动，别覆盖）。

