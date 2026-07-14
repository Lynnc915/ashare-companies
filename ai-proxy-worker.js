/**
 * Cloudflare Worker：A股网站 AI 助手代理
 *
 * 作用：
 * 1. 隐藏 Claude API Key（存在 Worker 环境变量里）
 * 2. 处理跨域（CORS），让 GitHub Pages 能调用
 * 3. 转发请求到 Anthropic Messages API
 *
 * 部署步骤：
 * 1. 登录 https://dash.cloudflare.com/，进入 Workers & Pages
 * 2. 创建 Service（或 Pages Function），把本文件内容粘贴进去
 * 3. 在 Settings > Variables 添加环境变量：
 *    CLAUDE_API_KEY = sk-ant-api03-你的key
 * 4. 保存并部署，记下 Worker URL（如 https://ashare-ai.xxx.workers.dev）
 * 5. 把 Worker URL 填到 index.html 的 AI 设置里
 */

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
          'Access-Control-Allow-Headers': 'Content-Type',
          'Access-Control-Max-Age': '86400',
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

    if (!env.CLAUDE_API_KEY) {
      return new Response(JSON.stringify({ error: 'CLAUDE_API_KEY not configured' }), {
        status: 500,
        headers: {
          'Content-Type': 'application/json',
          'Access-Control-Allow-Origin': origin,
        },
      });
    }

    try {
      const body = await request.json();

      const res = await fetch('https://api.anthropic.com/v1/messages', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'x-api-key': env.CLAUDE_API_KEY,
          'anthropic-version': '2023-06-01',
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
