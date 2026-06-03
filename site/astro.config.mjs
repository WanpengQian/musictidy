import { defineConfig } from 'astro/config';

export default defineConfig({
  site: 'https://musictidy.com',
  // 静态生成（CF Pages 直接 host dist/）
  output: 'static',
  // 三语：zh 默认走 /，英 /en/，日 /ja/。Astro 自带 i18n 路由。
  i18n: {
    defaultLocale: 'zh',
    locales: ['zh', 'en', 'ja'],
    routing: {
      prefixDefaultLocale: false,
    },
  },
  build: {
    inlineStylesheets: 'auto',
  },
});
