import { createRouter, createWebHistory } from "vue-router";
import { auth, checkSession } from "./api";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/login", component: () => import("./pages/LoginPage.vue") },
    { path: "/", component: () => import("./pages/AskPage.vue") },
    { path: "/analyses/:id", component: () => import("./pages/AnalysisPage.vue") },
    { path: "/topics", component: () => import("./pages/TopicsPage.vue") },
    { path: "/topics/:id", component: () => import("./pages/TopicPage.vue") },
    { path: "/monitor", component: () => import("./pages/MonitorPage.vue") },
    { path: "/:rest(.*)*", redirect: "/" },
  ],
});

router.beforeEach(async (to) => {
  if (to.path === "/login") return true;
  if (auth.signedIn === null) await checkSession();
  return auth.signedIn ? true : "/login";
});

export default router;
