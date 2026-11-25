// Vue 应用入口：加载页面、全局样式，并挂载到 index.html 的 #app 节点。
import { createApp } from "vue";
import App from "./App.vue";
import "./style.css";

createApp(App).mount("#app");
