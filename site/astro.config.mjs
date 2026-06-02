import { defineConfig } from 'astro/config';

export default defineConfig({
  site: 'https://musictidy.com',
  // 静态生成（CF Pages 直接 host dist/）
  output: 'static',
  build: {
    inlineStylesheets: 'auto',
  },
});
