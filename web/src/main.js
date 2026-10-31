import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHashHistory } from 'vue-router'
import App from './App.vue'
import './styles/global.css'

import ShelfView from './views/ShelfView.vue'
import OutlineView from './views/OutlineView.vue'
import WriteView from './views/WriteView.vue'
import ReviewView from './views/ReviewView.vue'
import CurateView from './views/CurateView.vue'
import ForeshadowView from './views/ForeshadowView.vue'
import DailyView from './views/DailyView.vue'
import BenchmarkView from './views/BenchmarkView.vue'
import NameView from './views/NameView.vue'
import PortfolioView from './views/PortfolioView.vue'
import UsageView from './views/UsageView.vue'
import ProvidersView from './views/ProvidersView.vue'
import FeedbackView from './views/FeedbackView.vue'
import TruthView from './views/TruthView.vue'
import NotFoundView from './views/NotFoundView.vue'

const routes = [
  { path: '/', redirect: '/shelf' },
  { path: '/shelf', name: 'shelf', component: ShelfView, meta: { title: '书架' } },
  { path: '/b/:book/outline', name: 'outline', component: OutlineView, meta: { title: '章纲工作台' } },
  { path: '/b/:book/write/:ch?', name: 'write', component: WriteView, meta: { title: '写作台' } },
  { path: '/b/:book/review/:ch?', name: 'review', component: ReviewView, meta: { title: '审稿台' } },
  { path: '/b/:book/curate/:ch?', name: 'curate', component: CurateView, meta: { title: 'Curator 确认台' } },
  { path: '/b/:book/foreshadow', name: 'foreshadow', component: ForeshadowView, meta: { title: '伏笔面板' } },
  { path: '/b/:book/daily', name: 'daily', component: DailyView, meta: { title: '日更面板' } },
  { path: '/b/:book/truth', name: 'truth', component: TruthView, meta: { title: '真相源' } },
  { path: '/b/:book/benchmark', name: 'benchmark', component: BenchmarkView, meta: { title: '拆书台' } },
  { path: '/b/:book/names', name: 'names', component: NameView, meta: { title: '起名' } },
  { path: '/b/:book/portfolio', name: 'portfolio', component: PortfolioView, meta: { title: '作品集' } },
  { path: '/b/:book/feedback', name: 'feedback', component: FeedbackView, meta: { title: '读者反馈' } },
  { path: '/usage', name: 'usage', component: UsageView, meta: { title: '用量' } },
  { path: '/providers', name: 'providers', component: ProvidersView, meta: { title: '模型路由' } },
  // 兜底：未知路由不能白屏。带 :book 的未知路径回到该书章纲，其余回书架。
  { path: '/b/:book/:rest(.*)*', name: 'book-not-found', component: NotFoundView, meta: { title: '页面不存在' } },
  { path: '/:pathMatch(.*)*', name: 'not-found', component: NotFoundView, meta: { title: '页面不存在' } },
]

const router = createRouter({ history: createWebHashHistory(), routes })

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')
