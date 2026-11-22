"""Compatibility response for older callers. Final submission belongs to the user."""


def submission_script(origin, registry, proposals, expected, answers, *, begin=False):
    return "({state:'awaiting_review', message:'请检查答案并在网页上手动提交。'})"
