import { createRouter, createWebHistory } from 'vue-router'

/**
 * 六区（每区独立页面，不做长滚动合并）：
 *   首页   /                  搜索（上）+ 图谱（中）+ 回顾（下）
 *   采集区 /workbench         收集素材 + 图文加工存储（经济工具拿原文）
 *   加工区 /process           核准 + 解析（先设模板/字数 → AI 总结 → 不满意附手稿）
 *   原创区 /original          写自己的东西（空模板骨架，与加工区并列）
 *   管理区 /manage            设置各项数据（分区/标签/模板/Prompt/维护/平台能力）
 *   设置   /settings          AI / 存储 / 系统（Tab 与管理区同款）
 *   状态   /status            任务流水线完整页（**非一级导航**，
 *                               从采集区/加工区的状态横条「全部 →」跳入）
 */
const routes = [
  { path: '/', name: 'home', component: () => import('@/views/HomeView.vue'), meta: { title: '每周回顾' } },
  {
    path: '/workbench',
    name: 'workbench',
    component: () => import('@/views/WorkbenchView.vue'),
    meta: { title: '采集区' }
  },
  {
    path: '/process',
    name: 'process',
    component: () => import('@/views/ProcessView.vue'),
    meta: { title: '加工区' }
  },
  {
    path: '/original',
    name: 'original',
    component: () => import('@/views/OriginalView.vue'),
    meta: { title: '原创区' }
  },
  {
    path: '/manage',
    name: 'manage',
    component: () => import('@/views/ManageView.vue'),
    meta: { title: '管理区' }
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('@/views/SettingsView.vue'),
    meta: { title: '设置' }
  },
  {
    path: '/status',
    name: 'status',
    component: () => import('@/views/StatusView.vue'),
    meta: { title: '任务状态' }
  },
  {
    path: '/note/:path(.*)',
    name: 'note',
    component: () => import('@/views/NoteView.vue'),
    meta: { title: '阅读' }
  },

  // ---- 旧入口（重定向，书签与历史不失效）----
  { path: '/insight', redirect: '/' },
  { path: '/settings/storage', redirect: '/settings' },
  { path: '/settings/system', redirect: '/settings' },
  { path: '/process/parse', redirect: '/process' },
  { path: '/process/compose', redirect: '/original' },
  { path: '/features', redirect: '/manage' },
  { path: '/collect', redirect: '/workbench' },
  { path: '/queue', redirect: '/workbench' },
  { path: '/review', redirect: '/process' },
  { path: '/write', redirect: '/original' },
  { path: '/search', redirect: '/' },
  { path: '/recap', redirect: '/' },
  { path: '/tags', redirect: '/manage' }
]

const router = createRouter({
  history: createWebHistory(),
  routes
})

router.afterEach((to) => {
  document.title = to.meta?.title
    ? `${to.meta.title} · SecondBrain`
    : 'SecondBrain'
})

export default router
