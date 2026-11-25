// 仅供隔离验证：后端 8001，前端 5174，避免与用户的开发服务冲突。
import { defineConfig } from "vite";
import base from "./vite.config";
export default defineConfig({
  ...base,
  server: {
    ...base.server,
    port: 5174,
    proxy: {
      "/api": { target: "http://127.0.0.1:8001", proxyTimeout: 200000 },
    },
  },
});
