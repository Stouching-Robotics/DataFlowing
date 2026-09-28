/** i18n 桥接 —— 把经典脚本 i18n.js 的全局 `t()` 包一层。
 *
 * 为什么要单独一个文件:应用的词典在 `static/js/i18n.js`,那是个**经典脚本**
 * (没有 export),ES module 里 `import ... from '../i18n.js'` 拿不到任何东西。
 * 与其让每个视图自己去摸 `window.t`,不如集中在这一处,顺带做防御:
 *
 * 页面照常渲染优先于文案语言正确 —— i18n.js 万一没加载(脚本 404、加载顺序
 * 出问题),退回英文而不是抛异常、白屏。三个视图都依赖本模块,所以只写一份。
 */

/**
 * @param {string} key      词典键
 * @param {string} fallback 取不到时的英文兜底
 */
export function tr(key, fallback) {
  return typeof window.t === 'function' ? window.t(key) : fallback;
}
