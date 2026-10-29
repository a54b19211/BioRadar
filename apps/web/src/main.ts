import { createApp } from "vue";
import { createRouter, createWebHistory } from "vue-router";
import "element-plus/dist/index.css";
import "./style.css";
import App from "./App.vue";
import ConnectionsPage from "./pages/ConnectionsPage.vue";
import RadarPage from "./pages/RadarPage.vue";
import DecisionPage from "./pages/DecisionPage.vue";
import HistoryPage from "./pages/HistoryPage.vue";
import InvestigationsPage from "./pages/InvestigationsPage.vue";
import TopicsPage from "./pages/TopicsPage.vue";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/investigations" },
    { path: "/radar", component: RadarPage },
    { path: "/decisions", component: DecisionPage },
    { path: "/history", component: HistoryPage },
    { path: "/topics", component: TopicsPage },
    {
      path: "/investigations",
      component: InvestigationsPage,
    },
    {
      path: "/deliverables",
      component: InvestigationsPage,
    },
    { path: "/admin/data", component: ConnectionsPage },
    { path: "/:pathMatch(.*)*", redirect: "/radar" },
  ],
});
createApp(App).use(router).mount("#app");
