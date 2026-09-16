/**
 * 可选工具：校验 Markdown 中所有 ```mermaid 代码块能否被 Mermaid 官方解析器解析。
 * 图有语法错误时退出码为 1，可在文档改动后做一次快速回归（不属于 pytest 套件）。
 *
 * 用法（需要 Node 18+，仓库本身仍是纯 Python、无 Node 依赖）：
 *
 *   npm i mermaid@11 jsdom@24        # 在任意目录安装一次
 *   $env:MERMAID_DEPS = "<该目录>"   # PowerShell；bash 用 MERMAID_DEPS=... export
 *   node docs/check_flowchart.mjs docs/FLOWCHART.md
 *
 * 依赖目录的查找顺序：$MERMAID_DEPS → 当前工作目录（向上逐级） → 脚本所在目录。
 * 注意 jsdom 必须是 24.x：更高版本为纯 ESM，会被 CJS 依赖链 require 失败。
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));

/** 从 startDir 起向上查找已安装的 npm 包目录。 */
function findPackage(name, startDir) {
  let dir = path.resolve(startDir);
  for (;;) {
    const candidate = path.join(dir, "node_modules", name);
    if (fs.existsSync(path.join(candidate, "package.json"))) return candidate;
    const parent = path.dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

/** 读取 package.json 的 ESM 入口，转成可直接 import 的 file URL。 */
function entryUrl(packageDir) {
  const manifest = JSON.parse(fs.readFileSync(path.join(packageDir, "package.json"), "utf8"));
  const dot = manifest.exports?.["."] ?? manifest.exports;
  const entry =
    (typeof dot === "string" ? dot : null) ??
    dot?.import?.default ??
    dot?.import ??
    dot?.default ??
    manifest.module ??
    manifest.main ??
    "index.js";
  return pathToFileURL(path.join(packageDir, entry)).href;
}

const searchRoots = [process.env.MERMAID_DEPS, process.cwd(), scriptDir].filter(Boolean);
const resolved = (() => {
  for (const root of searchRoots) {
    const jsdomDir = findPackage("jsdom", root);
    const mermaidDir = findPackage("mermaid", root);
    if (jsdomDir && mermaidDir) return { jsdomDir, mermaidDir };
  }
  return null;
})();

if (!resolved) {
  console.error("未找到 mermaid / jsdom。请先安装一次：npm i mermaid@11 jsdom@24");
  console.error("并设置 MERMAID_DEPS 指向含 node_modules 的目录（或在该目录下执行本脚本）。");
  process.exit(2);
}

const jsdomModule = await import(entryUrl(resolved.jsdomDir));
const JSDOM = jsdomModule.JSDOM ?? jsdomModule.default?.JSDOM;

// mermaid 在 Node 下需要最小 DOM 环境；必须早于 mermaid 的 import，
// 否则其内部依赖（dompurify）会拿到 Node 版工厂函数而不是实例。
const dom = new JSDOM("<!DOCTYPE html><html><body></body></html>", { pretendToBeVisual: true });
global.window = dom.window;
global.document = dom.window.document;
Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator, configurable: true });
global.SVGElement = dom.window.SVGElement;
global.Element = dom.window.Element;
global.HTMLElement = dom.window.HTMLElement;
global.Node = dom.window.Node;
global.getComputedStyle = dom.window.getComputedStyle.bind(dom.window);
global.requestAnimationFrame = (callback) => setTimeout(() => callback(Date.now()), 0);

const mermaid = (await import(entryUrl(resolved.mermaidDir))).default;

const target = process.argv[2] ?? path.join(scriptDir, "FLOWCHART.md");
const markdown = fs.readFileSync(target, "utf8");
const blocks = [...markdown.matchAll(/```mermaid\r?\n([\s\S]*?)```/g)].map((match) => match[1].trim());

mermaid.initialize({ startOnLoad: false, securityLevel: "loose" });

let failures = 0;
for (const [index, code] of blocks.entries()) {
  const label = `diagram ${index + 1}`;
  try {
    const parsed = await mermaid.parse(code);
    console.log(`${label}: OK  (type=${parsed?.diagramType})`);
  } catch (error) {
    failures += 1;
    const message = String(error?.message ?? error).split("\n").slice(0, 5).join(" | ");
    console.log(`${label}: FAIL -> ${message}`);
  }
}

console.log(`checked ${blocks.length} diagram(s) in ${target}; failures=${failures}`);
process.exit(failures === 0 && blocks.length > 0 ? 0 : 1);
