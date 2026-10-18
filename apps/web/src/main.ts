import { createApp } from "vue";
import { createRouter, createWebHistory } from "vue-router";
import "element-plus/dist/index.css";
import "./style.css";
import App from "./App.vue";
import WorkbenchPage from "./pages/WorkbenchPage.vue";
import ConnectionsPage from "./pages/ConnectionsPage.vue";
import RadarPage from "./pages/RadarPage.vue";
import DecisionPage from "./pages/DecisionPage.vue";
import HistoryPage from "./pages/HistoryPage.vue";
import InvestigationsPage from "./pages/InvestigationsPage.vue";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/radar" },
    { path: "/radar", component: RadarPage },
    { path: "/decisions", component: DecisionPage },
    { path: "/history", component: HistoryPage },
    { path: "/topics", component: WorkbenchPage, props: { kind: "topics" } },
    {
      path: "/investigations",
      component: InvestigationsPage,
    },
    {
      path: "/deliverables",
      component: WorkbenchPage,
      props: { kind: "deliverables" },
    },
    { path: "/admin/data", component: ConnectionsPage },
    { path: "/:pathMatch(.*)*", redirect: "/radar" },
  ],
});
createApp(App).use(router).mount("#app");
