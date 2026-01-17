import {createRouter, createWebHistory} from 'vue-router'
import LoginView from '../views/LoginView.vue'
import HomeView from '../views/HomeView.vue'
import ShareView from '../views/ShareView.vue'
import MySharesView from '../views/MySharesView.vue'
import InboxView from '../views/InboxView.vue'
import SysDictView from '../views/SysDictView.vue'

const router = createRouter({
    history: createWebHistory(import.meta.env.BASE_URL),
    routes: [
        {
            path: '/',
            name: 'login',
            component: LoginView,
        },
        {
            path: '/home',
            name: 'home',
            component: HomeView,
            meta: {requiresAuth: true}
        },
        {
            path: '/shares',
            name: 'my-shares',
            component: MySharesView,
            meta: {requiresAuth: true}
        },
        {
            path: '/inbox',
            name: 'inbox',
            component: InboxView,
            meta: {requiresAuth: true}
        },
        {
            path: '/sys_dicts',
            name: 'sys-dicts',
            component: SysDictView,
            meta: {requiresAuth: true, requiresAdmin: true}
        },
        {
            path: '/s/:token',
            name: 'share',
            component: ShareView
        }
    ],
})

router.beforeEach((to, from, next) => {
    const token = localStorage.getItem('token')
    const userStr = localStorage.getItem('user')

    if (to.path === '/' && token) {
        next('/home')
    } else if (to.meta.requiresAuth && !token) {
        next('/')
    } else if (to.meta.requiresAdmin) {
        const user = JSON.parse(userStr || '{}')
        if (user.role !== 'admin') {
            next('/home')
        } else {
            next()
        }
    } else {
        next()
    }
})

export default router
