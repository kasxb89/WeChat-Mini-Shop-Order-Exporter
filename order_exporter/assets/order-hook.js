// Runtime-only connection for one selected miniapp. No executable files are changed.
const config = @@CONFIG@@;
const endpoint = @@LOCAL_URL@@;
if (Process.arch !== 'x64') throw new Error('当前小程序运行时不是已适配的 Windows x64 架构');
const appid = 'wxca8d4b8e8feedc2a';
const module = Process.getModuleByName('flue.dll');
const layout = config.MiniAppConfigStructOffsets;
const saved = [];
function follow(address, offsets) {
  for (const offset of offsets) address = address.add(offset).readPointer();
  return address;
}
const hook = Interceptor.attach(module.base.add(config.LoadStartHookOffset), {
  onEnter(args) {
    let selected = false;
    try {
      const launch = follow(args[0], layout.LaunchConfigOffsets);
      const bytes = new Uint8Array(launch.add(176).readByteArray(18));
      if (!Array.from(appid).every((character, index) => bytes[index] === character.charCodeAt(0))) return;
      selected = true;
      const remote = follow(launch, layout.RemoteDebugConfigOffsets);
      const scene = remote.add(layout.SceneOffset);
      const mode = remote.add(layout.RemoteDebugModeOffset);
      const url = launch.add(layout.WebSocketURLStringOffset);
      if (url.add(23).readS8() < 0) throw new Error('小店已经连接其他调试工具，请先关闭该连接。');
      saved.push({launch, scene, mode, url,
        sceneValue: scene.readInt(), modeValue: mode.readInt(), urlValue: url.readByteArray(24)});
      args[1] = args[1].and(ptr('0xffffffffffffff00')).or(1);
      scene.writeInt(1101);
      mode.writeInt(1);
      url.writeByteArray(new Uint8Array(24));
      url.writeUtf8String(endpoint);
      url.add(23).writeU8(endpoint.length);
    } catch (error) { if (selected) send({type: 'connection_error', message: String(error)}); }
  }
});
const filter = module.base.add(config.CDPFilterHookOffset);
const convert = new NativeFunction(module.base.add(config.CastToJsonHookOffset), 'pointer', ['pointer', 'pointer']);
const callback = new NativeCallback(function(self, output, input) {
  convert(output, input);
  return output;
}, 'pointer', ['pointer', 'pointer', 'pointer']);
Interceptor.replace(filter, callback);
rpc.exports = {stop() {
  hook.detach();
  Interceptor.revert(filter);
  let restored = 0;
  for (const item of saved.reverse()) {
    try {
      if (item.launch.add(176).readUtf8String(18) !== appid) continue;
      if (item.scene.readInt() === 1101) item.scene.writeInt(item.sceneValue);
      if (item.mode.readInt() === 1) item.mode.writeInt(item.modeValue);
      if (item.url.readUtf8String() === endpoint) item.url.writeByteArray(item.urlValue);
      restored++;
    } catch (_) { /* The miniapp may already have disposed its launch object. */ }
  }
  return {restored};
}};
