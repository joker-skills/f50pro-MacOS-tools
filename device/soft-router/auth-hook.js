// Temporary, process-local hook. No APK, Settings, network, or boot changes.
import Java from 'frida-java-bridge';

let method;
let feature;
let reflected;
let active = false;
let calls = 0;

function report(event, extra = {}) {
  send({ event, pid: Process.id, active, calls, ...extra });
}

function restore() {
  if (active) method.implementation = null;
  active = false;
  report('restored');
}

function probe() {
  if (!active) throw new Error('Hook is not active');
  const before = calls;
  // Reflection exercises ART dispatch, not Frida's original-method wrapper.
  const value = reflected.invoke(null, Java.array('java.lang.Object', [null]));
  const result = Java.cast(value, Java.use('java.lang.Boolean')).booleanValue();
  if (result !== false || calls !== before + 1) {
    throw new Error('Replacement dispatch verification failed');
  }
  report('probe-passed', { returned: false });
}

function apply() {
  if (active) throw new Error('Hook already active');
  method.implementation = function (_context) { calls++; return false; };
  active = true;
  try { probe(); } catch (error) { restore(); throw error; }
  report('installed');
}

rpc.exports = {
  init() {
    return new Promise((resolve, reject) => {
      if (!Java.available || Process.arch !== 'arm64') {
        reject(new Error('Expected Android ARM64 Java process'));
        return;
      }
      Java.perform(() => {
        try {
          if (String(Java.use('android.app.ActivityThread').currentProcessName()) !== 'com.zte.web') {
            throw new Error('Unexpected target process');
          }
          feature = Java.use('com.zte.web.device.frameworks.feature.FeatureConfig');
          method = feature.isSecondAuthOpen.overload('android.content.Context');
          if (method.returnType.className !== 'boolean') throw new Error('Unexpected signature');
          const context = Java.use('android.content.Context');
          reflected = feature.class.getDeclaredMethod('isSecondAuthOpen',
            Java.array('java.lang.Class', [context.class]));
          reflected.setAccessible(true);
          apply();
          resolve();
        } catch (error) {
          restore();
          report('failed', { reason: String(error) });
          reject(error);
        }
      });
    });
  },
  onFridaStdin(line) {
    Java.perform(() => {
      try {
        switch (line.trim()) {
          case 'status': report('status'); break;
          case 'probe': probe(); break;
          case 'restore': restore(); break;
          case 'apply': apply(); break;
          default: report('unknown-command');
        }
      } catch (error) { report('failed', { reason: String(error) }); }
    });
  }
};
