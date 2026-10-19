"""本地验收站：不连接真实课程。服务器仅在页面上报真实 ended/按钮操作后记完成。"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


VIDEO = '''<!doctype html><html><body><h1>测试视频</h1><video controls width="480"></video>
<script>
(async()=>{
 const canvas=document.createElement('canvas'); canvas.width=320;canvas.height=180;
 const ctx=canvas.getContext('2d'); const stream=canvas.captureStream(12);
 const recorder=new MediaRecorder(stream,{mimeType:'video/webm'}), chunks=[];
 recorder.ondataavailable=e=>{if(e.data.size)chunks.push(e.data)};
 const stopped=new Promise(resolve=>recorder.onstop=resolve);
 recorder.start();let count=0;
 const draw=setInterval(()=>{ctx.fillStyle=count++%2?'#2255bb':'#5599dd';ctx.fillRect(0,0,320,180)},50);
 await new Promise(resolve=>setTimeout(resolve,1300));recorder.stop();await stopped;clearInterval(draw);
 stream.getTracks().forEach(t=>t.stop());
 const video=document.querySelector('video');video.src=URL.createObjectURL(new Blob(chunks,{type:'video/webm'}));
 video.addEventListener('ended',()=>fetch('/observed-ended',{method:'POST'}));
})();
</script></body></html>'''

ARTICLE = '''<!doctype html><html><body><article style="height:200px;overflow:auto;width:500px">
<div style="height:480px">本地测试正文。需要滚动并点击阅读确认。</div>
<button onclick="fetch('/observed-read',{method:'POST'})">我已阅读</button>
</article></body></html>'''

SLIDES = '''<!doctype html><html><body>
<h1>测试 PPT</h1><div class="pptSwiper" tabindex="0"><div class="swiper-wrapper"></div></div>
<script>
const wrap=document.querySelector('.swiper-wrapper'); let index=0, sent=new Set();
for(let i=0;i<3;i++) {
 const el=document.createElement('div'); el.className='swiper-slide';
 el.innerHTML='<img width="400" height="300" src="IMAGE_URL"><span>Page '+(i+1)+'</span>';
 wrap.append(el);
}
function render(){[...wrap.children].forEach((el,i)=>{
 el.classList.toggle('swiper-slide-active',i===index);el.style.display=i===index?'block':'none';
});}
document.addEventListener('keydown',e=>{
 if(e.key==='ArrowDown')index=Math.min(2,index+1);
 if(e.key==='ArrowUp')index=Math.max(0,index-1);
 render();
});render();
setInterval(()=>{
 const img=wrap.children[index].querySelector('img');
 if(document.visibilityState==='visible' && img.complete && img.naturalWidth>0 && !sent.has(index)){
   sent.add(index);fetch('/observed-slide/'+index,{method:'POST'});
 }
},600);
</script></body></html>'''


class MockSite:
    def __init__(self):
        self.finished = {}
        self.status_reads = 0
        self.delay = 1.2
        self.requests = []
        self.fail_schedule = False
        self.omit_unstarted_schedules = False
        self.user_id = 7
        self.include_slides = False
        self.slide_reads = {}
        self.ignore_slide_reads = False
        self.slides_broken = False
        self.slides_have_problem = False
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, value, status=200, cookie=None):
                raw = (json.dumps(value).encode() if isinstance(value, dict) else value.encode())
                self.send_response(status)
                self.send_header('Content-Type', 'application/json' if isinstance(value, dict) else 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                if cookie:
                    self.send_header('Set-Cookie', cookie)
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                route = urlparse(self.path).path
                query = parse_qs(urlparse(self.path).query)
                site.requests.append(('GET', route))
                if route == '/login':
                    return self.send('logged in', cookie='mock_auth=yes; Path=/; Max-Age=86400')
                if route in ('/v2/web', '/v2/web/studentLog/10'):
                    return self.send('<a href="/video/1">测试视频</a><a href="/article/2">测试图文</a>')
                if route == '/video/1':
                    return self.send(VIDEO)
                if route == '/article/2':
                    return self.send(ARTICLE)
                if route == '/v2/web/studentCards/10/30/40/ppt':
                    image = '/missing.png' if site.slides_broken else 'data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%22400%22 height=%22300%22%3E%3Crect width=%22400%22 height=%22300%22 fill=%22lightblue%22/%3E%3C/svg%3E'
                    return self.send(SLIDES.replace('IMAGE_URL', image))
                if 'mock_auth=yes' not in self.headers.get('Cookie', ''):
                    return self.send({'errcode': 1}, 401)
                if route == '/v2/api/web/userinfo':
                    return self.send({'errcode': 0, 'data': [{'user_id': site.user_id, 'name': '本地验收账号'}]})
                if route == '/v2/api/web/courses/list':
                    return self.send({'errcode': 0, 'data': {'list': [
                        {'classroom_id': 10, 'course': {'name': '本地测试课程', 'university_id': 1}}]}})
                if route == '/mooc-api/v1/lms/learn/course/chapter':
                    if query.get('sign') != ['local-test-sign'] or query.get('classroom_id') != ['10']:
                        return self.send({'success': False, 'msg': 'missing course context'}, 400)
                    extra = [{'id': 3, 'name': '测试 PPT', 'leaf_type': 7}] if site.include_slides else []
                    return self.send({'success': True, 'data': {'course_chapter': [{'section_leaf_list': [
                        {'id': 1, 'name': '测试视频', 'leaf_type': 0},
                        {'leaf_list': [{'id': 2, 'name': '测试图文', 'leaf_type': 3}]}, *extra]}]}})
                if route == '/mooc-api/v1/lms/learn/course/schedule':
                    if query.get('sign') != ['local-test-sign'] or query.get('classroom_id') != ['10']:
                        return self.send({'success': False, 'msg': 'missing course context'}, 400)
                    site.status_reads += 1
                    if site.fail_schedule:
                        return self.send({'success': False}, 503)
                    schedules = {
                        lid: 1 if lid in site.finished and time.monotonic()-site.finished[lid]>=site.delay else 0
                        for lid in ('1', '2')
                        if not site.omit_unstarted_schedules or
                        lid in site.finished and time.monotonic()-site.finished[lid]>=site.delay}
                    if site.include_slides:
                        schedules['3'] = {'done': sum(time.monotonic()-when>=site.delay for when in site.slide_reads.values()), 'total': 3}
                    return self.send({'success': True, 'data': {'leaf_schedules': schedules}})
                if route == '/edu_admin/leaf_level_info/':
                    return self.send({'classroom_id': 10, 'courseware_id': 30, 'activity_id': 40})
                if route == '/v2/api/web/cards/detlist/30':
                    slides = [{'PageIndex': i+1, 'Problem': {'type': 'question'} if site.slides_have_problem and i==1 else None} for i in range(3)]
                    return self.send({'errcode': 0, 'data': {'Slides': slides}})
                if route == '/v2/api/web/cards/view_depth':
                    return self.send({'errcode': 0, 'data': {'depth_detail': [i for i,when in site.slide_reads.items() if time.monotonic()-when>=site.delay]}})
                if route == '/v2/api/web/classrooms/10':
                    return self.send({'errcode': 0, 'data': {'course_sign': 'local-test-sign', 'uv_id': 1}})
                return self.send({'success': False}, 404)

            def do_POST(self):
                site.requests.append(('POST', self.path))
                if self.path == '/observed-ended':
                    site.finished['1'] = time.monotonic()
                elif self.path == '/observed-read':
                    site.finished['2'] = time.monotonic()
                elif self.path.startswith('/observed-slide/') and not site.ignore_slide_reads:
                    site.slide_reads[int(self.path.rsplit('/', 1)[-1])] = time.monotonic()
                self.send({'success': True})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
