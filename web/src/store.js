import { defineStore } from 'pinia'
import { api } from './api/client'

let toastSeq = 0

export const useAppStore = defineStore('app', {
  state: () => ({
    shelf: { novels: [] },
    currentBook: null,
    toasts: [],
    // 各页共享的章节列表缓存
    chapters: [],
    loadedBook: null,
  }),
  actions: {
    toast(text, kind = '') {
      const id = ++toastSeq
      this.toasts.push({ id, text, kind })
      setTimeout(() => {
        this.toasts = this.toasts.filter((t) => t.id !== id)
      }, kind === 'err' ? 6000 : 3200)
    },
    async refreshShelf() {
      try {
        this.shelf = await api.shelf()
      } catch (e) {
        this.toast(`书架加载失败：${e.message}`, 'err')
      }
    },
    async loadChapters(dir) {
      const r = await api.chapters(dir)
      this.chapters = r.chapters || []
      this.loadedBook = dir
      return this.chapters
    },
  },
})
