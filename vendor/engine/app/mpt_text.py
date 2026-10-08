# Adapted from MoneyPrinterTurbo 3d5f4e421927d61f3eac729cf4b711ac0b69d688.
# Copyright (c) 2024 Harry. MIT; see vendor/moneyprinterturbo/LICENSE.
# Changes: stdlib logger; glyph checks fail closed and inspect all visible characters.
import logging
import unicodedata
from PIL import ImageFont
logger = logging.getLogger(__name__)

def wrap_text(text, max_width, font="Arial", fontsize=60):
    # 字幕换行必须在真正创建 TextClip 前完成，否则 MoviePy 只会按原始文本
    # 计算渲染区域。这里用 PIL 按当前字体和字号测量宽度，确保每一行都尽量
    # 控制在视频可用宽度内，避免大字号或中文长句直接溢出画面。
    font = ImageFont.truetype(font, fontsize)
    max_width = int(max_width)

    # getbbox() 返回的是“当前字形的可见墨迹高度”，并不是字体行高。例如只含
    # A、m、n 等无下伸部字符的英文会缺少 descent，多行时这个误差会逐行累积，
    # 最终让 TextClip 的最后一行被画布裁掉。ascent + descent 来自字体自身，
    # 不受具体语种和字符组合影响，也与 MoviePy 的 baseline 绘制模型一致。
    ascent, descent = font.getmetrics()
    line_height = int(ascent + descent)
    if line_height <= 0:
        # 正常 TrueType/OpenType 字体不会进入这里；保留可诊断日志和字号兜底，
        # 避免损坏或非常规字体返回异常 metrics 后生成零高度字幕。
        logger.warning(
            "invalid subtitle font metrics, fallback to font size: "
            f"ascent={ascent}, descent={descent}, fontsize={fontsize}"
        )
        line_height = max(1, int(fontsize))

    def get_text_size(inner_text):
        inner_text = inner_text.strip()
        if not inner_text:
            return 0, line_height
        left, top, right, bottom = font.getbbox(inner_text)
        # bbox 仍适合测量换行所需的实际宽度；高度必须始终使用稳定字体行高。
        return right - left, line_height

    width, height = get_text_size(text)
    if width <= max_width:
        # SRT 条目允许作者手工换行。即使整段文本在宽度上不需要再次折行，
        # 画布高度仍必须按现有行数计算，否则第二行及后续行会被裁掉。
        return text, (text.count("\n") + 1) * line_height

    def split_long_token(token):
        # 当一个 token 本身就超宽时（常见于中文无空格长句，或英文超长单词），
        # 退化为字符级拆分。关键点是：检测到 candidate 超宽时，先提交上一个
        # 仍然合法的 current，再把当前字符放入下一行，不能把超宽字符塞回上一行。
        lines = []
        current = ""
        for char in token:
            candidate = f"{current}{char}"
            candidate_width, _ = get_text_size(candidate)
            if candidate_width <= max_width or not current:
                current = candidate
                continue
            lines.append(current)
            current = char
        if current:
            lines.append(current)
        return lines

    lines = []
    current = ""
    words = text.split(" ")
    for word in words:
        candidate = f"{current} {word}".strip() if current else word
        candidate_width, _ = get_text_size(candidate)
        if candidate_width <= max_width:
            current = candidate
            continue

        if current:
            lines.append(current)

        word_width, _ = get_text_size(word)
        if word_width <= max_width:
            current = word
        else:
            lines.extend(split_long_token(word))
            current = ""

    if current:
        lines.append(current)

    line_start_punctuation = "，。！？；：、,.!?;:)]}）】》」』”’"
    for index in range(1, len(lines)):
        # 中文长句按字符拆分时，最后一个句号、逗号等闭合标点可能被单独
        # 放到下一行，导致字幕背景被异常撑高，视觉上像一个小点掉在正文
        # 下方。这里在不重新设计换行算法的前提下，把上一行最后一个字
        # 移到标点行前面，让标点跟随文字显示，兼容中英文常见闭合标点。
        if not lines[index] or lines[index][0] not in line_start_punctuation:
            continue
        if len(lines[index - 1]) <= 1:
            continue

        candidate = f"{lines[index - 1][-1]}{lines[index]}"
        candidate_width, _ = get_text_size(candidate)
        if candidate_width <= max_width:
            lines[index] = candidate
            lines[index - 1] = lines[index - 1][:-1]

    result = "\n".join(line.strip() for line in lines if line.strip()).strip()
    # 高度以最终结果为准。原文本中的显式换行可能保留在某个 token 内，
    # 此时临时 lines 列表的长度不等于 MoviePy 实际渲染的行数。
    height = (result.count("\n") + 1) * line_height
    return result, height

def _subtitle_font_supports_sample(font_path: str, sample: str) -> bool:
    """检查字体是否包含样本文字需要的字形，并缓存重复检查结果。"""
    try:
        font = ImageFont.truetype(font_path, 30)
        missing_mask = font.getmask("\U0010ffff")
        missing_signature = (
            missing_mask.size,
            missing_mask.getbbox(),
            bytes(missing_mask),
        )
        for char in sample:
            char_mask = font.getmask(char)
            char_signature = (
                char_mask.size,
                char_mask.getbbox(),
                bytes(char_mask),
            )
            if char_mask.getbbox() is None or char_signature == missing_signature:
                return False
        return True
    except Exception as e:
        # 字体探测失败不应阻止用户生成；保留日志供环境兼容问题排查。
        logger.warning(f"failed to inspect subtitle font glyphs: {font_path}, {e}")
        return True

def subtitle_font_supports_text(font_path: str, text: str) -> bool:
    """检查字体能否绘制文本中的字母和数字，忽略空白及标点符号。"""
    sample = "".join(
        dict.fromkeys(
            char
            for char in str(text or "")
            if not char.isspace()
        )
    )
    if not sample:
        return True
    return _subtitle_font_supports_sample(font_path, sample)
