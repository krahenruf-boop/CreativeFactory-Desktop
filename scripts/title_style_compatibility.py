"""Apply the title-only extension to a copied engine, including future staging."""
from pathlib import Path


def apply(engine):
    engine = Path(engine)
    path = engine/'prompts/DEEPSEEK_TIKTOK_BR_V1.txt'
    text = path.read_text()
    text = text.replace(
        'Aim for 3–10 words and 1–2 lines for a Hook visible for the first five seconds.',
        'Use complete natural on-screen hooks without a word-count quota. '
        'Choose relevant angles freely; the caller handles exactly two lines '
        'and font fitting without rewriting the approved text.')
    text = text.replace('handles up to three lines ', 'handles exactly two lines ')
    path.write_text(text)

    path = engine/'prompts/DEEPSEEK_TIKTOK_BR_BATCH_V1.txt'
    text = path.read_text().replace(
        '1 = pain_hook (A 强痛点), 2 = product_benefit (B 强卖点), 3 = emotion (C 强情绪).',
        'IDs 1, 2, 3 identify candidates only. Choose any relevant angles from '
        'the evidence; do not assign a fixed category to an ID.')
    text = text.replace(
        'Titles 3–10 words, fit 1–2 lines.',
        'No word-count quota. Preserve natural complete hooks; the caller '
        'handles exactly two lines and font fitting; never a third line.')
    text = text.replace('handles up to three lines and font fitting.',
                        'handles exactly two lines and font fitting; never a third line.')
    text = text.replace('"angle":"pain_hook"', '"angle":"chosen_angle_1"')
    text = text.replace('"angle":"product_benefit"', '"angle":"chosen_angle_2"')
    text = text.replace('"angle":"emotion"', '"angle":"chosen_angle_3"')
    path.write_text(text)

    path = engine/'app/s3/deepseek_copy.py'
    text = path.read_text().replace(
        'text: str = Field(min_length=1, max_length=150)',
        'text: str = Field(min_length=1, max_length=300)')
    text = text.replace(
        "    if any(len(t['text'].split())>14 for t in titles):\n"
        "        raise DeepSeekError('INVALID_DEEPSEEK_OUTPUT')\n", '')
    if 'from autocut.title_style import GUIDANCE' not in text:
        anchor = "    prompt=(ROOT/'prompts'/f'{PROMPT_VERSION}.txt').read_text()"
        if anchor not in text:
            raise ValueError('TITLE_STYLE_PREPARE_ANCHOR_MISSING')
        text = text.replace(anchor, anchor + '\n'
            '    from autocut.title_style import GUIDANCE\n'
            "    prompt += '\\n' + GUIDANCE", 1)
    path.write_text(text)

    path = engine/'app/s3/deepseek_batch.py'
    text = path.read_text().replace(
        "                    if [t['angle'] for t in result['titles']] != ['pain_hook','product_benefit','emotion']:\n"
        "                        raise DeepSeekError('INVALID_DEEPSEEK_OUTPUT')\n", '')
    text = text.replace(
        "                    if any(len(t['text'].split()) > 10 for t in result['titles']):\n"
        "                        raise DeepSeekError('INVALID_DEEPSEEK_OUTPUT')\n", '')
    path.write_text(text)


if __name__ == '__main__':
    apply(Path(__file__).resolve().parents[1]/'staging/engine')
