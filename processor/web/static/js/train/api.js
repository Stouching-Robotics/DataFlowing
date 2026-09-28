/** 训练页的后端调用 —— 全页只有这里出现 `fetch`。
 *
 * 对应后端:`app/routes/training.py`。只读的发现类接口(runs/gpus/datasets/ready)
 * 与训练执行类接口(jobs/*)都在这里。
 */

const BASE = '/api/v1/training';

async function readJSON(res) {
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

async function getJSON(url) {
  return readJSON(await fetch(url, { headers: { Accept: 'application/json' } }));
}

async function postJSON(url, body) {
  return readJSON(await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body ?? {}),
  }));
}

// ── 训练执行 ────────────────────────────────────────────────────────

/**
 * 提交一个训练任务。**成功即已起进程**,返回 202。
 *
 * 校验不过会抛 Error,消息就是后端那句中文理由(显卡读不到 / 数据集不在导出
 * 产物里 / 目录已存在 / 已有任务在跑)—— 直接显示给用户即可,不要自己再编
 * 一句"提交失败"把它盖掉。
 *
 * @param {object} spec `{job_name, dataset, gpus, params, resume}`
 */
export function createTrainingJob(spec) {
  return postJSON(`${BASE}/jobs`, spec);
}

/**
 * 训练任务列表(新的在前)。
 *
 * 与 `listRuns()` 是两回事:那个列的是**磁盘上的产物**,这里列的是**任务**。
 * 失败的任务、还没写出产物的任务只在这里出现。
 *
 * @returns {Promise<{jobs: Array}>}
 */
export function listTrainingJobs() {
  return getJSON(`${BASE}/jobs`);
}

/** 单个任务的当前状态(进度、pid、错误)。前端轮询这个。 */
export function getTrainingJob(jobId) {
  return getJSON(`${BASE}/jobs/${encodeURIComponent(jobId)}`);
}

/**
 * 增量读训练日志。
 *
 * LeRobot 自己不写日志,这份是后端接管子进程输出存下来的,**失败原因全在这里**。
 * 传上次返回的 `offset` 回来就只拿新增部分。
 *
 * @returns {Promise<{text: string, offset: number, size: number, truncated: boolean}>}
 */
export function trainingJobLog(jobId, offset = 0) {
  return getJSON(`${BASE}/jobs/${encodeURIComponent(jobId)}/log?offset=${offset}`);
}

/** 取消训练 —— 后端会杀整个进程组,显存在几秒内释放。 */
export function cancelTrainingJob(jobId) {
  return postJSON(`${BASE}/jobs/${encodeURIComponent(jobId)}/cancel`);
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
 * 可训练的数据集 = **视频审核页导出后落在 `state/datasets/` 的成品**。
 *
 * 刻意**不列 `sessions/<项目>` 原始项目**:那里混着未审核的集,而且留出集
 * (UMIGripper_Action_AI 的 52–73)就在里面 —— 按项目选等于同时踩两个坑。
 * 导出那一步才是唯一能明确控制"到底训了哪些集"的地方,所以以导出产物为准。
 * 详见 `app/training/datasets.py` 的 docstring。
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
