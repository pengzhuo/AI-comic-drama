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
| `VITE_HIDE_HEADER_SETTINGS=1` | 去掉内部端右上「设置」入口（连带锚在它上面的缺失配置提醒气泡） |
| `VITE_HIDE_RELEASE_POPUP=1` | 不自动弹更新公告 |
| `VITE_HIDE_OFFICIAL_CHANNELS=1` | 设置里只留自定义/本地网关 |
| `VITE_HIDE_MODEL_GUIDE=1` | 去掉「配置手册」引导链接 |
| `VITE_HIDE_GITHUB_STARS=1` | 不请求 `api.github.com` 拿星标数 |
| `VITE_LOGIN_ANNOUNCEMENTS_URL=/announcements.empty.json` | 公告改读本站空列表（完全静默，不出网） |
| `VITE_REQUIRE_INSTANCE_CONFIG=1` | **不是隐藏开关**：开启构建期严格校验——本节整组键（含 `VITE_INTERNAL_ENTRY_PATH`）必须齐全，缺任何一个镜像构建直接失败 |
| `VITE_BRAND_ATTRIBUTION=基于(Dramaclaw)开发` | **内容型**（不是隐藏开关）：底部状态栏右端的署名文案；未设置则不渲染 |

其中 `VITE_BRAND_ATTRIBUTION` 与其余「=1 即隐藏」的开关不同，它读的是**任意字符串**
（`frontend/src/lib/brand-attribution.ts` 的 `brandAttribution()`，刻意不放进
`ui-hide-flags.ts` —— 那是「只做减法」的布尔开关，而这条是上游永远不该出现的内容），
未设置或只有空白时返回 `null`、整个元素不渲染，所以上游/其他部署的产物一个字节都不会变。

它们是**编译期常量**：改了必须重新构建，只换产物文件不重建无效。服务器
`/root/dramaclaw/.env` 第 370 行附近也留着同一组值（供源码 `docker compose build web`
使用）；从服务器源码构建出来的镜像天然带这些开关，而本机热补丁产物**不会**自动继承
它们 —— 这就是 `.env.ce.local` 存在的意义。

> **整组键必须一起存在，一个都不能少。** 2026-11-09 又复发过一次：服务器 `.env` 里
> 少了 `VITE_INTERNAL_ENTRY_PATH=/console`（同时少了 `VITE_HIDE_GITHUB_STARS`、
> `VITE_LOGIN_ANNOUNCEMENTS_URL`、`VITE_BRAND_ATTRIBUTION`、`VITE_HIDE_HEADER_SETTINGS`），
> `docker compose build web` 于是产出**单入口内部产物**：客户 `/studio?phone=&code=`
> 被弹回内部端要内部口令，右上「设置」也重新露出来。旧脚本只对「非空」的值下断言，
> 空值静默通过 —— 所以现在**必须**在服务器 `.env` 设 `VITE_REQUIRE_INSTANCE_CONFIG=1`，
> 让 `verify:ce` 在缺键时硬失败（见 `frontend/scripts/verify-ce-bundle.mjs`）。
> 本机 `.env.ce.local` 同样要带全这组键。

## 2. 构建（在开发机）

```powershell
cd frontend
$env:VITE_APP_VERSION="dev"    # 与现网 version.json 的 version 保持一致
npm run build:ce               # tsc -b && vite build --mode ce（自动加载 .env.ce.local）
```

构建后先离线自检（`verify:ce` 就是镜像构建末尾那一支脚本；下面三条是最常看的人眼判据）：

```powershell
npm run verify:ce               # 断言产物确实带上了实例配置（脚本版，全绿才继续）

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

### 2.1 镜像构建（`docker compose build web`）也必须走 CE 形态

除了 `.env.ce.local` + 本机构建，**重建 web 镜像**这条路也常走。两种方式必须产出同一个形态，
否则又会踩同一个坑。2026-11-06 出过一次线上事故：用官方镜像里那条 `RUN pnpm build`
（production 模式）重建，`.env.ce.local` 不但没被加载，`.dockerignore` 里的 `.env.*` 还把它
挡在构建上下文之外 —— 产物丢掉全部实例配置（7 个隐藏开关、公告源、署名），并且
`VITE_INTERNAL_ENTRY_PATH` 变空 → `src/routes/studio.tsx` 的守卫把客户链接
`/studio?phone=&code=` 弹回内部首页 → 客户被要求输入**内部访问口令**。

现在这条链已经焊死，改任何一环都要同步：

- `frontend/Dockerfile` 用 `RUN pnpm build:ce`（不是 `build`），并为每个实例开关声明
  `ARG`/`ENV` —— `.dockerignore` 排除 `.env`/`.env.*`，镜像里读不到 `.env.ce.local`，
  只能靠 build args 传；每个 `ARG` 都写了注释说明为什么必须有。
- `docker-compose.yml` 的 `web.build.args` 从宿主 `.env` 转发同一组值（默认空 = 官方原样），
  并转发 `VITE_REQUIRE_INSTANCE_CONFIG`：实例在 `.env` 里设 `=1` 后，`verify:ce` 进入严格
  模式，缺任何一个实例键就让构建失败，而不是静默产出降级产物。
- 构建末尾 `RUN pnpm verify:ce`（= `frontend/scripts/verify-ce-bundle.mjs`）断言产物确实带上
  了这些值：`api.github.com` 必须 0 命中、公告源与署名必须命中、
  `VITE_INTERNAL_ENTRY_PATH` 必须进了内联的 `import.meta.env` 对象。不符直接
  `process.exit(1)` 让构建失败。注意 `VITE_HIDE_*` 是直接 `import.meta.env.X` 读的，
  压缩后产物里**看不到变量名**，所以脚本只对这 4 类「可观察后果」下断言、其余只打日志记录。
- `frontend/src/__tests__/dockerfile-build-args.test.ts` 守住三方一致：
  `verify-ce-bundle.mjs` 的 `INSTANCE_VARS` ⊆ `docker-compose.yml` 的 args ⊆ Dockerfile 的 `ARG`。

所以源码构建就是：

```bash
# 宿主 .env 里要带上 §1 那组值（服务器 /root/dramaclaw/.env 已有）
docker compose build web
```

如果只想拿「本机 `npm run build:ce` 的 dist」打一个持久镜像（不跑 vite、不装依赖），用最小的
两阶段 Dockerfile：`FROM nginx:... AS nginx-config-test` 自检 `docker/19-payment-form-csp.envsh`
+ `nginx.conf.template`，再 `COPY dist /usr/share/nginx/html`。**务必构建到
`--target nginx-config-test` 先过自检**（普通 `docker build` 只建最后阶段，会跳过 CSP 检查），
再用 `docker compose -f docker-compose.release.yml up -d --no-deps web` 换容器（`--no-deps`
保证不碰 api）。线上 `2.0.15-nn27` 就是这么打的。

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

### 6.3 客户画布专用产物（`build:canvas`，给「捏画」独立入口）

比工作室产物还窄：只留 `__root` / `login` / `canvas` —— 内部树（`_app`）与 `studio`
都在构建期被整棵排除（`vite.config.ts` 的 `CANVAS_ROUTE_IGNORE_PATTERN`）。适合
「只把画布开给客户」的实例：域名根地址 `/` 自动落 `/canvas`（无有效会话时是门禁页，
有会话是项目列表），`/canvas/<项目>` 是整屏画布，门禁沿用工作室的专属链接
（`?phone=&code=` → `st_studio` cookie），所以只给客户一条 `/canvas/...` 链接即可。

```powershell
cd frontend
$env:VITE_APP_VERSION="dev"    # 与现网 version.json 的 version 保持一致
npm run build:canvas           # = vite build --mode canvas，**故意不跑 tsc**
```

与 `build:client` 的两点差别：

1. 路由白名单更窄：`build:client` 保留 `studio`，`build:canvas` 连 `studio` 都排除；
   两者都一样**保留 `/canvas`**（所以工作室实例也能直达客户画布）。
2. 路由树落到 `src/client/routeTree.canvas.gen.ts`（同样构建时生成、不入库），
   `@/router-tree` 在 `--mode canvas` 下指向 `src/client/router-tree.canvas.ts`；
   内部树 `src/routeTree.gen.ts` 不会被改动。

第 6.2 节的三个易踩点（不跑 `tsc`、复用 `.env.ce.local`、生成文件不入库）对画布产物
**同样成立**，`.env.canvas` 也只放 `VITE_APP_MODE=canvas`。

自检：

```powershell
# 路由树只有 __root__ / login / canvas
Select-String -Path src\client\routeTree.canvas.gen.ts -Pattern "id: '" | Select-Object -First 12
Select-String -Path src\client\routeTree.canvas.gen.ts -Pattern "/studio" -SimpleMatch -List  # 必须 0

# 内部页必须 0 命中；客户画布的大 chunk 必须在
Select-String -Path dist\assets\*.js -Pattern "/_app" -SimpleMatch -List
Get-ChildItem dist\assets | Where-Object { $_.Name -match "FreezoneCanvasRoot" }
```

> 品牌文案「拿捏漫剧工作台」由客户外壳渲染，工作室与画布产物**都会**命中，不能当
> 「工作室是否残留」的判据；判定看路由树里有没有 `studio`。

部署后（无痕窗口）确认：`/` 自动进 `/canvas`；带 `?phone=&code=` 的专属链接能直接进
`/canvas/<项目>`；画布里的「返回项目列表」落在 `/canvas`；直接敲 `/studio` 或
`/projects/<id>/reports` 得到 404 → 自动回 `/canvas`。

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

## 8. 客户入口访问验证（`?phone=&code=`）+ 内部口令

现网形态：根地址给客户（`/studio`），内部端挂 `/console`。**只看路径分区不是访问控制**，
所以本实例额外开了服务端访问验证：

| 入口 | 凭据 | 结果 |
| --- | --- | --- |
| `/studio` | 专属链接 `?phone=<11位>&code=<32位hex>` | 换 `st_studio` cookie（7 天），**只看得到自己创建的项目** |
| `/console`（及所有内部页 / 内部深链） | 内部访问口令 | 换 `st_internal` cookie（30 天），看全部项目（含客户的） |
| 两者都没有 | — | 匿名：项目列表为空、单项目 404、建项目 403 |

校验码 = `md5(手机号 + client_secret)`，**不带时间戳**（链接长期有效、可转发；作废靠轮换
secret）。校验只在服务端做，前端拿不到 secret。隔离是**默认拒绝**：匿名请求连「项目存不
存在」都拿不到（404 而不是 403），`client:<手机号>` 是项目归属，项目目录落在
`/data/output/<手机号>/<项目>`。

**项目可见范围**（`src/novelvideo/ports/local/project.py:242`）：

- 内部身份 `user/local` → 全部项目（含所有客户的）；
- 客户身份 `client:<手机号>` → 只有 `owner_type=client` 且 `owner_id` 等于自己手机号的项目；
- 匿名 → 空。

这里**没有「把项目分配给某个客户」的机制**：内部端控制台里替客户建的项目 owner 是 `local`，
客户在自己的 `/studio` 里看不到它。客户要看到项目，得自己在 `/studio` 上传剧本（项目才归属到
该手机号）。这是当前的设计取舍，不是 bug。

**客户外壳（`/studio`）自带滚动容器**：全站基础样式把 `html/body/#root` 锁成
`height:100%; overflow:hidden`（`frontend/src/index.css`），所以任何挂在 `_app` 之外的整页外壳
都必须自己提供 `overflow-y-auto`（登录页、`/studio` 都是）。改 `/studio` 布局时别把
`routes/studio.tsx` 的 `min-h-0 flex-1 overflow-y-auto` 去掉，否则内容一超一屏就被裁掉。

### 8.1 开关与密钥（**不要加环境变量**）

加环境变量要重建容器，所以密钥放**可热补**的 secret 文件：

```bash
# 服务器上（api 容器挂载了 dramaclaw_ce-data → /data）
cat > /data/studio-access.secret <<'EOF'
client_secret=<客户密钥，只在运维处保存>
internal_key=<32 位随机串>
EOF
chmod 600 /data/studio-access.secret    # 必须 owner-only，别给 group/other
```

- 文件格式是 `key=value` 行，`#` 开头是注释；行首尾空白会被去掉。
- 读文件按 `(路径, mtime, size)` 缓存：**改一行即轮换**，所有既有会话下一次请求就失效，
  不需要重启。删掉文件 = 彻底关掉访问验证（回到原来的单用户行为）。
- 也可以走环境变量 `STUDIO_ACCESS_SECRET` / `STUDIO_INTERNAL_KEY`（优先级高于文件），
  但改它们要重建容器；现网用文件。另有 `STUDIO_ACCESS_SECRET_FILE` 可换路径。
- 日常轮换不用登服务器：内部端顶栏的**盾牌图标**进「访问设置」页改（见 §8.5）。网页写法
  同样是「原子替换 + 0600 + 保留注释」，且**从不回明文**（只回 `ab12…yz89` 这类掩码）。

第三个可写键是 `gate`：

| 值 | 含义 |
| --- | --- |
| 缺省 / `gate=on` | 正常：访问验证生效 |
| `gate=off` | **暂时关闭**：密钥全部保留，任何人可见全部项目（原单用户行为），内部口令在页面上还能一键恢复 |

`gate=off` 与「删文件」的差别：前者是页面上的「关闭访问验证」开关（可逆，运营自己就能开回来），
后者是运维兜底（密钥一起没了，必须重新写文件）。两者都不需要重启。

Cookie 属性：`HttpOnly` + `SameSite=Lax` + `Path=/`，`Secure` 只在请求确实是 HTTPS 时加
（纯 HTTP 上加 `Secure` 会被浏览器丢掉 → 门禁死循环，所以按请求判定而不是写死）。

### 8.2 发链接

```bash
# 服务器上（默认读 /data/studio-access.secret）
python3 scripts/studio_link.py --phone 13800000000 --base https://example.com

# 一次多人
python3 scripts/studio_link.py --phone 13800000000 --phone 13900000000 --base https://example.com
```

输出形如 `https://example.com/studio?phone=13800000000&code=<32位hex>`，每行一条。
本地临时生成可显式 `--secret`（或 `STUDIO_ACCESS_SECRET`）。算法与后端
`novelvideo.studio_access.expected_code` 一致，回归测试见 `tests/test_studio_link_script.py`。

### 8.3 内部口令门

内部入口没有有效 `st_internal` 时，整页显示口令输入框（`/console` 与所有内部页），
口令正确 → 整页重载进入。**客户 cookie 进内部端也会被拦**（带提示），所以一台浏览器只有
一个身份：想同时当客户和内部，用两个浏览器 / 无痕窗口。

### 8.4 部署与自检

后端是新增文件 + 改动，照第 7 节热补：

```bash
docker cp src/novelvideo/studio_access.py              dramaclaw-api-1:/app/src/novelvideo/studio_access.py
docker cp src/novelvideo/api/routes/access.py          dramaclaw-api-1:/app/src/novelvideo/api/routes/access.py
docker cp src/novelvideo/ports/project.py              dramaclaw-api-1:/app/src/novelvideo/ports/project.py
docker cp src/novelvideo/ports/local/project.py        dramaclaw-api-1:/app/src/novelvideo/ports/local/project.py
docker cp src/novelvideo/project_context.py            dramaclaw-api-1:/app/src/novelvideo/project_context.py
docker cp src/novelvideo/api/auth.py                   dramaclaw-api-1:/app/src/novelvideo/api/auth.py
docker cp src/novelvideo/api/__init__.py               dramaclaw-api-1:/app/src/novelvideo/api/__init__.py
docker cp src/novelvideo/api/routes/projects.py        dramaclaw-api-1:/app/src/novelvideo/api/routes/projects.py
docker cp src/novelvideo/api/routes/chat.py            dramaclaw-api-1:/app/src/novelvideo/api/routes/chat.py
docker cp /root/studio-access.secret                    dramaclaw-api-1:/data/studio-access.secret   # 先在开发机写文件，scp 到服务器再 docker cp
docker restart dramaclaw-api-1
```

自检（`cookie` 用登录/激活后从浏览器复制，或先 `POST /api/v1/access/client-session`）：

```bash
# 1) 匿名：列表空、单项目 404、建项目 403（注意**不是** 401，401 会被前端弹去 /login）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8780/api/v1/projects            # 200 + data: []
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8780/api/v1/projects/<别人的id>  # 404

# 2) 会话探测：无 cookie = none；带 cookie = client/internal
curl -s http://127.0.0.1:8780/api/v1/access/session

# 3) 拿客户 cookie（验证码用脚本生成的那条）
curl -s -i -X POST http://127.0.0.1:8780/api/v1/access/client-session \
  -H 'Content-Type: application/json' -d '{"phone":"13800000000","code":"<code>"}' | grep -i set-cookie

# 4) 建项目归属正确：data.owner_type=client、data.owner_id=<手机号>、output_dir 在 /data/output/<手机号>/
```

浏览器侧（无痕窗口）：
1. 打开 `https://<host>/studio?phone=...&code=...` → 地址栏里的 `phone/code` **立刻消失**
   （`redirect({href, replace})`，后退也拿不回来），列表里只有自己的项目。
2. 换发另一条链接给第二个手机号 → 两边互相看不到对方项目（同一项目名可以重复存在）。
3. 打开 `https://<host>/console` → 口令门；输错有提示、输对整页重载进内部首页，能看到
   **全部**项目（含客户的）。
4. 篡改链接里的 `code`（或手机号）→ 工作台显示「需要专属访问链接」+「访问链接无效」。

> **踩坑（2026-11 已修）**：`/studio` 的 `beforeLoad` 必须用 `location.searchStr` +
> `URLSearchParams` 读 `phone/code`，**不能**读 `location.search` —— TanStack Router 的默认
> `parseSearch` 会对每个 query 值做 `JSON.parse`，11 位手机号到那里已是 number，
> `typeof value === "string"` 会把它判成空串，于是兑换请求从不发出、客户端永远停在门禁页
> （链接和校验码完全正确也一样，服务端日志里看不到任何 `POST /access/client-session`）。
> 同类坑：`studio/$project.tsx` 的 `autostart`（`?autostart=1` 同样被解析成 number）。
> 回归测试见 `frontend/src/__tests__/routes/studio-link-exchange.test.ts`（按真实 number 形态断言）。

排查：`docker logs dramaclaw-api-1 | Select-String "studio|internal console"`（失败只在服务端
记日志，手机号只打后 4 位，口令永不入库/入日志）。API 的 `8780` 已用
`restrict-api-port.service` 收口（`DOCKER-USER`/`INPUT` 只放行 `172.16.0.0/12` 与
`127.0.0.0/8`，外部直连超时），但**隔离本身仍由 API 负责**：直连 `127.0.0.1:8780` 一样是
默认拒绝的。

### 8.5 访问设置页（顶栏盾牌图标）

内部身份（`st_internal` 有效）进顶栏的**盾牌图标** → `/access-settings`（客户身份会被送回
`/console`，那里是口令门）。页面四张卡：内部口令、客户链接密钥、发客户链接（一次出工作台
`/studio` + 捏画 `/canvas` 两条）、危险开关。

接口（都挂在 `/api/v1`，`401` 一律不用，避免前端全局 401 → `/login`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/access/config` | 掩码状态（`/data/studio-access.secret` 路径、最近更新时间、每个键的来源） |
| `POST` | `/access/config` | `{internal_key?, client_secret?}`，空串 = 不改；返回新状态 + `notices` |
| `POST` | `/access/config/link` | `{phone, project?}` → `{phone, code, link, canvas_link}`（`link`=`/studio`，`canvas_link`=`/canvas[/<项目>]`，同一 `code`；`STUDIO_BASE_URL` 优先，否则按 `X-Forwarded-Host` + scheme 拼） |
| `POST` | `/access/gate` | `{enabled, confirm_key}`；关闭必须带当前内部口令，开启可一并带新密钥 |

安全模型：

- **GET 永不回明文**，只有掩码；随机口令由浏览器 `crypto.getRandomValues` 生成后显示在
  输入框里，服务端只存提交值 —— 生成完必须当场保存，刷新拿不回来。
- 改 `internal_key` 会**当场给操作者重签一张 `st_internal`**（不然轮换口令等于把自己踢出去），
  其他设备上的内部登录立即失效；改 `client_secret` 则让此前发出的所有客户链接失效。
- 键由环境变量提供时（`env_locked`）输入框和按钮禁用，并说明「改文件不生效」。
- **关闭访问验证**要重输当前内部口令才能确认（`confirm_key`），写 `gate=off`，密钥保留；
  重新开启一键恢复，旧客户链接继续有效。客户密钥缺失时「重新开启」按钮置灰。
- 门禁开启时，访问设置接口只认内部身份（`403`）；门禁关闭（`gate=off`）时对外**只回 404**，
  持内部 cookie 的操作者仍可读写 —— 这样关掉之后还能自己开回来，而掩码不会裸奔。
- 暴力猜测有内存级限流：`internal-session` 与设置写接口同一 IP 5 分钟 8 次失败 → `429`
  （成功即清零；api 是单进程 uvicorn，进程内计数足够）。计数按 `X-Forwarded-For` 首段，
  伪造该头可绕过——它是减速带，真正的门槛是 32 位随机口令 + 掩码不回明文。

服务器上不需要额外操作。想直接改也可以：

```bash
# 关闭（保留密钥，页面可恢复）
docker exec dramaclaw-api-1 sh -c 'printf "gate=off\n" >> /data/studio-access.secret'
# 恢复：把 gate=off 改成 gate=on（用 sed 或直接在设置页点「重新开启」）
```

宿主机保留一份**只读副本**（`/root` 是 0700，副本本身也是 0600），方便运维不用进容器就能看到
当前值：`studio-access-sync.timer` 每 2 分钟把容器里的文件同步到
`/root/.deploy/studio-access.secret`（`scripts/studio-access-sync.sh`，另有 `show` 子命令）。
副本只读不写——改值一律走设置页或容器内文件，避免两边打架。

自检（在服务器上，`curl` 直连本机 8780）：

```bash
# 匿名/客户一律 403；内部 200 且响应里没有任何明文密钥
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8780/api/v1/access/config
curl -s -b "st_internal=<内部 cookie>" http://127.0.0.1:8780/api/v1/access/config   # mask 而非明文
# 连错 9 次：前 8 次 400，第 9 次 429
for i in $(seq 1 9); do curl -s -o /dev/null -w '%{http_code} ' -X POST \
  http://127.0.0.1:8780/api/v1/access/internal-session \
  -H 'Content-Type: application/json' -d '{"key":"wrong"}'; done; echo
```

浏览器侧（无痕窗口）：`/console` 输口令 → 点盾牌 → 改内部口令（本页自动续期）→ 用新口令在
另一个无痕窗口重新进入；改客户密钥后用旧链接应报「访问链接无效」，用页面新生成的链接可进。

