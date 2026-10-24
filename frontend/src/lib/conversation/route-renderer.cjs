'use strict';
const Reconciler = require('react-reconciler');
const {DefaultEventPriority, LegacyRoot} = require('react-reconciler/constants');

// Browser host surface for actual route effects. React and the application hooks
// remain real; this implements only DOM methods needed by layout/scroll/input.
class HostElement extends EventTarget {
  constructor(type, props = {}) {
    super();
    this.kind = 'element'; this.type = type; this.props = props;
    this.children = []; this.parent = null; this.hidden = false;
    this.style = {setProperty(name, value) {this[name] = value;}, removeProperty(name) {delete this[name];}};
    this.scrollTop = 0; this.scrollLeft = 0; this.scrollHeight = 120;
    this.scrollWidth = 100; this.clientWidth = 100; this.clientHeight = 120;
    this.value = props.value ?? ''; this.selectionStart = this.value.length; this.selectionEnd = this.value.length;
  }
  scrollTo(options) {this.scrollTop = options.top;}
  focus() {document.activeElement = this;}
  setSelectionRange(start, end) {this.selectionStart = start; this.selectionEnd = end;}
  getBoundingClientRect() {return {top: 0, left: 0, width: 100, height: 120, right: 100, bottom: 120};}
  getAttribute(name) {return this.props[name] ?? null;}
  setAttribute(name, value) {this.props[name] = value;}
  removeAttribute(name) {delete this.props[name];}
  contains(target) {return this === target || this.children.some(child => child.contains?.(target));}
  querySelector() {return null;}
  querySelectorAll() {return [];}
}
function detach(child) {if(child.parent) {const offset = child.parent.children.indexOf(child); if(offset >= 0) child.parent.children.splice(offset,1);} child.parent = null;}
function append(parent, child) {detach(child); child.parent = parent; parent.children.push(child);}
function insert(parent, child, before) {detach(child); child.parent = parent; const offset = parent.children.indexOf(before); parent.children.splice(offset < 0 ? parent.children.length : offset,0,child);}
function remove(_parent, child) {detach(child);}
const reconciler = Reconciler({
  supportsMutation: true, supportsPersistence: false, supportsHydration: false,
  isPrimaryRenderer: false, noTimeout: -1, now: Date.now,
  scheduleTimeout: setTimeout, cancelTimeout: clearTimeout,
  getCurrentEventPriority: () => DefaultEventPriority,
  getRootHostContext: () => null, getChildHostContext: () => null,
  getPublicInstance: node => node, prepareForCommit: () => null,
  resetAfterCommit() {}, preparePortalMount() {}, beforeActiveInstanceBlur() {}, afterActiveInstanceBlur() {}, detachDeletedInstance() {},
  shouldSetTextContent: () => false,
  createInstance: (type, props) => new HostElement(type, props),
  createTextInstance: text => ({kind: 'text', type: '#text', text: String(text), props: {}, children: [], parent: null, hidden: false}),
  appendInitialChild: append, appendChild: append, appendChildToContainer: append,
  insertBefore: insert, insertInContainerBefore: insert, removeChild: remove, removeChildFromContainer: remove,
  finalizeInitialChildren: () => false, prepareUpdate: () => true,
  commitUpdate(node, _payload, _type, _old, props) {node.props = props; node.value = props.value ?? node.value;},
  commitTextUpdate(node, _old, text) {node.text = String(text);}, commitMount() {},
  resetTextContent(node) {node.children.forEach(child => {child.parent = null;}); node.children = [];},
  clearContainer(node) {node.children.forEach(child => {child.parent = null;}); node.children = []; return false;},
  hideInstance(node) {node.hidden = true;}, unhideInstance(node) {node.hidden = false;},
  hideTextInstance(node) {node.hidden = true;}, unhideTextInstance(node) {node.hidden = false;},
});
function createRouteRenderer(element) {
  const container = {kind: 'root', children: []};
  const root = reconciler.createContainer(container, LegacyRoot, null, false, null, '', error => {throw error;}, null);
  const flush = () => {reconciler.flushSync(() => {}); let loops = 0; while(reconciler.flushPassiveEffects()) {reconciler.flushSync(() => {}); if(++loops > 100) throw new Error('Route effects did not settle');}};
  const update = element => {reconciler.flushSync(() => reconciler.updateContainer(element,root,null,null)); flush();};
  const findAll = predicate => {const matches = []; function visit(node) {if(predicate(node)) matches.push(node); node.children.forEach(visit);} container.children.forEach(visit); return matches;};
  const text = node => node.hidden ? '' : node.kind === 'text' ? node.text : node.children.map(text).join('');
  const act = callback => {reconciler.flushSync(callback); flush();};
  update(element);
  return {container, flush, update, findAll, text, act,
    findOne(predicate) {const node = findAll(predicate)[0]; if(!node) throw new Error('No route host node matched'); return node;},
    click(node) {if(node.props.disabled) throw new Error('Cannot click disabled route control'); if(typeof node.props.onClick !== 'function') throw new Error('Route control is not clickable'); act(() => node.props.onClick({target: node,currentTarget: node,stopPropagation() {},preventDefault() {}}));},
    unmount() {update(null);},
  };
}
module.exports = {HostElement,createRouteRenderer};
