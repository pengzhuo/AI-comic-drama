// SPDX-License-Identifier: Elastic-2.0
// Copyright (c) 2026 ClaymoreLab
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * 构建期接口防漂移。
 *
 * 实例级配置（隐藏开关、公告源、`VITE_INTERNAL_ENTRY_PATH`、署名）是构建期写进产物
 * 的，镜像构建读不到 `.env.ce.local`（`.dockerignore` 排除了 `.env.*`），只能靠 build
 * args 传。谁漏了一环，值就被静默丢掉 —— 2026-11-06 的线上事故正是这个形状：双入口
 * 开关没进产物，客户工作室链接被弹回内部端要内部口令。见
 * docs/agents/web-hotpatch-deploy.md §1/§6。
 *
 * 不变量（三个仓内文件，不需要第三份手写清单）：
 *   scripts/verify-ce-bundle.mjs 的 INSTANCE_VARS（实例接口）
 *     ⊆ docker-compose.yml 的 web.build.args（源码构建必须供给）
 *     ⊆ frontend/Dockerfile 的 ARG（镜像必须接收）
 */

// 与仓库既有测试一致：路径相对 `frontend/`（vitest 的 cwd）解析。
const dockerfileText = readFileSync("Dockerfile", "utf8");
const composeText = readFileSync(resolve(process.cwd(), "../docker-compose.yml"), "utf8");
const verifyScriptText = readFileSync("scripts/verify-ce-bundle.mjs", "utf8");

/** 实例接口：断言脚本认为「必须传到构建里」的那组变量。 */
function instanceVarNames(script: string): string[] {
  const block = /const INSTANCE_VARS = \[([\s\S]*?)\];/.exec(script);
  expect(block, "verify-ce-bundle.mjs must declare INSTANCE_VARS").not.toBeNull();
  return [...block![1].matchAll(/"([A-Z0-9_]+)"/g)].map((match) => match[1]);
}

/** Dockerfile 里声明的 ARG 名字集合。 */
function dockerfileArgNames(dockerfile: string): Set<string> {
  return new Set([...dockerfile.matchAll(/^ARG\s+([A-Z0-9_]+)/gm)].map((match) => match[1]));
}

/** docker-compose.yml 中 web 服务的 build.args 键名（按缩进切块，避免依赖 YAML 解析）。 */
function composeWebBuildArgNames(compose: string): string[] {
  const lines = compose.split(/\r?\n/);
  const start = lines.findIndex((line) => /^ {2}web:\s*$/.test(line));
  expect(start, "docker-compose.yml must declare a web service").toBeGreaterThanOrEqual(0);

  let end = lines.length;
  for (let i = start + 1; i < lines.length; i += 1) {
    if (/^ {2}[A-Za-z0-9_-]+:/.test(lines[i])) {
      end = i;
      break;
    }
  }

  const block = lines.slice(start + 1, end);
  const argsAt = block.findIndex((line) => /^ {6}args:\s*$/.test(line));
  expect(argsAt, "the web service must declare build.args").toBeGreaterThanOrEqual(0);

  const names: string[] = [];
  for (let i = argsAt + 1; i < block.length; i += 1) {
    const line = block[i];
    if (line.trim() === "" || line.trimStart().startsWith("#")) continue;
    const match = /^ {8}([A-Z0-9_]+)\s*:/.exec(line);
    if (!match) break;
    names.push(match[1]);
  }
  return names;
}

describe("web image build interface", () => {
  it("declares every instance var the verify script asserts on", () => {
    const declared = dockerfileArgNames(dockerfileText);
    const missing = instanceVarNames(verifyScriptText).filter((name) => !declared.has(name));
    expect(missing).toEqual([]);
  });

  it("forwards every instance var from docker-compose.yml", () => {
    const forwarded = composeWebBuildArgNames(composeText);
    const missing = instanceVarNames(verifyScriptText).filter(
      (name) => !forwarded.includes(name),
    );
    expect(missing).toEqual([]);
  });

  it("only forwards args the Dockerfile actually accepts", () => {
    const declared = dockerfileArgNames(dockerfileText);
    const unknown = composeWebBuildArgNames(composeText).filter((name) => !declared.has(name));
    expect(unknown).toEqual([]);
  });

  it("builds the CE bundle and runs the bundle assertion", () => {
    expect(dockerfileText).toMatch(/RUN\s+pnpm\s+build:ce\b/);
    expect(dockerfileText).toMatch(/pnpm\s+verify:ce\b/);
    // 生产模式构建会丢掉全部 `.env.ce*` 实例配置，必须禁止回退。
    expect(dockerfileText).not.toMatch(/RUN\s+pnpm\s+build(?!:)/);
  });
});
