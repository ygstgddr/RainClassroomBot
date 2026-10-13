"""用户明确绑定一个网页编辑框后，以真实浏览器输入事件逐字追加。

不使用剪贴板、不写编辑器内部数据、不保存用户正文、不自动提交。
绑定保留 ElementHandle，导航或 DOM 替换后绝不按 selector 找另一个框。
"""
import asyncio
import copy
import inspect
import json
import math
import secrets
import time
import weakref
from urllib.parse import urlsplit


class ManualTypingError(ValueError):
    """可直接显示给用户的输入操作错误。"""


# 所有插入内容均作为 Playwright 参数传递，不能拼到页面 JavaScript 中。
_PICK = r"""({token}) => {
  const key='__rainManualTypingPicker';
  window[key]?.cleanup?.();
  const state={token,status:'selecting',element:null,error:''};
  const badge=document.createElement('div');
  badge.textContent='点击要输入的文本框 · Esc 取消';
  Object.assign(badge.style,{position:'fixed',top:'12px',left:'12px',zIndex:'2147483647',
    pointerEvents:'none',padding:'10px 16px',borderRadius:'14px',background:'rgba(17,37,68,.85)',
    color:'white',font:'15px system-ui',boxShadow:'0 3px 18px #0003'});
  const ring=document.createElement('div');
  Object.assign(ring.style,{position:'fixed',zIndex:'2147483646',pointerEvents:'none',
    border:'3px solid #3297ff',borderRadius:'7px',boxSizing:'border-box',display:'none'});
  (document.body||document.documentElement).append(badge,ring);
  const editable=(node) => {
    if(!(node instanceof Element))return null;
    let e=node.closest('input,textarea');
    if(!e && node.isContentEditable){e=node;while(e.parentElement?.isContentEditable)e=e.parentElement;}
    if(!e || e.matches('input')&&!['text','search','email','url','tel'].includes(e.type))return null;
    if(e.disabled||e.readOnly||e.getAttribute('aria-disabled')==='true'||e.getAttribute('aria-readonly')==='true')return null;
    if(!e.matches('input,textarea')&&!e.isContentEditable)return null;
    return e;
  };
  const move=e=>{const n=editable(e.composedPath()[0]);if(!n){ring.style.display='none';return;}
    const r=n.getBoundingClientRect();Object.assign(ring.style,{display:'block',left:r.left+'px',top:r.top+'px',width:r.width+'px',height:r.height+'px'});};
  const block=e=>{e.preventDefault();e.stopImmediatePropagation();};
  const click=e=>{block(e);const n=editable(e.composedPath()[0]);
    if(!n){state.error='请选择可编辑的文本框；密码、文件和只读框不能使用';return;}
    state.element=n;state.status='selected';n.focus({preventScroll:true});state.cleanup();};
  const escape=e=>{if(e.key==='Escape'){block(e);state.status='cancelled';state.cleanup();}};
  for(const name of ['pointerdown','mousedown','mouseup'])document.addEventListener(name,block,true);
  document.addEventListener('click',click,true);document.addEventListener('pointermove',move,true);
  document.addEventListener('keydown',escape,true);
  state.cleanup=()=>{for(const name of ['pointerdown','mousedown','mouseup'])document.removeEventListener(name,block,true);
    document.removeEventListener('click',click,true);document.removeEventListener('pointermove',move,true);
    document.removeEventListener('keydown',escape,true);badge.remove();ring.remove();};
  window[key]=state;
}"""

_DESCRIBE = r"""el => {
  const tag=el.tagName.toLowerCase(),type=tag==='input'?el.type:tag==='textarea'?'textarea':'contenteditable';
  const label=el.labels?.[0]?.cloneNode(true);label?.querySelectorAll('input,textarea,[contenteditable]').forEach(n=>n.remove());
  const text=(el.getAttribute('aria-label') || label?.textContent || el.getAttribute('placeholder') || el.getAttribute('name') || el.id || '未命名文本框').trim().slice(0,100);
  return {tag,type,label:text,multiline:type==='textarea'||type==='contenteditable',characters:tag==='input'||tag==='textarea'?el.value.length:(el.textContent||'').length};
}"""

# beforeinput 及 keydown 在 DOM 事件交付处再次守卫：即使焦点恰好在两次 RPC
# 之间移动，输入事件也会被阻止。其他 frame 同时安装 target=null 的输入守卫。
_INSTALL_GUARD = r"""({token,element,type}) => {
  const key='__rainManualTypingGuard';window[key]?.cleanup?.();
  const s={token,target:element,type,armed:false,fault:'',accepted:0};
  const valid=()=>{const e=s.target;if(!e||!e.isConnected||e.ownerDocument!==document)return false;
    const tag=e.tagName.toLowerCase();if((tag==='input'?e.type:tag==='textarea'?'textarea':e.isContentEditable?'contenteditable':'')!==s.type)return false;
    if(e.disabled||e.readOnly||e.getAttribute('aria-disabled')==='true'||e.getAttribute('aria-readonly')==='true')return false;
    for(let n=e;n;n=n.parentElement){const st=getComputedStyle(n);if(st.display==='none'||['hidden','collapse'].includes(st.visibility)||st.contentVisibility==='hidden'||Number(st.opacity)===0)return false;}
    const r=e.getBoundingClientRect();return r.width>0&&r.height>0;};
  const guard=e=>{if(!s.armed)return;const target=s.target;
    if(!valid()||s.fault||document.activeElement!==target||!(e.target===target||target?.isContentEditable&&target.contains(e.target))){
      s.fault=s.fault||(valid()?'focus':'invalid');e.preventDefault();e.stopImmediatePropagation();}};
  const focus=e=>{if(s.armed&&e.target!==s.target&&!s.target?.contains(e.target))s.fault='focus';};
  const input=e=>{if(s.armed&&e.isTrusted&&(e.target===s.target||s.target?.isContentEditable&&s.target.contains(e.target)))s.accepted++;};
  const submit=e=>{if(s.armed){e.preventDefault();e.stopImmediatePropagation();}};
  document.addEventListener('beforeinput',guard,true);document.addEventListener('keydown',guard,true);
  document.addEventListener('focusin',focus,true);document.addEventListener('submit',submit,true);document.addEventListener('input',input,true);
  s.cleanup=()=>{document.removeEventListener('beforeinput',guard,true);document.removeEventListener('keydown',guard,true);
    document.removeEventListener('focusin',focus,true);document.removeEventListener('submit',submit,true);document.removeEventListener('input',input,true);};
  s.valid=valid;window[key]=s;
}"""

_VALIDATE = r"""(el,{token,type,focused}) => {
  const s=window.__rainManualTypingGuard;
  const accepted=s?.accepted||0;
  if(!s||s.token!==token||s.target!==el||!s.valid()||s.type!==type)return {fault:'invalid',accepted};
  if(s.fault==='invalid')return {fault:'invalid',accepted};
  if(focused&&(s.fault||document.activeElement!==el||!document.hasFocus()))return {fault:'focus',accepted};
  return {fault:'',accepted};
}"""

# 完整导航会销毁旧文档的捕获监听。每个曾绑定的专用 page 安装一次启动守卫：
# 新文档先拒绝输入，再询问工作进程。没有绑定时立即开放正常人工编辑；有绑定
# 时持续拒绝，直到在途最多一个字符操作结束、旧绑定已安全清理。
_BOOTSTRAP_GUARD = r"""(() => {
  const state={locked:true};window.__rainManualTypingNavigation=state;
  const guard=e=>{if(state.locked&&e.isTrusted){e.preventDefault();e.stopImmediatePropagation();}};
  document.addEventListener('beforeinput',guard,true);document.addEventListener('keydown',guard,true);
  Promise.resolve(window[BINDING_NAME]()).then(locked=>{state.locked=!!locked}).catch(()=>{});
})()"""


class ManualTyping:
    """仅由工作进程的一个 asyncio 事件循环调用，不创建全局键盘钩子。

    select() 非阻塞；选择完成通过 on_update(snapshot) 发布 ready。
    start/resume 必须携带 UI 当前显示的 token，start 还要求 confirmed=True。
    控制响应不依赖字符间隔，暂停/停止通过 Event 唤醒异步等待。
    """
    def __init__(self, browser, on_update=None):
        self.browser = browser
        self.on_update = on_update
        self._state = dict(phase='idle', target=None, sent=0, total=0, speed=20,
                           message='先选择网页中的目标文本框')
        self._target = self._page = self._frame = None
        self._chain = []
        self._guards = []
        self._pick_frames = []
        self._selector = self._runner = None
        self._selection_token = None
        self._text = ''
        self._wake = asyncio.Event()
        self._last_progress = 0.0
        self._listeners = []
        self._operation_lock = asyncio.Lock()
        self._protected_pages = weakref.WeakSet()
        self._navigation_binding = '__rainTypingNavigation_' + secrets.token_hex(10)

    def snapshot(self):
        return copy.deepcopy(self._state)

    def _publish(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_progress < .25:
            return
        self._last_progress = now
        if self.on_update:
            result = self.on_update(self.snapshot())
            if inspect.isawaitable(result):
                asyncio.create_task(result)

    def _show(self, phase, message):
        self._state.update(phase=phase, message=message)
        self._publish(True)

    @staticmethod
    def _origin(url):
        parsed = urlsplit(url)
        if parsed.scheme in ('http', 'https'):
            hostname = parsed.hostname or '未知主机'
            if ':' in hostname:
                hostname = '[' + hostname + ']'
            return parsed.scheme + '://' + hostname + (':' + str(parsed.port) if parsed.port else '')
        return parsed.scheme + ':' if parsed.scheme else '未知页面'

    @staticmethod
    def _speed(value):
        try:
            value = float(value)
        except (ValueError, TypeError):
            raise ManualTypingError('输入速度须为每秒 1～60 个字符') from None
        if not math.isfinite(value) or not 1 <= value <= 60:
            raise ManualTypingError('输入速度须为每秒 1～60 个字符')
        return value

    async def select(self, timeout_seconds=90):
        """开始一次点击选择，先停掉上次输入与绑定，立即返回。"""
        await self.stop()
        if getattr(self.browser, 'actual_headless', getattr(self.browser, 'config', {}).get('headless', False)):
            raise ManualTypingError('请关闭后台运行模式，使用可见 Edge 窗口选择文本框')
        if not self.browser.context or not self.browser.page or self.browser.page.is_closed():
            raise ManualTypingError('请先打开助手专用 Edge 网页')
        self._state.update(target=None, sent=0, total=0)
        self._selection_token = secrets.token_urlsafe(18)
        self._show('selecting', '请在专用 Edge 中点击要输入的文本框，Esc 取消')
        self._selector = asyncio.create_task(self._pick(timeout_seconds))
        return self.snapshot()

    async def select_target(self, timeout_seconds=90):
        """供自动化测试/集成调用：等待一次选择完毕。"""
        await self.select(timeout_seconds)
        await self._selector
        return self.snapshot()

    async def _pick(self, timeout_seconds):
        token = self._selection_token
        started = time.monotonic()
        try:
            await self.browser.page.bring_to_front()
            while self._state['phase'] == 'selecting':
                for page in list(self.browser.context.pages):
                    if page.is_closed():
                        continue
                    for frame in page.frames:
                        if frame not in self._pick_frames:
                            await frame.evaluate(_PICK, {'token': token})
                            self._pick_frames.append(frame)
                        status = await frame.evaluate("token=>{const s=window.__rainManualTypingPicker;return s?.token===token?{status:s.status,error:s.error}:null}", token)
                        if not status:
                            raise ManualTypingError('选择期间页面发生变化，请重新选择文本框')
                        if status['status'] == 'cancelled':
                            self._show('idle', '已取消选择')
                            return
                        if status['error'] and status['error'] != self._state['message']:
                            self._show('selecting', status['error'])
                        if status['status'] == 'selected':
                            handle = await frame.evaluate_handle('() => window.__rainManualTypingPicker.element')
                            element = handle.as_element()
                            if not element:
                                raise ManualTypingError('该文本框已经不可用，请重新选择')
                            await self._bind(page, frame, element)
                            return
                if time.monotonic() - started >= timeout_seconds:
                    self._show('idle', '选择已超时，请重新点击选择文本框')
                    return
                await asyncio.sleep(.15)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._state['target'] = None
            self._show('error', str(exc) if isinstance(exc, ManualTypingError) else '页面发生变化，请重新选择文本框')
        finally:
            await self._clear_picker(token)

    async def _clear_picker(self, token=None):
        for frame in self._pick_frames:
            try:
                await frame.evaluate("token=>{const s=window.__rainManualTypingPicker;if(!token||s?.token===token){s?.cleanup?.();delete window.__rainManualTypingPicker}}", token)
            except Exception:
                pass
        self._pick_frames.clear()

    async def _bind(self, page, frame, element):
        description = await element.evaluate(_DESCRIBE)
        token = secrets.token_urlsafe(24)
        self._page, self._frame, self._target = page, frame, element
        await self._protect_navigation(page)
        cursor = frame
        while cursor.parent_frame:
            handle = await cursor.frame_element()
            self._chain.append((cursor.parent_frame, handle))
            cursor = cursor.parent_frame
        for guard_frame in page.frames:
            await guard_frame.evaluate(_INSTALL_GUARD, dict(token=token,
                element=element if guard_frame == frame else None,
                type=description['type'] if guard_frame == frame else ''))
            self._guards.append(guard_frame)
        self._state['target'] = {**description, 'token': token, 'url': self._origin(page.url),
                                 'frame': self._origin(frame.url)}
        if await self._validate(False):
            raise ManualTypingError('该文本框不可编辑或已隐藏，请重新选择')

        # 跳转、frame 增删、目标页面关闭均使绑定永久失效；不会自动恢复输入。
        def changed(*_):
            if self._target:
                asyncio.create_task(self.invalidate('网页或 iframe 已改变，请重新选择文本框'))
        for name in ('framenavigated', 'frameattached', 'framedetached', 'close', 'crash'):
            page.on(name, changed)
            self._listeners.append((page, name, changed))
        self._show('ready', '已锁定指定文本框；确认后逐字追加，不会提交')

    async def _protect_navigation(self, page):
        if page in self._protected_pages:
            return
        def check(source):
            if self._target and self._page is source['page']:
                asyncio.create_task(self.invalidate('网页或 iframe 已改变，请重新选择文本框'))
                return True
            return False
        await page.expose_binding(self._navigation_binding, check)
        # 仅随机绑定名称进入脚本；用户正文绝不拼接成 JavaScript。
        await page.add_init_script(_BOOTSTRAP_GUARD.replace('BINDING_NAME', json.dumps(self._navigation_binding)))
        # 没有活动绑定时也保留一个轻量导航回调，确保绑定通道暂时失败不会
        # 长期阻挡用户的正常手动编辑；运行期间则保持 fail-closed。
        def idle_navigation(frame):
            if not self._target or self._page is not page:
                asyncio.create_task(self._unlock_idle_frame(page, frame))
        page.on('framenavigated', idle_navigation)
        self._protected_pages.add(page)

    async def _unlock_idle_frame(self, page, frame):
        if self._target and self._page is page:
            return
        try:
            await frame.evaluate('() => {if(window.__rainManualTypingNavigation)window.__rainManualTypingNavigation.locked=false}')
        except Exception:
            pass

    async def _validate(self, focused=True):
        return (await self._validation_details(focused))[0]

    async def _validation_details(self, focused=True):
        if not self._target or not self._page or self._page.is_closed() or not self._state['target']:
            return 'invalid', 0
        try:
            result = await self._target.evaluate(_VALIDATE, dict(token=self._state['target']['token'],
                type=self._state['target']['type'], focused=focused))
            if result['fault']:
                return result['fault'], result['accepted']
            for parent, handle in self._chain:
                outer = await handle.evaluate("""(e,focused) => {
                  if(!e.isConnected)return 'invalid';
                  for(let n=e;n;n=n.parentElement){const s=getComputedStyle(n);if(s.display==='none'||['hidden','collapse'].includes(s.visibility)||s.contentVisibility==='hidden'||Number(s.opacity)===0)return 'invalid';}
                  const r=e.getBoundingClientRect();if(!r.width||!r.height)return 'invalid';
                  return focused&&e.ownerDocument.activeElement!==e?'focus':'';
                }""", focused)
                if outer:
                    return outer, result['accepted']
            return '', result['accepted']
        except Exception:
            return 'invalid', 0

    async def _arm(self, armed, reset=False):
        token = self._state['target']['token'] if self._state['target'] else None
        for frame in self._guards:
            try:
                await frame.evaluate("({token,armed,reset})=>{const s=window.__rainManualTypingGuard;if(s?.token===token){s.armed=armed;if(reset)s.fault=''}}", dict(token=token, armed=armed, reset=reset))
            except Exception:
                if armed:
                    raise ManualTypingError('网页已改变，请重新选择文本框') from None

    def _check_token(self, token):
        if not self._state['target'] or not secrets.compare_digest(str(token), self._state['target']['token']):
            raise ManualTypingError('目标文本框确认已过期，请重新选择并确认')

    async def start(self, token, text, speed=20, confirmed=False):
        async with self._operation_lock:
            self._check_token(token)
            if not confirmed:
                raise ManualTypingError('请先确认目标文本框，再开始输入')
            if self._state['phase'] not in ('ready', 'completed'):
                raise ManualTypingError('当前操作尚未结束，请先暂停或停止')
            if not isinstance(text, str) or not text:
                raise ManualTypingError('请输入需要输出的文字')
            if len(text) > 50000:
                raise ManualTypingError('一次最多输入 50000 个字符，请分段输入')
            text = text.replace('\r\n', '\n').replace('\r', '\n')
            if '\n' in text and self._state['target']['type'] not in ('textarea', 'contenteditable'):
                raise ManualTypingError('所选为单行文本框，不能输入换行；请选择多行文本框')
            if any(ord(c) < 32 and c != '\n' for c in text):
                raise ManualTypingError('输入内容包含制表符或控制字符，请先移除')
            self._state.update(speed=self._speed(speed), sent=0, total=len(text))
            self._text = text
            await self._begin()
            return self.snapshot()

    async def _begin(self):
        if await self._validate(False):
            await self.invalidate('目标文本框已不可用，请重新选择')
            raise ManualTypingError('目标文本框已不可用，请重新选择')
        # 仅显式 start/resume 会聚焦原绑定节点，运行中从不抢回用户焦点。
        # 用户确认按钮在助手窗口中；先展示原 page，再聚焦原节点。
        await self._page.bring_to_front()
        await self._target.focus()
        # 默认追加。只移动原节点的光标/选区，不修改 value 或编辑器内部数据。
        email = await self._target.evaluate("""el => {
          if(el.tagName==='INPUT'&&el.type==='email')return true;
          if(el.tagName==='INPUT'||el.tagName==='TEXTAREA')el.setSelectionRange(el.value.length,el.value.length);
          else {const r=document.createRange();r.selectNodeContents(el);r.collapse(false);
            const s=window.getSelection();s.removeAllRanges();s.addRange(r);}
          return false;
        }""")
        await self._arm(True, reset=True)
        if email:
            await self._page.keyboard.press('End')
        if await self._validate(True):
            await self._arm(False)
            raise ManualTypingError('无法聚焦已选文本框，请重新选择')
        self._wake.clear()
        self._show('running', '正在向已确认文本框输入；切换焦点会自动暂停')
        self._runner = asyncio.create_task(self._type())

    async def _type(self):
        try:
            while self._state['phase'] == 'running' and self._state['sent'] < len(self._text):
                fault, before = await self._validation_details(True)
                if self._state['phase'] != 'running':
                    return
                if fault:
                    if fault == 'focus':
                        self._show('paused', '焦点离开目标文本框，已自动暂停；确认目标后可继续')
                    else:
                        await self._invalidate_from_runner('目标文本框已改变，已停止；请重新选择')
                    return
                started = time.monotonic()
                char = self._text[self._state['sent']]
                if char == '\n':
                    # 换行直接产生原生文本输入，不触发站点 Enter 快捷提交。
                    await self._page.keyboard.insert_text('\n')
                elif ord(char) < 128:
                    await self._page.keyboard.type(char)
                else:
                    # insert_text 支持中文且发送浏览器 beforeinput/input，不是 paste。
                    await self._page.keyboard.insert_text(char)
                fault, after = await self._validation_details(True)
                # input 事件已发生后页面可能自行转移焦点。先记入 sent，继续时不重打。
                if after > before:
                    self._state['sent'] += 1
                if fault:
                    if fault == 'focus':
                        self._show('paused', '焦点改变，已暂停；请核对已输入文字后继续')
                    else:
                        await self._invalidate_from_runner('目标文本框发生变化，已停止；请重新选择')
                    return
                if after == before:
                    self._show('paused', '网页未接受当前字符，已暂停；请检查字数限制或输入框状态')
                    return
                self._publish()
                interval = max(0, 1 / self._state['speed'] - (time.monotonic() - started))
                if interval:
                    try:
                        await asyncio.wait_for(self._wake.wait(), interval)
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
            if self._state['phase'] == 'running':
                self._text = ''
                self._show('completed', '输入完成；请自行核对并提交')
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._invalidate_from_runner('浏览器或文本框已不可用，已停止输入')
        finally:
            await self._arm(False)

    async def pause(self):
        if self._state['phase'] == 'running':
            self._show('paused', '已暂停，继续时仍只向原绑定文本框追加')
            self._wake.set()
            await self._wait_runner()
        return self.snapshot()

    async def resume(self, token):
        async with self._operation_lock:
            self._check_token(token)
            if self._state['phase'] != 'paused' or not self._text:
                raise ManualTypingError('当前没有可继续的输入')
            await self._wait_runner()
            await self._begin()
            return self.snapshot()

    async def set_speed(self, speed):
        self._state['speed'] = self._speed(speed)
        self._wake.set()
        self._publish(True)
        return self.snapshot()

    async def _wait_runner(self):
        runner = self._runner
        if runner and runner is not asyncio.current_task():
            await asyncio.gather(runner, return_exceptions=True)
        self._runner = None

    async def _release_target(self):
        old_page = self._page
        for page, name, listener in self._listeners:
            page.remove_listener(name, listener)
        self._listeners.clear()
        for frame in self._guards:
            try:
                await frame.evaluate("()=>{window.__rainManualTypingGuard?.cleanup?.();delete window.__rainManualTypingGuard}")
            except Exception:
                pass
        self._guards.clear()
        handles = [self._target] + [handle for _, handle in self._chain]
        self._target = self._page = self._frame = None
        self._chain.clear()
        self._state['target'] = None
        if old_page and not old_page.is_closed():
            for frame in old_page.frames:
                try:
                    await frame.evaluate('() => {if(window.__rainManualTypingNavigation)window.__rainManualTypingNavigation.locked=false}')
                except Exception:
                    pass
        for handle in handles:
            if handle:
                try:
                    await handle.dispose()
                except Exception:
                    pass

    async def stop(self):
        self._state['phase'] = 'stopped'
        self._wake.set()
        if self._selector and self._selector is not asyncio.current_task():
            self._selector.cancel()
            await asyncio.gather(self._selector, return_exceptions=True)
        self._selector = None
        await self._wait_runner()
        await self._clear_picker(self._selection_token)
        self._selection_token = None
        self._text = ''
        await self._release_target()
        self._show('stopped', '已停止；再次输入须重新选择并确认文本框')
        return self.snapshot()

    async def cancel(self):
        return await self.stop()

    async def cancel_selection(self):
        return await self.stop()

    async def invalidate(self, reason='目标页面已改变，请重新选择文本框'):
        await self.stop()
        self._show('error', reason)
        return self.snapshot()

    async def _invalidate_from_runner(self, reason):
        self._text = ''
        await self._release_target()
        self._show('error', reason)
