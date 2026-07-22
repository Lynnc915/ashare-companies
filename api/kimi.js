/**
 * Vercel Edge Function：Kimi Code API 跨域代理
 *
 * 部署步骤：
 * 1. 把整个 ashare-companies 项目导入 Vercel（https://vercel.com/new）
 * 2. Vercel 会自动识别 api/kimi.js 这个 Edge Function
 * 3. 部署后得到域名，如 https://ashare-companies.vercel.app/api/kimi
 * 4. 把这个 URL 填到网站的「Kimi Code 代理服务地址」框里
 *
 * 说明：
 * - 本代理只转发请求，不保存访客的 Kimi Code API Key
 * - 费用由使用自己 Key 的访客承担
 */

export const config = {
  runtime: 'edge',
};

const ALLOWED_ORIGINS = [
  'https://lynnc915.github.io',
  'http://localhost:8080',
  'http://127.0.0.1:8080',
];

export default async function handler(request) {
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
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
      },
      body: JSON.stringify(body),
    });

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
}
