/**
 * Cloudflare Worker：Kimi Code API 跨域代理
 *
 * 作用：
 * 1. 解决 GitHub Pages 调用 api.kimi.com 时的 CORS 跨域问题
 * 2. 转发访客的 Authorization 头，让访客使用自己的 Kimi Code API Key
 * 3. 本 Worker 本身不保存任何 API Key，也不产生费用
 *
 * 部署步骤：
 * 1. 登录 https://dash.cloudflare.com/，进入 Workers & Pages
 * 2. 创建新 Service（如 kimi-code-proxy）
 * 3. 把本文件内容粘贴进去，保存部署
 * 4. 得到 Worker URL（如 https://kimi-code-proxy.xxx.workers.dev）
 * 5. 把 URL 填到 index.html 的「Kimi Code 代理服务地址」框里
 */

const ALLOWED_ORIGINS = [
  'https://lynnc915.github.io',
  'http://localhost:8080',
  'http://127.0.0.1:8080',
];

export default {
  async fetch(request, env, ctx) {
    const origin = request.headers.get('Origin') || '*';

    // 处理预检请求
    if (request.method === 'OPTIONS') {
      return new Response(null, {
        status: 204,
        headers: {
          'Access-Control-Allow-Origin': origin,
          'Access-Control-Allow-Methods': 'POST, OPTIONS',
          'Access-Control-Allow-Headers': 'Content-Type, Authorization',
          'Access-Control-Max-Age': '86400',
        },
      });
    }

    // 简单来源校验（可选，防止 Worker 被滥用）
    const isAllowed = ALLOWED_ORIGINS.includes(origin) || origin === '*';
    if (!isAllowed) {
      return new Response(JSON.stringify({ error: 'Origin not allowed' }), {
        status: 403,
        headers: {
          'Content-Type': 'application/json',
          'Access-Control-Allow-Origin': origin,
        },
      });
    }

    if (request.method !== 'POST') {
      return new Response(JSON.stringify({ error: 'Method not allowed' }), {
        status: 405,
        headers: {
          'Content-Type': 'application/json',
          'Access-Control-Allow-Origin': origin,
        },
      });
    }

    const authHeader = request.headers.get('Authorization');
    if (!authHeader) {
      return new Response(JSON.stringify({ error: 'Authorization header required' }), {
        status: 401,
        headers: {
          'Content-Type': 'application/json',
          'Access-Control-Allow-Origin': origin,
        },
      });
    }

    try {
      const body = await request.json();

      const res = await fetch('https://api.kimi.com/coding/v1/chat/completions', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': authHeader,
        },
        body: JSON.stringify(body),
      });

      // 复制响应并添加 CORS 头
      const newRes = new Response(res.body, {
        status: res.status,
        statusText: res.statusText,
        headers: res.headers,
      });
      newRes.headers.set('Access-Control-Allow-Origin', origin);
      return newRes;
    } catch (err) {
      return new Response(JSON.stringify({ error: err.message }), {
        status: 500,
        headers: {
          'Content-Type': 'application/json',
          'Access-Control-Allow-Origin': origin,
        },
      });
    }
  },
};
