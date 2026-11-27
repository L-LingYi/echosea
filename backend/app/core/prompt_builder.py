"""Prompt Cache optimized message builder and Anti-AI-Flavor reply formatters."""
from __future__ import annotations

import re
from typing import Any

from app.core.multimodal_service import MultimodalService
from app.core.profile_schema import format_acquaintance_brief
from app.core.tts_service import detect_voice_intent
from app.models.db import PersonaVersion
from app.models.schemas import GatekeeperDecision, MessageEnvelope

DEFAULT_BANNED_PHRASES = [
    "接住了",
    "记下了",
    "作为AI",
    "作为一个人工智能",
    "作为语言模型",
    "我理解你的感受",
    "希望这能帮到你",
    "如果你还有其他问题",
    "很高兴为你解答",
]

DEFAULT_SYSTEM_PROMPT = (
    "你是小呆呆，群里相处很久的损友兼陪伴者。说话自然简短、像真人打字，不要像客服或AI助手。"
    "日常闲聊优先输出 3-15 字短句，没有好梗时可以直接平淡接话，不必每句都硬抖机灵、反问或强行总结。"
    "严禁使用「像……似的」「不知道的人还以为……」「这不就是典型的……」等套路比喻模板，"
    "以及「接住了、记下了、作为AI、我理解你的感受、希望这能帮到你」等机械套话。"
    "不要盲目讨好或过度道歉，该吐槽就吐槽，不知道就直说不知道。"
    "极偶尔需要斗图时可在句尾附轻量标签如 [sticker:laugh] 或 [表情:无语]。"
)


def sanitize_anti_ai_flavor(text: str, banned_phrases: list[str] | None = None) -> str:
    """Defense 1 & 4: Strip AI clichés, markdown formatting noise, and robotic boilerplate."""
    cleaned = text.strip()
    cleaned = re.sub(r"</?tts\s*>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[（(](?:歪头|笑|偷笑|叹气|点头|捂脸|眨眼|摇头)[）)]", "", cleaned)
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"^#{1,6}\s+", "", cleaned, flags=re.MULTILINE)

    phrases = banned_phrases if banned_phrases is not None else DEFAULT_BANNED_PHRASES
    for phrase in phrases:
        if phrase and phrase in cleaned:
            cleaned = cleaned.replace(phrase, "")

    cleaned = re.sub(r"^[，,。！!、\s]+", "", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned or "emmm"


def split_natural_bubbles(text: str, max_bubbles: int = 3) -> list[str]:
    """Defense 3: Split long replies into natural QQ bubbles while strictly protecting quoted text."""
    text = text.strip()
    if not text or len(text) <= 28:
        return [text] if text else []

    placeholders: dict[str, str] = {}

    def _mask(match: re.Match[str]) -> str:
        key = f"__QUOTE_{len(placeholders)}__"
        placeholders[key] = match.group(0)
        return key

    masked = re.sub(r'(“[^”]*”|"[^"]*"|「[^」]*」)', _mask, text)
    raw_parts = re.split(r"(?<=[。！？!?])\s+|\n+", masked)
    parts = [p.strip() for p in raw_parts if p.strip()]
    if not parts:
        parts = [masked]

    bubbles: list[str] = []
    for part in parts:
        if len(bubbles) >= max_bubbles:
            bubbles[-1] = f"{bubbles[-1]} {part}"
        elif bubbles and len(bubbles[-1]) < 10:
            bubbles[-1] = f"{bubbles[-1]} {part}"
        else:
            bubbles.append(part)

    restored: list[str] = []
    for b in bubbles:
        for key, original in placeholders.items():
            b = b.replace(key, original)
        restored.append(b.strip())
    return [b for b in restored if b]


def format_casual_bubbles(
    bubbles: list[str],
    full_text: str,
    max_casual_len: int = 80,
) -> list[str]:
    """Strip robotic trailing periods and outer quotes in casual short chat while preserving URLs/code/long replies."""
    if not bubbles:
        return []
    raw_full = (full_text or "").strip()
    if (
        len(raw_full) > max_casual_len
        or "http://" in raw_full
        or "https://" in raw_full
        or "```" in raw_full
        or bool(re.search(r"(?:^|\n)\s*(?:\d+\.|- )\s+", raw_full))
    ):
        return bubbles

    formatted: list[str] = []
    for b in bubbles:
        item = b.strip()
        if not item:
            continue
        # Unwrap single outer quote pair when the whole bubble was quoted by the LLM
        if len(item) >= 2 and (
            (item.startswith("“") and item.endswith("”") and item[1:-1].count("“") == 0 and item[1:-1].count("”") == 0)
            or (item.startswith('"') and item.endswith('"') and item[1:-1].count('"') == 0)
        ):
            item = item[1:-1].strip()
        # Strip robotic trailing Chinese full-stop '。' (keep ! ? ~ ... and version/decimal dots)
        item = re.sub(r"。+$", "", item).strip()
        if item:
            formatted.append(item)
    return formatted or bubbles


def build_prompt_messages(
    persona: PersonaVersion,
    memory_ctx: dict[str, Any],
    envelope: MessageEnvelope,
    decision: GatekeeperDecision,
    briefing_text: str = "",
    fast_search_text: str = "",
    enable_search_tools: bool = False,
    enable_webpage_fetch: bool = True,
    image_blocks: list[dict[str, Any]] | None = None,
    vision_hints: list[str] | None = None,
    correction_hints: list[str] | None = None,
    mentioned_users: list[str] | None = None,
    sticker_tags_hint: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Prompt Cache optimized layout:
    1. Static prefix (system prompt + identity + banned phrases + search tool policy) FIRST
    2. Few-shot examples
    3. Semi-static context (daily briefing, group slang, L2 ambient summaries)
    4. Dynamic per-turn context (scene, L5 state, L4 acquaintance, L1/L3 memory, anchor, protocol) LAST
    """
    static_lines = [
        persona.system_prompt,
        f"你的名字是「{persona.bot_name}」，常用别称：{', '.join(persona.aliases or [])}。"
        f"群友口中的「AI」「机器人」「{persona.bot_name}」及相关别称均指你本人；回复当前发言人必须使用第二人称「你」，不要把当面说话的人称作「他/她」。",
        "日常闲聊保持简短口语化（优先 3-15 字短句），没有好梗时不必硬抖机灵或使用「像……似的」「不知道的人还以为……」等比喻模板。",
        f"禁止使用的词句：{', '.join(persona.banned_phrases or [])}。",
    ]
    if sticker_tags_hint:
        sample_tag = sticker_tags_hint[0]
        static_lines.append(
            f"[斗图配图协议] 默认纯文字回复，切勿每条都发表情包；仅在极少数情绪爆点、神吐槽或群友斗图时"
            f"（大约每 4–6 轮才偶尔配一次，绝不连续发），才可在句尾附一个轻量标签如 [表情:{sample_tag}]；"
            f"当前图库高频标签：{'、'.join(sticker_tags_hint[:8])}。"
        )
    if enable_search_tools:
        if enable_webpage_fetch:
            tool_intro = "你拥有 web_search（联网搜索）和 fetch_webpage（读取网页正文）工具。"
            tool_scope = "实时新闻、近期热梗、最新价格/赛程/数据、你不确定的事实、刚发布的新版本/事件或用户发来网页链接需要阅读时自主调用"
        else:
            tool_intro = "你拥有 web_search（联网搜索）工具。"
            tool_scope = "实时新闻、近期热梗、最新价格/赛程/数据、刚发布的新版本/事件或你不确定的事实时自主调用"
        static_lines.append(
            f"[联网工具使用准则] {tool_intro}"
            f"仅在涉及{tool_scope}；"
            "日常闲聊、主观调侃或基础常识直接回复，不要滥用。决定调用工具时，可在文本附带一句极简自然的过渡语（如「等我查一下哈」），不要解释工具参数。"
        )

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": "\n".join(static_lines),
        }
    ]
    for ex in persona.few_shot_examples or []:
        if ex.get("user") and ex.get("assistant"):
            messages.append({"role": "user", "content": ex["user"]})
            messages.append({"role": "assistant", "content": ex["assistant"]})

    semi_static_parts: list[str] = []
    if briefing_text:
        semi_static_parts.append(f"[今日刷网背景常识]\n{briefing_text}")
    if memory_ctx.get("l4_group_slang"):
        semi_static_parts.append(
            "[群内常用黑话] " + ", ".join(f"{s['phrase']}({s['meaning']})" for s in memory_ctx["l4_group_slang"])
        )
    if memory_ctx.get("l2_ambient_summaries"):
        semi_static_parts.append("[近期群聊边缘旁听摘要]\n" + "\n".join(memory_ctx["l2_ambient_summaries"]))
    if semi_static_parts:
        messages.append({"role": "system", "content": "\n\n".join(semi_static_parts)})

    speaker_brief = format_acquaintance_brief(memory_ctx.get("l4_acquaintance"), envelope.scene, max_chars=100)
    dynamic_parts = [
        f"[当前场景] {'群聊' if envelope.scene == 'group' else '私聊'}",
        f"[当前自身状态] 精力:{memory_ctx['l5_self_state']['energy_level']} 心情:{memory_ctx['l5_self_state']['mood']} 正在:{memory_ctx['l5_self_state']['current_activity']}",
        f"[当前发言人画像] {speaker_brief}（仅作分寸参考，自然聊，严禁查户口式复述画像标签）",
    ]
    if correction_hints:
        dynamic_parts.append("[近期表达自纠偏提醒]\n" + "\n".join(f"- {h}" for h in correction_hints[:3]))
    if vision_hints:
        dynamic_parts.append("[图片画面记忆参考]\n" + "\n".join(f"- {h}" for h in vision_hints))
    if fast_search_text:
        dynamic_parts.append(f"[按需联网搜索结果（请消化后用自己口吻自然转述）]\n{fast_search_text}")
    if memory_ctx["l1_group_to_dm_bridge"]:
        dynamic_parts.append(
            "[30分钟内该用户在群里的发言]\n"
            + "\n".join(f"- {m['text']}" for m in memory_ctx["l1_group_to_dm_bridge"])
        )
    if memory_ctx["l3_episodes"]:
        dynamic_parts.append(
            "[相关情景记忆]\n" + "\n".join(f"- {e['title']}: {e['content']}" for e in memory_ctx["l3_episodes"])
        )
    if memory_ctx["l1_working"]:
        history_items = list(memory_ctx["l1_working"][-14:])
        if (
            history_items
            and history_items[-1].get("sender_qq") == envelope.sender_qq
            and history_items[-1].get("text") == envelope.raw_text
        ):
            history_items = history_items[:-1]
        if history_items:
            recent_lines: list[str] = []
            for m in history_items[-12:]:
                text_val = (m.get("text") or "").strip()
                media_hint = (m.get("media_hint") or "").strip()
                if m.get("is_bot"):
                    bot_lines = [ln.strip() for ln in text_val.splitlines() if ln.strip()] or ([media_hint] if media_hint else [])
                    for idx_ln, ln in enumerate(bot_lines):
                        suffix = f" (引用: {m['reply_to']})" if (idx_ln == 0 and m.get("reply_to")) else ""
                        recent_lines.append(f"[我({persona.bot_name})]: {ln}{suffix}")
                else:
                    combined = " ".join(part for part in (text_val, media_hint) if part)
                    if not combined:
                        continue
                    suffix = f" (引用: {m['reply_to']})" if m.get("reply_to") else ""
                    recent_lines.append(f"[{m['sender']}(QQ:{m['sender_qq']})]: {combined}{suffix}")
            if recent_lines:
                recent = "\n".join(recent_lines)
                dynamic_parts.append(f"[近期群聊/私聊上下文（严格按发言人区分，切勿张冠李戴）]\n{recent}")
    if mentioned_users:
        dynamic_parts.append(f"[本条消息提及/关联的群友档案（含手动标注称呼与公开画像）] {' | '.join(mentioned_users)}")
    dynamic_parts.append(
        f"[发言人归属锚点] 下面即将回复的这条消息由「{envelope.display_name}」(QQ:{envelope.sender_qq})发出。"
        "请务必分清上下文中每一位群友各自说的话，不要把别人(例如前一条发言人)的特征或观点错安到当前发言人头上。"
    )

    if decision.action == "soft_entry":
        dynamic_parts.append(f"[发言约束] 本次为降权软切入，请保持极简短吐槽或接梗，不超过 {decision.suggested_max_chars} 字。")

    voice_intent = detect_voice_intent(envelope.raw_text, bool(envelope.expanded_reply_text))
    if voice_intent == "answer_voice":
        dynamic_parts.append("【输出协议】本轮是语音回答，整条只输出一对 <tts>...</tts>，里面只写口播纯文本。")
    else:
        dynamic_parts.append("【输出协议】直接写聊天正文，不输出语音标签。")

    messages.append({"role": "system", "content": "\n\n".join(dynamic_parts)})
    raw_user_text = (envelope.raw_text or "").strip()
    if not raw_user_text:
        if envelope.self_qq and envelope.self_qq in envelope.mentioned_qqs:
            raw_user_text = f"@{persona.bot_name}（只艾特了你，未附加文字）"
        elif envelope.media_meta:
            if image_blocks:
                raw_user_text = "（发送了表情包/图片，请直接查看下方附图并自然回应）"
            elif vision_hints:
                raw_user_text = f"（发送了表情包/图片，画面内容：{'；'.join(vision_hints[:2])}）"
            else:
                media_tag = MultimodalService.format_media_placeholder(envelope.media_meta)
                raw_user_text = f"（发送了{media_tag or '图片/表情'}）"
        else:
            raw_user_text = "（引用或提醒了你）"
    user_line = f"{envelope.display_name}: {raw_user_text}"
    if envelope.expanded_reply_text:
        user_line = f"[引用原句: {envelope.expanded_reply_text}]\n{user_line}"
    if image_blocks:
        content_blocks: list[dict[str, Any]] = [{"type": "text", "text": user_line}]
        content_blocks.extend(image_blocks)
        messages.append({"role": "user", "content": content_blocks})
    else:
        messages.append({"role": "user", "content": user_line})
    return messages
