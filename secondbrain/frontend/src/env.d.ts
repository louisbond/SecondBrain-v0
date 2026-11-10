/**
 * 类型检查用的环境声明（v42）。
 *
 * 项目是 JavaScript + Vue SFC，不是 TypeScript。开 `checkJs` 后 tsc 需要知道
 * `.vue` 模块长什么样、window 上挂了什么全局量。这里只放声明，无运行时代码。
 *
 * 注意本文件**不能**有顶层 import/export：一旦变成 module，
 * `declare module '*.vue'` 就会被当成"模块增强"而不是全局 ambient 声明，
 * 于是所有 .vue 的 import 仍然报 TS2307。
 */

declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  // SFC 模板不做类型检查，给一个通用组件类型让 import 语句通过即可。
  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}

// 全局脚本里的 interface 会合并进 lib.dom 的 Window
interface Window {
  /** App.vue 挂载后挂上来的 toast，供 main.js 的全局错误兜底复用。 */
  __sbToast?: (message: string, type?: string, timeout?: number) => void
}
