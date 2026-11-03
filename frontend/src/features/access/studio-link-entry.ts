// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import {
  activateClientSession,
  logoutAccessSession,
  setAccessNotice,
} from "@/lib/access-session";

/** 客户入口路由 `beforeLoad` 里拿到的最小 location 形状。 */
export interface ClientLinkLocation {
  pathname: string;
  hash: string;
  searchStr: string;
}

/**
 * 把专属链接里的 `?phone=&code=` 换成会话 cookie（`st_studio`），并当场从地址栏
 * 抹掉 query。返回**该跳转到的 href**（抹掉 query 后的同一路径）时，调用方必须
 * `throw redirect({ href, replace: true })`；返回 `null` 表示这次访问没带链接参数，
 * 照常走后续鉴权。
 *
 * 链接参数一旦出现就以链接为准：校验失败（或只带了一半）一律拦回门禁页，绝不回落
 * 到浏览器里已有的会话 cookie —— 否则「把 code 改成任意值也照样进」，门禁形同虚设。
 * 失败时顺带清掉旧会话（`st_studio` + `st_internal`）并留下一次性提示。
 *
 * 必须从**原始 query**（`location.searchStr`）读，不能读 `location.search`：
 * TanStack Router 的默认 `parseSearch` 会对每个值做 `JSON.parse`，
 * `?phone=13000000000` 因此被解析成 number，`typeof value === "string"` 会把手机号
 * 判成空串 —— 兑换被静默跳过，客户端永远停在门禁页（链接里的校验码再对也没用）。
 *
 * 内部端 `/studio` 与客户画布 `/canvas` 共用这一份；两边的门禁与兑换语义完全一致，
 * 只有「兑换后回到哪条路径」不同，所以这里只回 href、由调用方决定跳转。
 */
export async function exchangeClientLinkFromLocation(
  location: ClientLinkLocation,
): Promise<string | null> {
  const params = new URLSearchParams(location.searchStr);
  const phone = (params.get("phone") ?? "").trim();
  const code = (params.get("code") ?? "").trim();
  if (!phone && !code) return null;

  if (phone && code) {
    const result = await activateClientSession(phone, code);
    if (result.ok) {
      setAccessNotice(null);
    } else {
      setAccessNotice(result.error ?? "访问链接无效");
      // 链接无效时清掉旧会话并重置快照：重定向后重新探测得到 none，门禁页 + 报错
      // 落地；刷新也不会靠旧 cookie 溜进去。
      await logoutAccessSession();
    }
  } else {
    // 只带了一半参数（链接被截断、或被聊天软件改写）：明确提示，别静默回到门禁页，
    // 否则用户只会看到「需要专属访问链接」，无从判断是链接坏了还是没权限。
    setAccessNotice("访问链接不完整，请重新打开我们发给你的完整链接。");
    await logoutAccessSession();
  }

  // 校验码等价于密码：换到 cookie 之后立刻把 query 从地址栏与历史里抹掉（replace，
  // 不留下一条能按「后退」拿回校验码的记录）。
  return `${location.pathname}${location.hash}`;
}
