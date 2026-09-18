import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import Dashboard from './views/Dashboard.vue'
import ActivityDetail from './views/ActivityDetail.vue'
import PlanView from './views/PlanView.vue'
import ChatView from './views/ChatView.vue'
import Places from './views/Places.vue'
import Shoes from './views/Shoes.vue'
import Knowledge from './views/Knowledge.vue'
import Profile from './views/Profile.vue'
import './style.css'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'dashboard', component: Dashboard },
    { path: '/activity/:id', name: 'activity', component: ActivityDetail },
    { path: '/plan', name: 'plan', component: PlanView },
    { path: '/chat', name: 'chat', component: ChatView },
    { path: '/places', name: 'places', component: Places },
    { path: '/shoes', name: 'shoes', component: Shoes },
    { path: '/knowledge', name: 'knowledge', component: Knowledge },
    { path: '/profile', name: 'profile', component: Profile },
  ],
})

createApp(App).use(router).mount('#app')
