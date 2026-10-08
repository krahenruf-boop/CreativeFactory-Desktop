"""Conservative display-wrapper cleanup; never rewrite the confirmed script."""
import re


def prepare_tts_text(text: str) -> str:
    text = text.replace('\ufeff', '').replace('\u200b', '').replace('\u2060', '').strip()
    # Only whole-document fences and explicitly named project wrappers.
    for _ in range(4):
        before = text
        fence = re.fullmatch(r'(```|~~~)[\w-]*[ \t]*\r?\n(.*?)\r?\n\1', text, re.S)
        if fence and fence.group(1) not in fence.group(2):
            text = fence.group(2).strip()
        tag = re.fullmatch(r'<(tts_text|localized_text)>(.*?)</\1>', text, re.S | re.I)
        if tag:
            text = tag.group(2).strip()
        text = re.sub(r'^(?:\[(?:TTS|tts_text|localized_text|Creative Factory|S2)\]|(?:tts_text|localized_text|配音文案|本土化文案)\s*[:：])[ \t]*\r?\n', '', text, flags=re.I).strip()
        for left, right in [('"', '"'), ("'", "'"), ('“', '”'), ('‘', '’')]:
            paired = text.startswith(left) and text.endswith(right) and len(text) >= 2
            only_outer = text.count(left) == (2 if left == right else 1) and (left == right or text.count(right) == 1)
            if paired and only_outer:
                text = text[1:-1].strip()
                break
        if text == before:
            break
    # Whitespace only: preserve accents, words, prices, punctuation and inner quotes.
    return ' '.join(text.split())


def tts_request_payload(payload):
    """Cache identity uses the exact spoken text, not source/binding metadata."""
    result = {k: v for k, v in payload.items() if k not in ['script_id', 'revision', 'provider', 'tts_text']}
    result['text'] = payload.get('tts_text', payload['text'])
    return result
