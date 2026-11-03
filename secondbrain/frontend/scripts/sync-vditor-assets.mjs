/**
 * 把 Vditor 的运行时资源拷进 public/vditor/。
 *
 * 为什么要拷：Vditor 默认从 unpkg.com CDN 取 lute.min.js / i18n / icons，
 * 而 SecondBrain 是**本机离线**应用 —— CDN 取不到时编辑器框架能出来，
 * 但内容写不进去（setValue 抛 "Cannot read properties of undefined"）。
 *
 * 为什么不在 git 里存这些文件：它们是 node_modules 的副本，
 * 用 prebuild 钩子自动同步，仓库才不会多 3.8MB 二进制。
 *
 * 只拷运行时真正会加载的几个（其余如 mermaid/katex/plantuml 用不到）。
 */
import { cpSync, existsSync, mkdirSync, rmSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = dirname(fileURLToPath(import.meta.url))
const src = join(root, '..', 'node_modules', 'vditor', 'dist')
const dest = join(root, '..', 'public', 'vditor')

if (!existsSync(src)) {
  console.error('[vditor-assets] 找不到 node_modules/vditor，先跑 npm install')
  process.exit(1)
}

rmSync(dest, { recursive: true, force: true })

const items = [
  ['js/lute/lute.min.js', 'dist/js/lute/lute.min.js'],
  ['js/i18n/zh_CN.js', 'dist/js/i18n/zh_CN.js'],
  ['js/i18n/en_US.js', 'dist/js/i18n/en_US.js'],
  ['js/icons/ant.js', 'dist/js/icons/ant.js'],
  ['images/emoji', 'dist/images/emoji'],
  // v36：深色主题的**预览区内容样式**。
  // Vditor 的 `preview.theme.current = 'dark'` 会去
  // {cdn}/dist/css/content-theme/dark.css 取这个文件，缺了右侧预览还是白底。
  ['css/content-theme/dark.css', 'dist/css/content-theme/dark.css']
]

for (const [from, to] of items) {
  const s = join(src, from)
  if (!existsSync(s)) {
    console.warn(`[vditor-assets] 跳过缺失资源：${from}`)
    continue
  }
  const d = join(dest, to)
  mkdirSync(dirname(d), { recursive: true })
  cpSync(s, d, { recursive: true })
}

console.log('[vditor-assets] 已同步 Vditor 运行时资源到 public/vditor/')
