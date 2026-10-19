import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import { test } from 'node:test';

const source = readFileSync(new URL('../device/soft-router/auth-hook.js', import.meta.url), 'utf8')
  .replace("import Java from 'frida-java-bridge';", '');

function fixture(processName = 'com.zte.web', returnType = 'boolean', options = {}) {
  const messages = [];
  const contextClass = {};
  let implementation = null;
  const method = { returnType: { className: returnType } };
  Object.defineProperty(method, 'implementation', {
    get: () => implementation,
    set(value) {
      if (value === null && options.restoreError) throw new Error('mock restoration blocked');
      implementation = value;
    }
  });
  const feature = {
    isSecondAuthOpen: { overload: (type) => { assert.equal(type, 'android.content.Context'); return method; } },
    class: { getDeclaredMethod: (name, parameters) => {
      assert.equal(name, 'isSecondAuthOpen');
      assert.equal(parameters.length, 1);
      assert.equal(parameters[0], contextClass);
      return {
      setAccessible() {},
      invoke: (_receiver, args) => ({ booleanValue: () => {
        if (options.probe === 'true') return true;
        if (options.probe === 'no-hit') return false;
        return method.implementation(...args);
      } })
    }; } }
  };
  const types = {
    'com.zte.web.device.frameworks.feature.FeatureConfig': feature,
    'android.app.ActivityThread': { currentProcessName: () => processName },
    'android.content.Context': { class: contextClass },
    'java.lang.Boolean': {}
  };
  const rpc = {};
  vm.runInNewContext(source, {
    rpc, Process: { id: 123, arch: 'arm64' }, send: (m) => messages.push(m),
    Java: { available: true, perform: (f) => f(), use: (n) => {
      assert.ok(n in types, 'unexpected Java class'); return types[n];
    }, array: (_type, values) => values, cast: (v) => v }
  });
  return { api: rpc.exports, method, messages };
}

test('only the exact method is replaced, probed and reversibly restored', async () => {
  const { api, method, messages } = fixture();
  await api.init();
  assert.equal(method.implementation({ operator: 'YD', open: '1' }), false);
  assert.equal(method.implementation({ operator: 'DX', open: '1' }), false);
  api.onFridaStdin('status\n');
  assert.equal(messages.at(-1).active, true);
  api.onFridaStdin('restore\n');
  assert.equal(method.implementation, null);
  assert.equal(messages.at(-1).active, false);
  api.onFridaStdin('apply\n');
  assert.equal(messages.at(-1).event, 'installed');
  api.onFridaStdin('probe\n');
  assert.equal(messages.at(-1).event, 'probe-passed');
  api.onFridaStdin('restore\n');
  assert.equal(method.implementation, null);
});

test('unexpected process and method signature fail without installing', async () => {
  for (const f of [fixture('other.process'), fixture('com.zte.web', 'int')]) {
    await assert.rejects(f.api.init());
    assert.equal(f.method.implementation, null);
    assert.equal(f.messages.at(-1).event, 'failed');
  }
});

test('a failed probe restores an already-installed replacement and rejects init', async () => {
  for (const probe of ['true', 'no-hit']) {
    const { api, method, messages } = fixture('com.zte.web', 'boolean', { probe });
    await assert.rejects(api.init());
    assert.equal(method.implementation, null);
    assert.ok(messages.some((m) => m.event === 'restored'));
    assert.equal(messages.at(-1).event, 'failed');
    assert.equal(messages.at(-1).active, false);
  }
});

test('restoration errors reject init without falsely reporting inactive', async () => {
  const options = { probe: 'true', restoreError: true };
  const { api, method, messages } = fixture('com.zte.web', 'boolean', options);
  await assert.rejects(api.init(), /Restoration failed/);
  assert.notEqual(method.implementation, null);
  assert.equal(messages.at(-1).event, 'failed');
  assert.equal(messages.at(-1).active, true);
  assert.ok(!messages.some((m) => m.event === 'restored'));
  api.onFridaStdin('restore\n');
  assert.equal(messages.at(-1).event, 'failed');
  assert.equal(messages.at(-1).active, true);
  options.restoreError = false;
  api.onFridaStdin('restore\n');
  assert.equal(method.implementation, null);
  assert.equal(messages.at(-1).active, false);
});
