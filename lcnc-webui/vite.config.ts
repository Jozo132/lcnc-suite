import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  build: {
    assetsDir: 'static',  // avoid conflict with gateway /assets mount (machine STLs)
  },
  server: {
    proxy: {
      '/ws': {
        target: 'http://127.0.0.1:8000',
        ws: true,
      },
      '/files': 'http://127.0.0.1:8000',
      '/gcode': 'http://127.0.0.1:8000',
      '/preview': 'http://127.0.0.1:8000',
      '/surface_points': 'http://127.0.0.1:8000',
      '/comp_grid': 'http://127.0.0.1:8000',
      '/upload': 'http://127.0.0.1:8000',
      '/save': 'http://127.0.0.1:8000',
      '/hal': 'http://127.0.0.1:8000',
      '/g30': 'http://127.0.0.1:8000',
      '/assets': 'http://127.0.0.1:8000',
      '/import-tool-library': 'http://127.0.0.1:8000',
      '/telemetry': 'http://127.0.0.1:8000',
    },
  },
})
