"""Submit a fully checked document through its visible controls, never hidden APIs.

Each click rechecks answers in the same JS turn. A per-document latch prevents
duplicate submissions after a timeout, retry, or model-generated repeated call.
"""
import json

from browser.assessment_page import verify_fill_script


def submission_script(origin, registry, proposals, expected, answers, *, begin=False):
    request = json.dumps({'origin': origin, 'registry': registry + '_submission',
                          'begin': begin, 'count': len(expected['questions']), 'adapted': bool(proposals)})
    verify = verify_fill_script(origin, registry, proposals, expected, answers, complete=True)
    return r"""(() => {
      const request = REQUEST;
      if (location.origin !== request.origin) return {state:'blocked', message:'题页站点已改变。'};
      const visible = el => el.isConnected && el.getClientRects().length &&
        getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
      const clean = el => (el.innerText || el.textContent || '').replace(/\s+/gu,'').trim();
      const enabled = el => !el.disabled && el.getAttribute('aria-disabled') !== 'true';
      let latch = window[request.registry];
      if (latch && latch.url !== location.href) return {state:'unknown', message:'提交后地址发生变化，需要重新扫描确认状态。'};
      // Only status components outside question content count as a receipt.
      const statuses = [...document.querySelectorAll('[role=status], [role=alert], .el-message, .el-notification, .submit-success, .exam-result')]
        .filter(el => visible(el) && !el.closest('fieldset, [data-question-id], section.subject-item'));
      const success = statuses.find(el => !/未|没有|失败|错误|无法/u.test(clean(el)) &&
        /(?:交卷成功|提交成功|已成功提交|试卷已提交)(?:[！!。\s]|$)/u.test(clean(el)));
      if (latch?.clicked && success && !latch.oldStatuses.includes(success)) {
        latch.done = true;
        return {state:'submitted', message:clean(success).slice(0,200)};
      }
      if (latch?.done) return {state:'submitted', message:'页面已确认提交成功。'};
      if (latch && !latch.clicked) return {state:'blocked', message:'先前提交操作未完成，请核对页面。'};
      if (!latch && !request.begin) return {state:'blocked', message:'尚未发起提交。'};
      const verified = VERIFY;
      if (verified.error || verified.filled?.length !== request.count || request.count === 0) {
        return {state: latch ? 'unknown' : 'blocked', message:verified.error || '存在未写入或已变化的答案，已停止提交。'};
      }
      // Visible multi-page controls or hidden question groups mean this reader
      // cannot prove that all questions in the assignment have been reviewed.
      const paging = [...document.querySelectorAll('button, a, [role=button]')].some(el => visible(el) && enabled(el) && /^(下一页|下一题|下页)$/u.test(clean(el)));
      const hiddenQuestions = [...document.querySelectorAll('fieldset, [data-question-id], section.subject-item')]
        .some(el => !visible(el) && el.querySelector('input, textarea, [contenteditable=true]'));
      const pagination = [...document.querySelectorAll('.el-pagination, .pagination, [aria-label*=分页], [aria-label*=pagination]')].some(visible);
      const totals = [...document.querySelectorAll('.exam-header, .exam-title, .question-count, .subject-count, h1, h2, h3')]
        .filter(visible).map(el => el.innerText.match(/共\s*(\d+)\s*题/u)).filter(Boolean).map(match => Number(match[1]));
      // Model-proposed groups can omit an unrendered question. Require an
      // independent visible total before allowing a custom layout to submit.
      if (paging || pagination || hiddenQuestions || (totals.length && !totals.includes(request.count)) || (request.adapted && !totals.includes(request.count)))
        return {state:'blocked', message:'题目可能分布在多页或未完整展开，暂不自动提交。'};
      const dialogs = [...document.querySelectorAll('[role=dialog], .el-message-box, .el-dialog, dialog[open]')]
        .filter(el => visible(el) && /交卷|提交作业|提交答案|提交答卷/u.test(clean(el)));
      if (latch?.clicked) {
        if (latch.confirmed || !dialogs.length) return {state:'waiting', message:'等待网站提交回执…'};
        // Nested dialog wrappers must identify one and the same button.
        const buttons = [...new Set(dialogs.flatMap(el => [...el.querySelectorAll('button, [role=button]')]))]
          .filter(el => visible(el) && enabled(el) && /^(确定|确认|确认交卷|确定交卷|交卷|提交|确认提交|确定提交)$/u.test(clean(el)));
        if (buttons.length !== 1 || dialogs.some(el => /(?:[1-9]\d*道?题?未(?:作)?答|未(?:作)?答[：:]?[1-9])/u.test(clean(el))))
          return {state:'blocked', message:'确认弹窗不明确或提示仍有未答题，已停止。'};
        latch.confirmed = true;
        buttons[0].click();
        return {state:'waiting', message:'已确认交卷，等待网站回执…'};
      }
      if (dialogs.length) return {state:'blocked', message:'页面已有提交弹窗，请关闭后重新执行。'};
      const buttons = [...document.querySelectorAll('button, a, [role=button], input[type=submit]')]
        .filter(el => visible(el) && enabled(el) && !el.closest('fieldset, [data-question-id], section.subject-item') &&
          /^(交卷|提交|提交作业|提交答案|提交答卷|提交试卷)$/u.test(el.tagName === 'INPUT' ? el.value : clean(el)));
      if (buttons.length !== 1) return {state:'blocked', message:'没有找到唯一的提交按钮，保留已填写答案。'};
      latch = {url:location.href, clicked:true, confirmed:false, done:false, oldStatuses:success ? [success] : []};
      Object.defineProperty(window, request.registry, {value:latch});
      buttons[0].click();
      return {state:'waiting', message:'已点击提交，等待网站确认…'};
    })()""".replace('REQUEST', request).replace('VERIFY', verify)
