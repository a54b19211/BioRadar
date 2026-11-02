import { createApp } from "vue";
import ElementPlus, { ElMessage } from "element-plus";
import zhCn from "element-plus/es/locale/lang/zh-cn";
import "element-plus/dist/index.css";
import App from "./App.vue";
import router from "./router";
import "./style.css";

const app = createApp(App);
// Failed requests throw ApiError inside event handlers and page setup; show them all in one place.
app.config.errorHandler = (err) => {
  console.error(err);
  ElMessage.error(err instanceof Error ? err.message : String(err));
};
app.use(ElementPlus, { locale: zhCn }).use(router).mount("#app");
