"""Read-only personal order API through the selected local miniapp session."""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
from pathlib import Path
import secrets
import threading
import time

from .native import PageError, Paused
from .order_protocol import command, response

APP_ID = 'wxca8d4b8e8feedc2a'
RUNTIME_SHA256 = '57717d08d0d80e52926a3d3dbbffe9ae46fbe40f19fc82befc4e14b06ff80ebe'
RUNTIME_CONFIG = {
    'LoadStartHookOffset': '0x2dd2b30', 'CDPFilterHookOffset': '0x39a2a40',
    'CastToJsonHookOffset': '0xbb224f0',
    'MiniAppConfigStructOffsets': {'LaunchConfigOffsets': [64, 1616, 8],
        'RemoteDebugConfigOffsets': [1544, 16], 'SceneOffset': 480,
        'WebSocketURLStringOffset': 648, 'RemoteDebugModeOffset': 748},
}
ORDER_MODULE = 'subpackages/orderList/_shared/cgi/finderEcDataPipe/orderList/index.js'
CONTEXT_EXPRESSION = """(function(){try {
  if(typeof wx==='object' && typeof wx.getAccountInfoSync==='function' && typeof require==='function')
    return {appid:wx.getAccountInfoSync().miniProgram.appId,ready:typeof wx.operateWXData==='function'};
}catch(_){}return null;})()"""

# Only requested order and shipping fields cross the local transport.
# Payment data, cookies and request context are not exported or logged.
PROJECT_JS = """
function check(result) {
  const code=result.baseResp && result.baseResp.errcode;
  if(result.errorCode || result.errno || code)throw new Error('订单读取失败，错误码 '+(code||result.errorCode||result.errno));
  return result;
}
function identifier(value) {
  if(typeof value==='number' && !Number.isSafeInteger(value))throw new Error('平台返回的订单号精度不足');
  return String(value||'').replace(/^UID-/,'');
}
function products(order) {
  return (order.skuList||[]).map(item=>item.spuSnapshot||{}).filter(item=>item.spuName).map(item=>
    item.spuName+(item.skuName?'（'+item.skuName+'）':''));
}
function summary(order) {
  return {order_id:identifier(order.orderId),products:products(order),
    ordered_at:order.orderCreateTime,shop:(order.shopInfo||{}).shopName||'',
    status:order.orderStateStr||''};
}
function shippingAddress(address) {
  const parts=[address.province_name||address.province,address.city_name||address.city,
    address.county_name||address.county,address.detail_info||address.detailInfo];
  let result='';
  for(const part of parts) {
    const text=String(part||'').trim();
    if(!text)continue;
    if(!result || text.startsWith(result))result=text;
    else if(!result.endsWith(text))result+=text;
  }
  return result;
}
function detailRecord(response) {
  check(response);
  const order=response.orderDetail;
  if(!order)throw new Error('平台未返回订单详情');
  const finder=JSON.parse(order.finderShopOrderStr||'{}');
  const info=finder.order_info||{}, delivery=info.delivery_info||{};
  const numbers=[];
  function waybills(value) {
    if(Array.isArray(value)){for(const item of value)waybills(item);return;}
    if(!value || typeof value!=='object')return;
    const number=value.waybill_id||value.waybillId;
    if(typeof number==='number' && !Number.isSafeInteger(number))throw new Error('平台返回的快递单号精度不足');
    if(number)numbers.push(String(number));
    for(const [key,item] of Object.entries(value))
      if(!['address_info','history_address_list','approving_address','virtual_tel_number_info'].includes(key))waybills(item);
  }
  waybills(delivery);
  waybills(order.deliveryInfo);
  for(const sku of order.skuList||[])waybills(sku.deliveryInfo);
  for(const product of info.order_product_info||[])waybills(product.delivery_info);
  const result=summary(order);
  result.order_id=identifier((finder.order_key||{}).order_id||result.order_id);
  result.ordered_at=finder.create_time||result.ordered_at;
  result.status=order.orderStateStrSc||result.status;
  const address=delivery.address_info||{}, fallback=order.customerAddressInfo||{};
  result.recipient=address.user_name||fallback.contactName||'';
  result.address=shippingAddress(address)||shippingAddress(fallback);
  result.contact_phone=String(address.tel_number||fallback.telNumber||'');
  result.tracking_numbers=Array.from(new Set(numbers));
  result.shipping_known=!!(((order.deliveryInfo||{}).expressInfo||{}).stateNew ||
    (delivery.ewaybill_info_list||[]).length || (delivery.delivery_product_info||[]).length);
  return result;
}
"""


def browser_pid(window) -> int:
    import psutil
    process = psutil.Process(window.pid)
    while process.name().lower() == 'wechatappex.exe':
        if not any(arg.startswith('--type=') for arg in process.cmdline()):
            return process.pid
        process = process.parent()
    raise PageError('尚未找到所选微信的小程序运行时，请重新打开微信搜索页。')


def source_window(windows, target):
    """Choose the same account's search or main window when reconnecting a miniapp."""
    import psutil
    parent = psutil.Process(browser_pid(target))
    search = [w for w in windows if w.pid == parent.pid and w.title == '微信']
    if len(search) == 1:
        return search[0]
    wechat = parent.parent()
    main = [w for w in windows if w.pid == wechat.pid and w.path.lower().endswith(('\\weixin.exe', '\\wechat.exe'))]
    if len(main) == 1:
        return main[0]
    raise PageError('请将这个账号的微信主窗口打开，再开始采集。')


def wechat_pid(window):
    import psutil
    process = psutil.Process(window.pid)
    if process.name().lower() == 'wechatappex.exe':
        process = psutil.Process(browser_pid(window)).parent()
    if process.name().lower() not in {'weixin.exe', 'wechat.exe'}:
        raise PageError('所选窗口不属于已登录的微信账号。')
    return process.pid


class OrderAPI:
    def __init__(self, stop, check_stop=None):
        self.stop = stop
        self.check_stop = check_stop
        self.started = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.thread = None
        self.loop = None
        self.shutdown = None
        self.connection = None
        self.execution = None
        self.sequence = self.requests = 0
        self.request_seconds = 0.0
        self.pending = {}
        self.connections = set()
        self.cleanup = None

    def check(self):
        if self.check_stop:
            self.check_stop()
        if self.stop.is_set():
            raise Paused('已暂停，采集进度已保存。')
        if self.error:
            raise PageError(self.error)

    def wait(self, event, timeout, message):
        until = time.monotonic() + timeout
        while not event.wait(.05):
            self.check()
            if time.monotonic() > until:
                raise PageError(message)
        self.check()

    def start(self, pid):
        import psutil
        process = psutil.Process(pid)
        runtime = Path(process.exe()).parent
        digest = hashlib.sha256((runtime / 'flue.dll').read_bytes()).hexdigest()
        if digest != RUNTIME_SHA256:
            raise PageError('当前微信小程序运行时尚未适配数据直读。请保留现有记录，更新本工具后继续。')
        self.thread = threading.Thread(target=lambda: asyncio.run(self.serve(pid)), daemon=True)
        self.thread.start()
        self.wait(self.started, 15, '建立本地订单连接超时，请关闭其他小程序调试工具后继续。')

    async def request(self, ws, method, params, timeout=20):
        self.sequence += 1
        sequence = self.sequence
        future = self.loop.create_future()
        self.pending[sequence] = future
        try:
            await ws.send(command(sequence, method, params))
            message = await asyncio.wait_for(future, timeout)
            if message.get('error'):
                raise PageError(message['error'].get('message', '订单连接已中断。'))
            result = message.get('result', {})
            if result.get('exceptionDetails'):
                error = result['exceptionDetails'].get('exception', {})
                raise PageError(error.get('description', '读取订单失败。').split('\n')[0])
            return result.get('result', {}).get('value')
        finally:
            self.pending.pop(sequence, None)

    async def discover(self, ws, contexts):
        await self.request(ws, 'Runtime.enable', {})
        while not self.ready.is_set():
            for execution in list(contexts):
                params = {'expression': CONTEXT_EXPRESSION, 'returnByValue': True}
                if execution is not None:
                    params['contextId'] = execution
                try:
                    value = await self.request(ws, 'Runtime.evaluate', params, timeout=3)
                except (PageError, asyncio.TimeoutError):
                    continue
                if isinstance(value, dict) and value.get('appid') == APP_ID and value.get('ready'):
                    self.connection, self.execution = ws, execution
                    self.ready.set()
                    return
            await asyncio.sleep(.25)

    async def client(self, ws):
        if ws.request.path != self.url_path:
            await ws.close()
            return
        self.connections.add(ws)
        contexts = {None}
        discovery = asyncio.create_task(self.discover(ws, contexts))
        try:
            async for raw in ws:
                if not isinstance(raw, bytes):
                    continue
                message = response(raw)
                if not message:
                    continue
                future = self.pending.get(message.get('id'))
                if future is not None and not future.done():
                    future.set_result(message)
                if message.get('method') == 'Runtime.executionContextCreated':
                    contexts.add(message['params']['context']['id'])
                elif message.get('method') == 'Runtime.executionContextDestroyed':
                    contexts.discard(message['params']['executionContextId'])
        finally:
            discovery.cancel()
            await asyncio.gather(discovery, return_exceptions=True)
            self.connections.discard(ws)
            if ws is self.connection and not self.shutdown.is_set():
                self.error = '微信小店数据连接已断开，已有记录已保存，请继续上次任务。'

    async def serve(self, pid):
        import frida
        from websockets.asyncio.server import serve
        self.loop = asyncio.get_running_loop()
        self.shutdown = asyncio.Event()
        session = script = None
        try:
            # Four digits keep the URL within this runtime's short-string capacity.
            server = None
            for _ in range(8):
                port = 2000 + secrets.randbelow(8000)
                try:
                    server = await serve(self.client, '127.0.0.1', port, max_size=8 * 1024 * 1024)
                    break
                except OSError:
                    continue
            if server is None:
                raise OSError('没有可用的本机订单连接端口。')
            async with server:
                self.url_path = '/' + secrets.token_urlsafe(1)
                endpoint = f'ws://127.0.0.1:{port}{self.url_path}'
                assert len(endpoint) <= 22
                cancellation = frida.Cancellable()
                timer = threading.Timer(12, cancellation.cancel)
                timer.daemon = True
                timer.start()
                try:
                    with cancellation:
                        session = frida.get_local_device().attach(pid)
                finally:
                    timer.cancel()
                source = (Path(__file__).parent / 'assets' / 'order-hook.js').read_text(encoding='utf-8')
                source = source.replace('@@CONFIG@@', json.dumps(RUNTIME_CONFIG)).replace('@@LOCAL_URL@@', json.dumps(endpoint))
                script = session.create_script(source)
                def receive(message, data):
                    payload = message.get('payload', {})
                    if message['type'] == 'error' or payload.get('type') == 'connection_error':
                        self.error = payload.get('message') or message.get('description', '订单运行时连接失败。')
                script.on('message', receive)
                script.load()
                self.started.set()
                await self.shutdown.wait()
                for ws in list(self.connections):
                    await ws.close()
        except Exception as error:
            self.error = f'本地订单连接失败：{error}'
            self.started.set()
        finally:
            if script:
                self.cleanup = script.exports_sync.stop()
                script.unload()
            if session:
                session.detach()

    def evaluate(self, body):
        self.wait(self.ready, 20, '小店订单数据尚未准备好，请保持微信登录后继续。')
        expression = "(async function(){if(wx.getAccountInfoSync().miniProgram.appId!==" + json.dumps(APP_ID) + ")throw new Error('微信小店会话已变更');" + PROJECT_JS + 'const api=require(' + json.dumps(ORDER_MODULE) + ');' + body + '})()'
        params = {'expression': expression, 'returnByValue': True, 'awaitPromise': True, 'timeout': 20000}
        if self.execution is not None:
            params['contextId'] = self.execution
        started = time.monotonic()
        future = asyncio.run_coroutine_threadsafe(self.request(self.connection, 'Runtime.evaluate', params), self.loop)
        try:
            while True:
                self.check()
                try:
                    result = future.result(timeout=.05)
                    break
                except concurrent.futures.TimeoutError:
                    continue
        except BaseException:
            future.cancel()
            raise
        self.request_seconds += time.monotonic() - started
        return result

    def list(self, cursor='0', page_size=50):
        self.requests += 1
        args = json.dumps({'tabType': 0, 'pageSize': page_size, 'lastIndex': cursor, 'needEvaluationStatus': False})
        return self.evaluate('const result=check(await api.getOrderList(' + args + '));return {orders:(result.orderList||[]).map(summary),has_more:!!result.hasMore,cursor:result.lastIndex};')

    def details(self, orders):
        self.requests += len(orders)
        ids = json.dumps(['UID-' + item['order_id'] for item in orders])
        return self.evaluate('return await Promise.all(' + ids + '.map(orderId=>api.getOrderDetail({orderId,needEvaluationStatus:false,getFinderShopNewInfo:true,fromList:true}).then(detailRecord)));')

    def close(self):
        if self.loop and not self.loop.is_closed() and self.shutdown:
            self.loop.call_soon_threadsafe(self.shutdown.set)
        if self.thread:
            self.thread.join(10)
            if self.thread.is_alive():
                raise PageError('订单读取已停止，本地连接仍在退出，请稍候再开始。')
