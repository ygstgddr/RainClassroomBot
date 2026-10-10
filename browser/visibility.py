"""检测用户实际可见的验证界面，忽略站点预加载的透明、屏外 iframe。"""
from playwright.async_api import Error as PlaywrightError


ON_SCREEN = '''e => {
    for (let n=e; n; n=n.parentElement) {
        const s=getComputedStyle(n);
        if (s.display==='none' || s.visibility==='hidden' || s.visibility==='collapse' ||
            Number(s.opacity)<=0.01) return false;
    }
    const r=e.getBoundingClientRect();
    const left=Math.max(0,r.left), top=Math.max(0,r.top);
    const right=Math.min(innerWidth,r.right), bottom=Math.min(innerHeight,r.bottom);
    if (right-left<2 || bottom-top<2) return false;
    const hit=document.elementFromPoint((left+right)/2,(top+bottom)/2);
    return hit===e || e.contains(hit);
}'''


async def visible_security_challenge(frame):
    if not frame.parent_frame or 'drag_ele' not in frame.url:
        return False
    try:
        current = frame
        # Playwright is_visible 不检查透明度，也不能单独证明整个嵌套 iframe 在屏幕内。
        while current.parent_frame:
            element = await current.frame_element()
            if not await element.evaluate(ON_SCREEN):
                return False
            current = current.parent_frame
        for label in await frame.get_by_text('安全验证', exact=True).all():
            if await label.evaluate(ON_SCREEN):
                return True
    except PlaywrightError:
        pass  # 导航中验证框被移除不应触发登录暂停。
    return False
