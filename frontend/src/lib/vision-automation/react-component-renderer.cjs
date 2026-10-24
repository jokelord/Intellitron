'use strict';

const fs = require('node:fs');
const Module = require('node:module');
const path = require('node:path');
const Reconciler = require('react-reconciler');
const { DefaultEventPriority, LegacyRoot } = require('react-reconciler/constants');
const ts = require('typescript');

let hookInstalled = false;

function installTask6TypeScriptHook(frontendRoot) {
  if (hookInstalled) return;
  hookInstalled = true;
  const originalResolveFilename = Module._resolveFilename;
  Module._resolveFilename = function resolveTask6Alias(request, parent, isMain, options) {
    const resolvedRequest = request.startsWith('@/')
      ? path.join(frontendRoot, 'src', request.slice(2))
      : request;
    return originalResolveFilename.call(this, resolvedRequest, parent, isMain, options);
  };
  const compile = (module, filename) => {
    const source = fs.readFileSync(filename, 'utf8');
    const output = ts.transpileModule(source, {
      fileName: filename,
      compilerOptions: {
        target: ts.ScriptTarget.ES2022,
        module: ts.ModuleKind.CommonJS,
        moduleResolution: ts.ModuleResolutionKind.NodeJs,
        jsx: ts.JsxEmit.ReactJSX,
        esModuleInterop: true,
      },
    });
    module._compile(output.outputText, filename);
  };
  require.extensions['.ts'] = compile;
  require.extensions['.tsx'] = compile;
}

function detach(child) {
  if (child.parent === null) return;
  const index = child.parent.children.indexOf(child);
  if (index >= 0) child.parent.children.splice(index, 1);
  child.parent = null;
}

function append(parent, child) {
  detach(child);
  child.parent = parent;
  parent.children.push(child);
}

function insert(parent, child, before) {
  detach(child);
  const index = parent.children.indexOf(before);
  child.parent = parent;
  if (index < 0) parent.children.push(child);
  else parent.children.splice(index, 0, child);
}

function remove(parent, child) {
  const index = parent.children.indexOf(child);
  if (index >= 0) parent.children.splice(index, 1);
  child.parent = null;
}

const reconciler = Reconciler({
  supportsMutation: true,
  isPrimaryRenderer: false,
  supportsPersistence: false,
  supportsHydration: false,
  noTimeout: -1,
  now: Date.now,
  scheduleTimeout: setTimeout,
  cancelTimeout: clearTimeout,
  getCurrentEventPriority: () => DefaultEventPriority,
  getRootHostContext: () => null,
  getChildHostContext: () => null,
  getPublicInstance: (instance) => instance,
  prepareForCommit: () => null,
  resetAfterCommit: () => {},
  preparePortalMount: () => {},
  beforeActiveInstanceBlur: () => {},
  afterActiveInstanceBlur: () => {},
  detachDeletedInstance: () => {},
  shouldSetTextContent: () => false,
  createInstance(type, props) {
    return {
      kind: 'element',
      type,
      props: { ...props },
      children: [],
      parent: null,
      hidden: false,
      scrollLeft: 0,
      scrollWidth: 0,
      clientWidth: 0,
    };
  },
  createTextInstance(text) {
    return {
      kind: 'text',
      type: '#text',
      text: String(text),
      props: {},
      children: [],
      parent: null,
      hidden: false,
    };
  },
  appendInitialChild: append,
  appendChild: append,
  appendChildToContainer: append,
  insertBefore: insert,
  insertInContainerBefore: insert,
  removeChild: remove,
  removeChildFromContainer: remove,
  finalizeInitialChildren: () => false,
  prepareUpdate: () => true,
  commitUpdate(instance, _payload, _type, _oldProps, newProps) {
    instance.props = { ...newProps };
  },
  commitTextUpdate(instance, _oldText, newText) {
    instance.text = String(newText);
  },
  commitMount: () => {},
  resetTextContent(instance) {
    instance.children.forEach((child) => { child.parent = null; });
    instance.children = [];
  },
  clearContainer(container) {
    container.children.forEach((child) => { child.parent = null; });
    container.children = [];
    return false;
  },
  hideInstance(instance) { instance.hidden = true; },
  unhideInstance(instance) { instance.hidden = false; },
  hideTextInstance(instance) { instance.hidden = true; },
  unhideTextInstance(instance) { instance.hidden = false; },
});

function createRenderer(initialElement) {
  const container = { kind: 'root', children: [], parent: null };
  const root = reconciler.createContainer(
    container,
    LegacyRoot,
    null,
    false,
    null,
    '',
    (error) => { throw error; },
    null,
  );

  const flush = () => {
    reconciler.flushSync(() => {});
    let loops = 0;
    while (reconciler.flushPassiveEffects()) {
      reconciler.flushSync(() => {});
      loops += 1;
      if (loops > 100) throw new Error('React component renderer did not settle');
    }
  };

  const update = (element) => {
    reconciler.flushSync(() => {
      reconciler.updateContainer(element, root, null, null);
    });
    flush();
  };

  const visit = (node, predicate, matches) => {
    if (predicate(node)) matches.push(node);
    node.children.forEach((child) => visit(child, predicate, matches));
  };

  const findAll = (predicate) => {
    const matches = [];
    container.children.forEach((child) => visit(child, predicate, matches));
    return matches;
  };

  const text = (node) => {
    if (node.kind === 'text') return node.hidden ? '' : node.text;
    return node.hidden ? '' : node.children.map(text).join('');
  };

  const findComponent = (predicate) => {
    const visitFiber = (fiber) => {
      if (fiber === null) return null;
      if (predicate(fiber)) return fiber;
      const childMatch = visitFiber(fiber.child);
      if (childMatch !== null) return childMatch;
      return visitFiber(fiber.sibling);
    };
    const match = visitFiber(root.current.child);
    if (match === null) throw new Error('No rendered component fiber matched the predicate');
    return match;
  };

  const act = (callback) => {
    reconciler.flushSync(callback);
    flush();
  };

  update(initialElement);
  return {
    container,
    flush,
    update,
    text,
    findAll,
    findComponent,
    act,
    findOne(predicate) {
      const matches = findAll(predicate);
      if (matches.length === 0) throw new Error('No rendered host node matched the predicate');
      return matches[0];
    },
    click(node) {
      if (typeof node.props.onClick !== 'function') throw new Error('Rendered node is not clickable');
      reconciler.flushSync(() => {
        node.props.onClick({
          currentTarget: node,
          stopPropagation() {},
        });
      });
      flush();
    },
    unmount() {
      update(null);
    },
  };
}

module.exports = {
  createRenderer,
  installTask6TypeScriptHook,
};
