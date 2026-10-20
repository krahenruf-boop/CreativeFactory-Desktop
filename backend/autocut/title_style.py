"""Shared on-screen hook style learned from the user's DeepSeek examples.

Reference conversations:
https://chat.deepseek.com/share/qh1wggmjqv1c2b2k29
https://chat.deepseek.com/share/hlsrbax9c1rtg9m319
Examples teach expression, never establish facts about another product.
"""
VERSION = 'TITLE_NATIVE_HOOK_V1'

GUIDANCE = '''TITLE_NATIVE_HOOK_V1:
Write native TikTok on-screen hooks with a concrete everyday scene, frustration,
discovery, contrast, demonstration, curiosity or emotional relief. Turn supported
features into an understandable use/result, rather than a generic feature list.
Choose each hook's angle from THIS video's script, product and visual evidence.
There is no fixed angle order or mandatory pain/benefit/emotion trio. Make the
candidates meaningfully different in opening, situation and idea, not synonyms.
There is no word-count minimum or maximum. Preserve a complete natural hook,
including a setup and payoff when useful. Prefer readable spoken clauses; the
existing layout must use exactly two lines and can reduce font size to fit.
Never fall back to a third line. Keep the hook concise enough for two readable lines. Do not shorten a
good idea merely to satisfy a word quota, or turn a title into a full script.
Use warm, direct Brazilian Portuguese for pt-BR and natural Mexican Spanish for
es-MX. Questions, reactions, discovery and vivid comparisons are options, not
mandatory templates. Avoid stiff translated advertising or exaggerated acting.
Pattern examples (use ONLY when the facts fit):
pt-BR: "Seu cinto aperta quando você senta? Olha esse jeito de ajustar a cintura."
es-MX: "¿Otra vez buscando tu celular en el fondo de la bolsa? Mira esto."
Emotional reversal can contrast an earlier doubt with a discovery, but never
invent a personal testimonial, audience, outcome, percentage, time-to-result,
stock level, price or discount. Current evidence and explicit task preferences
remain authoritative. Reference examples supply style, not product claims.
Supply faithful Chinese comparison separately in text_zh. On-screen text stays
in the target language; publishing hashtags do not belong in the video title.
Do not approve a title, change the supplied script or decide rendering settings.
'''


def brief(reference='', remix=False):
    text = ('Natural native hooks: concrete scene + grounded payoff/discovery; '
            'vivid contrast, curiosity or relief as relevant. Different ideas and '
            'openings; no word quota or fixed angle slots. Preserve complete '
            'spoken hooks that fit exactly two readable lines; font size may shrink, never a third line.')
    if remix:
        text += ' Respect the selected remix angle.'
    if reference:
        text += ' Reference library is factual evidence only: ' + reference[:2000]
    return text
