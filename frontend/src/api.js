import axios from 'axios'

const api = axios.create({ baseURL: '/api', timeout: 180000 })

const uploadFile = (file, path) => {
  const fd = new FormData()
  fd.append('file', file)
  return api.post(path, fd, { headers: { 'Content-Type': 'multipart/form-data' } })
}

export default {
  activities: (params) => api.get('/activities', { params }),
  activity: (id) => api.get(`/activities/${id}`),
  track: (id) => api.get(`/activities/${id}/track`),
  deleteActivity: (id) => api.delete(`/activities/${id}`),
  upload: (file) => uploadFile(file, '/activities/upload'),
  probe: (file) => uploadFile(file, '/activities/probe'),
  manualActivity: (data) => api.post('/activities/manual', data),
  recognize: (imageBase64) => api.post('/activities/recognize', { image_base64: imageBase64 }),
  setFeedback: (id, data) => api.post(`/activities/${id}/feedback`, data),
  setHeartRate: (id, data) => api.post(`/activities/${id}/heart-rate`, data),

  summary: (days) => api.get('/analytics/summary', { params: { days } }),
  trend: (days) => api.get('/analytics/trend', { params: { days } }),
  metrics: () => api.get('/analytics/metrics'),
  loadCurve: (days) => api.get('/analytics/load', { params: { days } }),
  recalc: () => api.post('/analytics/recalc'),

  getProfile: () => api.get('/profile'),
  updateProfile: (data) => api.put('/profile', data),
  daily: (days) => api.get('/profile/daily', { params: { days } }),
  upsertDaily: (data) => api.post('/profile/daily', data),

  generatePlan: (data) => api.post('/agent/plan/generate', data),
  confirmPlan: (threadId) => api.post('/agent/plan/confirm', { thread_id: threadId }),
  chat: (message, threadId) => api.post('/agent/chat', { message, thread_id: threadId }),
  plans: (limit = 10) => api.get('/agent/plans', { params: { limit } }),
  deletePlan: (id) => api.delete(`/agent/plans/${id}`),

  // 去过的城市（地图点亮）
  citySearch: (q) => api.get('/places/cities', { params: { q } }),
  visitedCities: () => api.get('/places/visited'),
  addVisitedCity: (data) => api.post('/places/visited', data),
  deleteVisitedCity: (id) => api.delete(`/places/visited/${id}`),

  // 跑鞋管理
  shoes: () => api.get('/shoes'),
  createShoe: (data) => api.post('/shoes', data),
  updateShoe: (id, data) => api.patch(`/shoes/${id}`, data),
  deleteShoe: (id) => api.delete(`/shoes/${id}`),
  uploadShoeImage: (id, file) => uploadFile(file, `/shoes/${id}/image`),
  shoeMileage: (id) => api.get(`/shoes/${id}/mileage`),
  addMileage: (id, data) => api.post(`/shoes/${id}/mileage`, data),
  deleteMileage: (mid) => api.delete(`/shoes/mileage/${mid}`),
  activityCalendar: (year, month) => api.get('/activities/calendar', { params: { year, month } }),

  // 执行链路追踪
  trace: (sinceId = 0) => api.get('/trace/recent', { params: { since_id: sinceId } }),

  // 训练知识库
  kbArticles: (params) => api.get('/knowledge/articles', { params }),
  kbArticle: (id) => api.get(`/knowledge/articles/${id}`),
  kbSearch: (q, topN = 6) => api.get('/knowledge/search', { params: { q, top_n: topN } }),
  kbStats: () => api.get('/knowledge/stats'),
  kbCategories: () => api.get('/knowledge/categories'),
  kbRuns: (limit = 20) => api.get('/knowledge/runs', { params: { limit } }),
  kbRun: (id) => api.get(`/knowledge/runs/${id}`),
  kbCrawl: (data) => api.post('/knowledge/crawl', data),
  kbCrawlStatus: () => api.get('/knowledge/crawl/status'),
  kbUpdateSource: (id, data) => api.put(`/knowledge/sources/${id}`, data),
}
