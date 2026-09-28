/** 训练页的后端调用 —— 全页只有这里出现 `fetch`。
 *
 * **这里刻意没有**提交训练 / 停止 / 删除 / 下载。本期不接后端执行,与其放一个
 * 点了会 404 的函数,不如等端点真正存在时再加进来 —— 视图层也就不会误以为
 * 它们可用。将来接执行时,新端点加在本文件顶部即可,形状保持一致。
 *
 * 对应后端:`app/routes/training.py`(只读,不执行任何训练)。
 */

const BASE = '/api/v1/training';

async function getJSON(url) {
  const res = await fetch(url, { headers: { Accept: 'application/json' } });
  if (!res.ok) {
    // 后端的错误体是 {detail: ...}。取不到就退回状态码 —— 不要吞成空消息,
    // 否则页面上只剩一个没有原因的空白。
    let detail = '';
    try {
      detail = (await res.json()).detail || '';
    } catch {
      /* 非 JSON 响应(网关/代理错误页),用状态码兜底 */
    }
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return res.json();
}

/**
 * 列出训练产物。
 *
 * @returns {Promise<{runs: Array, root: string, exists: boolean}>}
 *   `exists: false` 表示训练目录不存在(新机器、或本机从未跑过训练)。
 *   这是**正常情况**,由调用方渲染空态,不要当错误抛。
 */
export function listRuns() {
  return getJSON(`${BASE}/runs`);
}

/**
 * 单个 checkpoint 文件的下载地址 —— 给 `<a download>` 用的**字符串**,不是请求。
 *
 * 路由是 ``/runs/{run}/checkpoints/{step}/files/{name:path}``,``{name:path}`` 要收
 * 子目录路径(``pretrained_model/model.safetensors``),所以**整体保留斜杠、只逐段
 * 编码**;整串 encodeURIComponent 会把斜杠也编掉,后端就找不到文件。
 */
export function checkpointFileUrl(runName, step, name) {
  const seg = encodeURIComponent;
  const path = String(name).split('/').map(seg).join('/');
  return `${BASE}/runs/${seg(runName)}/checkpoints/${seg(step)}/files/${path}`;
}

/**
 * 本机 GPU 列表(读 nvidia-smi)。
 *
 * @returns {Promise<{available: boolean, error: string, gpus: Array}>}
 *   `available: false` 表示**问不到显卡**(驱动没起来 / 没装 nvidia-smi),
 *   与"显卡闲着"完全是两回事 —— 调用方必须区别显示,不能当 0 用。
 */
export function listGpus() {
  return getJSON(`${BASE}/gpus`);
}

/**
 * 可训练的数据集 = `sessions/<项目>` 下的多集 LeRobot 树。
 *
 * 不是 `state/exports/*`:那里每条都是单集导出(total_episodes=1),凑不成训练集。
 */
export function listDatasets() {
  return getJSON(`${BASE}/datasets`);
}

/**
 * 各项目离「可以导出」还有多远 —— 训练页的「到点提醒」用。
 *
 * 判据是**已通过集数达到项目设置的采集上限**(`params.target_episodes`),
 * 到点只提醒、不自动导出 —— 理由见 `app/training/projects.py` 的 docstring。
 *
 * @returns {Promise<{projects: Array, ready_count: number}>}
 */
export function listReadyProjects() {
  return getJSON(`${BASE}/ready`);
}

// 注:这里曾经有一个 listEpisodes()(走 /api/v1/ingestion/episodes 逐个勾选
// episode)。改成"按数据集(项目)选"之后就没人用了,已删除 —— 等真要做
// "从已审核的集组装训练集"那一步时再加回来,那时它多半还要带上筛选参数。
