/** 展示层格式化工具 —— 纯函数,不碰 DOM、不发请求。
 *
 * 单独成文件的原因:checkpoints 与 params 两个视图都要用,而它们之间不该
 * 互相依赖。放在这里,两边都只依赖这一个叶子模块。
 */

/**
 * 字节数 → 人类可读。1024 进制,和后端报的 total_bytes 口径一致。
 * 0 与非法值都返回 '0 B'(不返回 NaN —— 它会在表格里显示成 "NaN MB")。
 */
export function bytes(n) {
  const v = Number(n);
  if (!Number.isFinite(v) || v <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let x = v;
  while (x >= 1024 && i < units.length - 1) {
    x /= 1024;
    i += 1;
  }
  // 字节和 KB 不留小数(4.0 KB 不如 4 KB 好读),MB 以上留一位。
  return `${i <= 1 ? Math.round(x) : x.toFixed(1)} ${units[i]}`;
}

/**
 * Unix 秒 → `YYYY-MM-DD HH:MM`。
 *
 * 刻意不用 toLocaleString:它受浏览器语言影响,中英切换时同一个时间会变成
 * 两种格式,表格列宽跟着跳。这里固定格式,与时区无关的部分保持稳定。
 */
export function dateTime(seconds) {
  const v = Number(seconds);
  if (!Number.isFinite(v) || v <= 0) return '—';
  const d = new Date(v * 1000);
  if (Number.isNaN(d.getTime())) return '—';
  const p = (x) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 大整数 → 千分位。20000 显示成 20,000,免得数零。 */
export function thousands(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return '—';
  return v.toLocaleString('en-US');
}
