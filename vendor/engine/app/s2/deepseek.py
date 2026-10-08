"""Thin Headless DeepSeek transport. Credentials stay in the process environment.

No video decisions, business validation, provider fallback, or request-body logs.
"""
import json
import os
import time

import httpx


class DeepSeekError(Exception):
    def __init__(self, code, uncertain=False, retry_safe=False):
        super().__init__(code)
        self.code, self.uncertain, self.retry_safe = code, uncertain, retry_safe


def parse_json_content(content):
    """Local syntax-only tolerance. Never invent missing text or use eval.

    Accept prose/fences around a JSON object and trailing commas outside strings.
    An ambiguous multi-object answer or duplicate key still requires review.
    """
    chars = []; quoted = escaped = False
    for i, ch in enumerate(content):
        if quoted:
            chars.append(ch)
            if escaped: escaped = False
            elif ch == '\\': escaped = True
            elif ch == '"': quoted = False
        else:
            if ch == '"': quoted = True
            if ch == ',' and content[i+1:].lstrip().startswith(('}', ']')):
                continue
            chars.append(ch)
    cleaned = ''.join(chars)
    def unique(pairs):
        result = {}
        for k, v in pairs:
            if k in result: raise ValueError('DUPLICATE_KEY')
            result[k] = v
        return result
    decoder = json.JSONDecoder(object_pairs_hook=unique)
    candidates = []; i = 0
    while i < len(cleaned):
        if cleaned[i] != '{':
            i += 1; continue
        try:
            value, length = decoder.raw_decode(cleaned[i:])
            candidates.append(value); i += length
        except ValueError:
            # Do not reinterpret an inner object as a complete broken response.
            raise ValueError('INVALID_JSON') from None
    if len(candidates) != 1:
        raise ValueError('INVALID_JSON')
    return candidates[0]


class DeepSeekAdapter:
    endpoint = 'https://api.deepseek.com/chat/completions'

    def __init__(self, client=None):
        self._key = os.environ.get('DEEPSEEK_API_KEY', '').strip()
        if not self._key:
            raise DeepSeekError('DEEPSEEK_NOT_CONFIGURED')
        self.client = client or httpx.Client(timeout=httpx.Timeout(90, connect=10),
                                             follow_redirects=False, trust_env=False)

    def complete(self, model, messages, *, max_tokens=3000):
        start = time.monotonic()
        try:
            response = self.client.post(self.endpoint,
                headers={'Authorization': 'Bearer ' + self._key},
                json={'model':model, 'messages':messages, 'response_format':{'type':'json_object'},
                      'max_tokens':max_tokens, 'stream':False, 'temperature':.7})
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise DeepSeekError('DEEPSEEK_TIMEOUT', retry_safe=True) from None
        except httpx.TimeoutException:
            raise DeepSeekError('DEEPSEEK_TIMEOUT', uncertain=True) from None
        except httpx.HTTPError:
            raise DeepSeekError('DEEPSEEK_HTTP_ERROR', uncertain=True) from None
        if response.status_code == 429:
            raise DeepSeekError('DEEPSEEK_RATE_LIMITED')
        if response.status_code >= 400 or response.is_redirect:
            raise DeepSeekError('DEEPSEEK_HTTP_ERROR', uncertain=response.status_code >= 500)
        try:
            body = response.json()
            choice = body['choices'][0]
            content = choice['message']['content']
            if not isinstance(content, str) or choice['finish_reason'] != 'stop':
                raise ValueError()
            # Don't persist credentials even if a defective response echoes one.
            if self._key in content:
                raise ValueError()
            data = parse_json_content(content)
        except (ValueError, KeyError, TypeError, IndexError):
            raise DeepSeekError('DEEPSEEK_INVALID_JSON') from None
        usage = {k:v for k,v in body.get('usage',{}).items()
                 if k in {'prompt_tokens','completion_tokens','total_tokens',
                          'prompt_cache_hit_tokens','prompt_cache_miss_tokens'}
                 and isinstance(v,int)}
        request_id = response.headers.get('x-request-id') or response.headers.get('request-id') or body.get('id')
        if not isinstance(request_id,str) or len(request_id)>200 or self._key in request_id:
            request_id = None
        return dict(data=data, usage=usage, request_id=request_id,
                    model=body.get('model',model), latency_seconds=round(time.monotonic()-start,3))

    def close(self):
        self.client.close()
