"""Submit a brief voice reply as JSON data to the local mic service."""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from tools.voice_line import check_line, check_expects_answer, limit_refusal, DEFAULT_LIMITS


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def submit_line(conversation_id, kind, text, action=None, final=False, endpoint=None, expects_answer=False):
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        return {"outcome": "refused", "reason": "missing_conversation_id"}
    if kind != "reply" and not kind.startswith("announcement:"):
        return {"outcome": "refused", "reason": "invalid_kind"}
    if final and expects_answer:
        return {"outcome": "refused", "reason": "final_and_expects_answer"}
    base = endpoint or os.environ.get("BART_SPEAK_URL", "http://127.0.0.1:7780")
    try:
        url = urllib.parse.urlsplit(base)
        if url.scheme != "http" or url.hostname not in {"127.0.0.1", "::1", "localhost"} or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}:
            return {"outcome": "refused", "reason": "nonlocal_endpoint"}
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(base.rstrip('/')+'/status', timeout=3) as response:
            status = json.loads(response.read(131073))
        replies = status['speaker']['rules']['replies']
        limits = {key:replies[key] for key in DEFAULT_LIMITS}
        if any(type(n) is not int or n < 1 for n in limits.values()):
            return {"outcome":"refused", "reason":"invalid_rules"}
        refusal = limit_refusal(text, limits)
        if refusal:
            return refusal
        reason = check_expects_answer(text, limits) if expects_answer else check_line(text, limits)
        if reason:
            return {"outcome":"refused", "reason":reason}
        payload = {"conversation_id": conversation_id, "kind": kind, "text": text, "final": bool(final)}
        if expects_answer:
            payload["expects_answer"] = True
        if action is not None:
            payload["action"] = action
        request = urllib.request.Request(base.rstrip('/') + '/speak', data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
        # No proxy, redirect or shell can send this line somewhere else.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            response = opener.open(request, timeout=35)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            result = json.loads(response.read(8193))
        if not isinstance(result, dict) or result.get("outcome") not in {"spoken", "suppressed", "refused"}:
            return {"outcome": "refused", "reason": "invalid_response"}
        if result["outcome"] != "spoken" and not isinstance(result.get("reason"), str):
            return {"outcome": "refused", "reason": "invalid_response"}
        return result
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError):
        return {"outcome": "refused", "reason": "speaker_unavailable"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--conversation-id', required=True)
    parser.add_argument('--kind', default='reply')
    parser.add_argument('--text', help='Omit to read literal text from stdin.')
    parser.add_argument('--action')
    parser.add_argument('--final', action='store_true')
    parser.add_argument('--expects-answer', action='store_true')
    args = parser.parse_args(argv)
    text = args.text if args.text is not None else sys.stdin.read(8193).rstrip('\n')
    extra = {'expects_answer': True} if args.expects_answer else {}
    print(json.dumps(submit_line(args.conversation_id, args.kind, text, args.action, args.final, **extra)))
    return 0
