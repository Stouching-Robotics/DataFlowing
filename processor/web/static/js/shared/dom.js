/** 极小的 DOM 构造 helper —— 一律 createElement + textContent。
 *
 * 为什么不用 innerHTML 拼字符串:checkpoint 的文件名、路径、后端错误信息都
 * 来自磁盘和网络,拼进 HTML 就是一个转义面。仓库里 `escHtml()` 已经有**四份
 * 实现**,其中 dashboard.js 那份还漏了 `String()` 转换 —— 与其再加第五份,
 * 不如让新代码从根上不产生转义需求。
 *
 * 用法:
 *     el('div', { class: 'p-2', text: 'hi' }, [el('span', { text: 'x' })])
 */

const ATTR_ALIASES = { class: 'class', for: 'for', style: 'style' };

/** 必须走 setAttribute 的键(即使不含连字符)。
 *
 *  `icon` 是给 `<iconify-icon>` 这个**自定义元素**用的:它只监听 attribute
 *  变化。`node.icon = '...'` 只是给元素挂了个 JS 属性,不反映成 attribute,
 *  结果就是图标位置一片空白 —— 而且不报错,很难查。 */
const SET_ATTR = new Set(['icon']);

/**
 * @param {string} tag
 * @param {object} [attrs]  `text` 走 textContent;`on` 是 {事件名: 处理函数};
 *                          `dataset` 是 {键: 值};其余按属性 setAttribute。
 * @param {Array}  [children]  子节点(字符串会被转成文本节点)
 */
export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);

  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;

    if (key === 'text') {
      node.textContent = String(value);
    } else if (key === 'on') {
      for (const [event, handler] of Object.entries(value)) {
        node.addEventListener(event, handler);
      }
    } else if (key === 'dataset') {
      for (const [k, v] of Object.entries(value)) node.dataset[k] = String(v);
    } else if (ATTR_ALIASES[key] || SET_ATTR.has(key) || key.includes('-')) {
      node.setAttribute(key, String(value));
    } else {
      node[key] = value;
    }
  }

  append(node, children);
  return node;
}

/** 把 children(数组或单值)追加进 node。null/undefined/false 会被跳过,
 *  这样 `cond && el(...)` 这种写法可以直接塞进数组。 */
export function append(node, children) {
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(
      child instanceof Node ? child : document.createTextNode(String(child)),
    );
  }
  return node;
}

/** 清空一个容器 —— 比 `innerHTML = ''` 明确,也不会触发 HTML 解析。 */
export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}
