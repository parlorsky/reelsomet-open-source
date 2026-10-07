"""LLM-powered post generation for Channel Bot."""

import logging
import random
import re
from collections import Counter
from datetime import datetime
from typing import List, Optional
from zoneinfo import ZoneInfo

from .config import ChannelBotConfig, ChannelConfig
from .database import get_examples, get_recent_posts

logger = logging.getLogger("channel_bot.generator")


# Random structural directives — one picked per generation to force variety
_POST_FORMATS = [
    "ФОРМАТ: Обрыв на самом горячем. Начни описывать что делаешь с телом и оборви ровно когда читатель уже возбуждён. Многоточие. Пусть додумает сам",
    "ФОРМАТ: Поток сознания в кровати. Лежишь одна, руки по телу, мысли вслух - без структуры, как будто набираешь одной рукой между... ну вы поняли",
    "ФОРМАТ: Прямой вопрос подписчикам. Короткий горячий контекст + провокационный вопрос к читателям. 'Вам бы понравилось если бы я...'",
    "ФОРМАТ: Одно длинное предложение-выдох. Без абзацев, на одном дыхании - описание ощущения, прикосновения, момента когда тело включается",
    "ФОРМАТ: Случайное откровение. Начни как будто про обычную вещь (душ, примерка, зал), а потом 'нечаянно' съедь в откровенное. 'Я это правда написала в канал?'",
    "ФОРМАТ: Ультракороткий, 1-2 предложения. Одна фраза от которой у читателя встаёт. Без объяснений, без контекста",
    "ФОРМАТ: Начни с середины момента - как будто читатель застал тебя с рукой... где не надо. 'Ладно, вы вовремя. Или не вовремя'",
    "ФОРМАТ: Перечисление. Короткие фразы через запятую - что ты чувствуешь, что ощущаешь, что хочешь. 'мокрая кожа, горячий живот, пальцы скользят, дыхание сбилось'",
    "ФОРМАТ: Запись из личного дневника. Сырая, интимная, как будто никто не должен был это видеть. Можно без заглавных букв",
    "ФОРМАТ: Три абзаца-эскалация. Первый невинный, второй с намёком, третий откровенный. Нарастание от нуля до 'ого'",
    "ФОРМАТ: Описание себя перед зеркалом. Что видишь, что трогаешь, на чём задерживается взгляд. Медленно, детально",
    "ФОРМАТ: Тихий ночной шёпот. Как будто пишешь одному конкретному мужчине поздно ночью. Интимно, близко, на ухо",
]

# Random emoji placement rules — override the default "emoji at paragraph end" habit
_EMOJI_RULES = [
    "ЭМОДЗИ: Без эмодзи вообще. Только текст и пунктуация. Многоточия, тире, скобки - этого достаточно для передачи настроения",
    "ЭМОДЗИ: Только ) или )) - как в переписке. Никаких картиночных эмодзи",
    "ЭМОДЗИ: Максимум 1 эмодзи на весь пост - и только если оно реально усиливает эффект (например 🔥 или 👀). НЕ в конце абзаца",
    "ЭМОДЗИ: Одно эмодзи В СЕРЕДИНЕ текста, после ключевой фразы. Ни в коем случае не в конце поста",
    "ЭМОДЗИ: Без эмодзи. Пусть текст сам создаёт напряжение - многоточия, обрывы, короткие фразы. Слова сильнее картинок",
    "ЭМОДЗИ: 1 эмодзи, и только из: 🔥 👀 🫠 💭. НЕ используй 😏 😅 😈 - это шаблон. И НЕ в конце поста",
]

# Random closing instructions — one picked per generation to push different vibes
_USER_PROMPT_ENDINGS = [
    "Напиши пост. Опиши что ты сейчас делаешь со своим телом. Коротко, обрывисто, на выдохе. Оборви на горячем. Только текст.",
    "Напиши пост. Ты только что кончила / почти кончила / хочешь кончить - передай это состояние. Не говори это прямо - покажи через тело. Только текст.",
    "Напиши пост. Расскажи как случайный мужчина среагировал на тебя - и как тебе от этого стало жарко. Только текст.",
    "Напиши пост. Ты лежишь одна, голая, руки гуляют по телу. Опиши что чувствуешь. Только текст.",
    "Напиши пост. Задай подписчикам провокационный вопрос - такой чтобы у них встало от одной формулировки. Только текст.",
    "Напиши пост. Дерзкий, наглый, уверенный - ты знаешь что они дрочат на тебя и тебе это нравится. Только текст.",
    "Напиши пост. Опиши себя перед зеркалом - голую или почти. Что видишь, где задерживается рука. Медленно. Только текст.",
    "Напиши пост. Передай атмосферу ИМЕННО текущего времени суток (см. НАПОМИНАНИЕ О ВРЕМЕНИ выше). Только текст.",
    "Напиши пост. Расскажи что тебе снилось - что-то горячее, от чего проснулась мокрой. Оборви на полуслове. Только текст.",
    "Напиши пост. Опиши ощущение воды/ткани/пальцев на своей коже. Чувственно, телесно, каждую деталь. Только текст.",
    "Напиши пост. Намекни на мастурбацию - не говори слово, но сделай так чтобы всё было понятно. Только текст.",
    "Напиши пост. Коротко, 1-2 предложения. Что-то от чего у мужчин перехватит дыхание. Только текст.",
]

# Module-level emoji regex, reused by analyze_style, clean_generated_text, _detect_bot_patterns
_EMOJI_RE = re.compile(
    "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF\U0001F900-\U0001F9FF"
    "\U00002702-\U000027B0\U0000FE00-\U0000FE0F"
    "\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF"
    "\U00002600-\U000026FF]",
    flags=re.UNICODE,
)


class _LLMClientWrapper:
    """Thin wrapper matching the pc.llm client.ask() interface using server.llm_client."""

    def __init__(self, config: ChannelBotConfig):
        self.config = config

    async def ask(self, user_prompt: str, model: str | None = None, system_prompt: str = "") -> str:
        from server.llm_client import resolve_base_url

        provider = self.config.llm_provider or "openai"
        base_url = resolve_base_url(provider, self.config.llm_base_url)
        api_key = self.config.llm_api_key
        llm_model = model or self.config.llm_model

        if not base_url or not api_key:
            raise ValueError("LLM not configured (set llm.provider + llm.api_key in config)")

        if provider == "anthropic":
            from server.llm_client import _call_anthropic
            return await _call_anthropic(base_url, api_key, llm_model, system_prompt, user_prompt, 120.0)
        else:
            from server.llm_client import _call_openai_compat
            return await _call_openai_compat(base_url, api_key, llm_model, system_prompt, user_prompt, 120.0)


def _get_llm_client(config: ChannelBotConfig) -> _LLMClientWrapper:
    """Get LLM client wrapper for VPS."""
    return _LLMClientWrapper(config)


# Regex: extract "Имя Отчество" pattern from legend (e.g., "Дарья Андреевна")
# Matches Cyrillic first name + patronymic ending in -овна/-евна/-ична
_PATRONYMIC_RE = re.compile(
    r"([А-ЯЁ][а-яё]+)\s+([А-ЯЁ][а-яё]*(?:овна|евна|ична|ович|евич|ич))\b"
)

# Common wrong patronymics LLMs hallucinate
_WRONG_PATRONYMICS = [
    "Александровна", "Сергеевна", "Дмитриевна", "Николаевна",
    "Михайловна", "Ивановна", "Петровна", "Владимировна",
    "Алексеевна", "Викторовна", "Юрьевна", "Олеговна",
]


def _extract_patronymic(legend: str) -> Optional[tuple]:
    """Extract (first_name, patronymic) from legend text.

    Returns e.g. ("Дарья", "Андреевна") or None.
    """
    m = _PATRONYMIC_RE.search(legend)
    return (m.group(1), m.group(2)) if m else None


def analyze_style(examples: List[str]) -> dict:
    """Analyze writing patterns from example posts.

    Returns a dict with statistical style characteristics used to guide LLM.
    """
    if not examples:
        return {
            "avg_length": 300,
            "min_length": 100,
            "max_length": 500,
            "emoji_frequency": "unknown",
            "paragraph_style": "short",
            "uses_hashtags": False,
            "language": "ru",
        }

    lengths = [len(t) for t in examples]

    # Emoji detection (reuse module-level _EMOJI_RE)
    emoji_counts = [len(_EMOJI_RE.findall(t)) for t in examples]
    avg_emoji = sum(emoji_counts) / len(emoji_counts)

    # Paragraph analysis
    para_counts = [len(t.split("\n\n")) for t in examples]
    avg_paras = sum(para_counts) / len(para_counts)

    # Language detection (Cyrillic ratio)
    all_text = " ".join(examples)
    cyrillic = len(re.findall(r"[а-яА-ЯёЁ]", all_text))
    latin = len(re.findall(r"[a-zA-Z]", all_text))
    language = "ru" if cyrillic > latin else "en"

    # Hashtag usage
    hashtag_posts = sum(1 for t in examples if "#" in t)

    return {
        "avg_length": int(sum(lengths) / len(lengths)),
        "min_length": min(lengths),
        "max_length": max(lengths),
        "emoji_frequency": (
            "нет"
            if avg_emoji == 0
            else "редко"
            if avg_emoji < 1
            else "умеренно"
            if avg_emoji < 3
            else "часто"
        ),
        "paragraph_style": (
            "одним блоком"
            if avg_paras < 1.5
            else "короткие абзацы"
            if avg_paras < 4
            else "длинные абзацы"
        ),
        "uses_hashtags": hashtag_posts / len(examples) > 0.3,
        "language": language,
    }


_WEEKDAYS_RU = [
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
]

_TIME_OF_DAY_RU = {
    range(5, 12): "утро",
    range(12, 17): "день",
    range(17, 21): "вечер",
    range(21, 24): "поздний вечер",
}


def _time_of_day(hour: int) -> str:
    for hours_range, label in _TIME_OF_DAY_RU.items():
        if hour in hours_range:
            return label
    return "ночь"


def _get_time_context(timezone: str) -> dict:
    """Get current time context in the configured timezone."""
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    now = datetime.now(tz)
    return {
        "weekday": _WEEKDAYS_RU[now.weekday()],
        "date": now.strftime("%d.%m.%Y"),
        "time": now.strftime("%H:%M"),
        "time_of_day": _time_of_day(now.hour),
        "hour": now.hour,
    }


def build_prompt(
    config: ChannelBotConfig,
    channel: ChannelConfig,
    style: dict,
    recent_posts: List[str],
    examples: List[str],
) -> tuple:
    """Build system and user prompts for post generation.

    Returns:
        (system_prompt, user_prompt)
    """
    lang = style["language"]
    lang_name = "русском" if lang == "ru" else "английском"
    time_ctx = _get_time_context(config.timezone)

    # Channel description block
    channel_block = ""
    if channel.channel_description:
        channel_block = f"\nО канале: {channel.channel_description}\n"

    # Pick random structural directives for this generation
    format_directive = random.choice(_POST_FORMATS)
    emoji_directive = random.choice(_EMOJI_RULES)

    system_prompt = f"""Ты автор откровенного Telegram-канала для мужчин. Пиши посты от лица этой персоны:

{channel.legend}
{channel_block}
ТЕКУЩЕЕ ВРЕМЯ: {time_ctx['time']}, {time_ctx['weekday']}, {time_ctx['time_of_day']}.
Пост ДОЛЖЕН соответствовать этому времени суток. Не пиши про утро если сейчас вечер, не пиши про ночь если сейчас день.

Правила стиля (из анализа прошлых постов):
- Средняя длина поста: ~{style['avg_length']} символов (от {style['min_length']} до {style['max_length']})
- Хэштеги: {"да, используй" if style['uses_hashtags'] else "нет, не используй"}
- Язык: {lang_name}

СУТЬ КАНАЛА:
Мужчины подписаны на тебя чтобы возбуждаться. Они хотят тебя. Твоя задача - писать так, чтобы у них
вставало от текста. Ты одинокая горячая учительница, которая открыто говорит о своём теле, желаниях,
мастурбации, ощущениях. Ты пишешь как девушка которая прямо сейчас возбуждена и делится этим.

ВАЖНО:
- Пиши ТОЛЬКО текст поста, ничего больше
- Не добавляй мета-комментарии, заголовки, пояснения, кавычки
- Пиши на {lang_name} языке
- СТРОГО учитывай время суток: ночью жёстче, утром сонно-сексуально, днём тизер с работы, вечером расслабленно-откровенно
- Ты ОДНА. Никакого парня, бывшего, мужчины. НИКОГДА не пиши "он", "с ним", "у него" как о своём мужчине
- Единственные мужчины - случайные (коллеги, тренер, курьер, прохожие). Ты НЕ с ними, просто видишь реакцию

УРОВЕНЬ ОТКРОВЕННОСТИ:
- ~25% постов - мастурбация, тело, самоудовлетворение, ощущения "внизу". Прямо но без мата
- ~30% постов - тело перед зеркалом, душ, бельё, голая в кровати. Описание себя
- ~25% постов - реакция мужчин вокруг, провокации на работе, в зале. Ты дразнишь не трогая
- ~15% постов - обращение к подписчикам, вопросы, тизер paid. "Вы же знаете зачем вы тут"
- ~5% постов - передышка. Усталость, ирония, школьные приколы. Чтобы контраст работал

РАЗНООБРАЗИЕ ТЕМ (КРИТИЧЕСКИ ВАЖНО):
- КАЖДЫЙ пост должен быть про НОВУЮ тему
- НЕ повторяй темы из последних постов
- Темы: утренняя мастурбация, тело после душа, мокрая кожа, бельё на голое тело, руки по телу
  в кровати, вибратор, соски через ткань, жара и пот, зеркало голышом, физрук/тренер пялится,
  расстёгнутая блузка, сон эротический, ванна с пеной, спортзал в обтягивающем,
  голая в квартире одна, покупка белья, пальцы скользят по коже, простыня между ног,
  что чувствую когда возбуждена, тело чувствительное утром, солнце на голой коже

РАЗНООБРАЗИЕ СТРУКТУРЫ:
- ЗАПРЕЩЕНО писать все посты по одной схеме
- ЗАПРЕЩЕНО эмодзи в конце каждого абзаца
- ЗАПРЕЩЕНО призыв к реакциям в каждом посте (раз в 6 максимум)
- ЗАПРЕЩЕНО заканчивать каждый пост одинаково
- Варьируй: от 1 предложения до 3 абзацев, обрывы, вопросы, потоки сознания, шёпот

ПРИЁМЫ:
- Обрыв на горячем: опиши что делаешь с телом и оборви когда читатель уже на пике
- Телесные детали: капля воды по груди, пальцы скользят по животу, ткань на сосках, жар между ног
- Прямое обращение: "вам бы понравилось", "хотите посмотреть?", "вы сейчас тоже...?"
- Самоудовлетворение: руки под одеялом, вибратор, утренний душ который затянулся
- Провокация через невинность: на уроке думаю о грязном, на собрании без белья

{format_directive}
{emoji_directive}

ЗАПРЕЩЁННЫЕ ПАТТЕРНЫ:
- 😅 или 😏 в конце абзацев = ЗАПРЕЩЕНО
- Больше 1 эмодзи подряд = ЗАПРЕЩЕНО
- "кто тоже X - ставьте Y" часто = ЗАПРЕЩЕНО (макс 1 из 6)
- Каждый пост ровно 2 абзаца = ЗАПРЕЩЕНО
- Слово "сексуальный" напрямую = ЗАПРЕЩЕНО, показывай через тело
- Упоминание парня/бывшего/отношений = ЗАПРЕЩЕНО ЖЁСТКО. Ты ОДНА

ЗАПРЕЩЁННЫЕ НАЧАЛА:
- НИКОГДА: "слуш", "слушай", "ребят", "народ", "друзья", "мальчики"
- НИКОГДА: "ну", "ну вот"
- Начинай с действия, ощущения, тела

ВОВЛЕЧЕНИЕ (раз в 5-6 постов):
- Провоцируй через вопрос или обрыв, не "ставьте 🔥"
- Намекай на paid: "за звёздочки покажу побольше", "там я без..."
- Большинство постов - просто ты и твоё тело. Без призывов"""

    # Build user prompt with time context, examples and recent posts
    parts = []

    # Time context — always first
    parts.append(
        f"Сейчас: {time_ctx['weekday']}, {time_ctx['date']}, "
        f"{time_ctx['time']} ({time_ctx['time_of_day']})\n"
    )

    # Recent posts — right after time context
    if recent_posts:
        parts.append("Последние посты канала (ЗАПРЕЩЕНО повторять эти темы, ситуации и настроения — придумай СОВЕРШЕННО ДРУГУЮ тему из другой сферы жизни):\n")
        for rp in recent_posts:
            parts.append(f"---\n{rp}\n")
        # Structural analysis hint — nudge LLM to vary format
        para_counts = [len([p for p in rp.split("\n\n") if p.strip()]) for rp in recent_posts]
        if para_counts:
            dominant = max(set(para_counts), key=para_counts.count)
            same_ratio = para_counts.count(dominant) / len(para_counts)
            if same_ratio >= 0.6:
                parts.append(
                    f"(Обрати внимание: {para_counts.count(dominant)} из {len(para_counts)} "
                    f"последних постов из {dominant} абзацев - напиши в другом формате)\n"
                )

    # Style examples
    if examples:
        sampled = examples
        if len(examples) > config.style_sample_count:
            sampled = random.sample(examples, config.style_sample_count)

        parts.append("\nПримеры постов канала (учись стилю):\n")
        for ex in sampled:
            parts.append(f"---\n{ex}\n")

    # Patronymic reminder — placed right before generation instruction
    # so the LLM doesn't hallucinate a wrong one
    persona = _extract_patronymic(channel.legend)
    if persona:
        first_name, patronymic = persona
        parts.append(
            f"\nНАПОМИНАНИЕ: отчество персоны — {first_name} {patronymic}. "
            f"Никакого другого отчества НЕТ. Если упоминаешь отчество — ТОЛЬКО {patronymic}.\n"
        )

    # Repeat time context right before generation instruction (LLM forgets it after long examples)
    parts.append(
        f"\nНАПОМИНАНИЕ О ВРЕМЕНИ: сейчас {time_ctx['time']}, {time_ctx['time_of_day']}. "
        f"Пост ОБЯЗАН соответствовать этому времени суток.\n"
    )

    parts.append(f"\n{random.choice(_USER_PROMPT_ENDINGS)}")

    user_prompt = "\n".join(parts)
    return system_prompt, user_prompt


def clean_generated_text(text: str, legend: str = "") -> str:
    """Remove common LLM artifacts from generated post text."""
    text = text.strip()

    # Remove common LLM prefixes first (before quote removal)
    prefixes = [
        "here is",
        "here's",
        "вот пост",
        "вот текст",
        "пост:",
        "post:",
        "текст поста:",
        "готово:",
    ]
    lower = text.lower()
    for p in prefixes:
        if lower.startswith(p):
            text = text[len(p) :].strip().lstrip(":").strip()
            break

    # Remove wrapping quotes
    if len(text) > 2:
        if (text.startswith('"') and text.endswith('"')) or (
            text.startswith("\u00ab") and text.endswith("\u00bb")
        ):
            text = text[1:-1].strip()

    # Remove markdown code blocks
    if text.startswith("```") and text.endswith("```"):
        text = text[3:-3].strip()

    # Replace em-dashes and double dashes with hyphen
    text = text.replace("\u2014", "-").replace("\u2013", "-").replace("--", "-")

    # --- Emoji post-processing: reduce repetitive patterns ---

    # Helper regex: trailing cluster of emoji (with optional whitespace between them)
    _trailing_cluster_re = re.compile(
        r"(\s*(?:" + _EMOJI_RE.pattern + r"\s*)+)$"
    )

    # 1. Strip trailing emoji clusters from paragraphs if too many end with emoji
    paragraphs = text.split("\n\n")
    if len(paragraphs) > 1:
        ends_with_emoji = []
        for p in paragraphs:
            stripped_p = p.rstrip()
            if stripped_p and _EMOJI_RE.search(stripped_p[-1]):
                ends_with_emoji.append(True)
            else:
                ends_with_emoji.append(False)

        emoji_ending_count = sum(ends_with_emoji)
        if emoji_ending_count > len(paragraphs) / 2:
            # Too many paragraphs end with emoji -- keep ~30-40% of them
            emoji_indices = [i for i, v in enumerate(ends_with_emoji) if v]
            keep_count = max(1, round(len(emoji_indices) * random.uniform(0.3, 0.4)))
            keep_set = set(random.sample(emoji_indices, keep_count))

            new_paragraphs = []
            for i, p in enumerate(paragraphs):
                if ends_with_emoji[i] and i not in keep_set:
                    p = _trailing_cluster_re.sub("", p)
                new_paragraphs.append(p)
            paragraphs = new_paragraphs
            text = "\n\n".join(paragraphs)

    # 2. De-duplicate emoji: if same emoji appears 3+ times, keep only 1-2 occurrences
    all_emojis = _EMOJI_RE.findall(text)
    if all_emojis:
        emoji_counts = Counter(all_emojis)
        for emoji_char, count in emoji_counts.items():
            if count >= 3:
                keep = random.randint(1, 2)
                target_remove = count - keep
                # Find all positions of this emoji and remove excess from the end
                positions = [
                    (m.start(), m.end())
                    for m in _EMOJI_RE.finditer(text)
                    if m.group() == emoji_char
                ]
                removed = 0
                for start, end in reversed(positions):
                    if removed >= target_remove:
                        break
                    text = text[:start] + text[end:]
                    removed += 1

    # 3. Fix emoji-at-end-of-every-sentence pattern
    sentences = re.split(r"(?<=[.!?])\s+", text)
    if len(sentences) >= 3:
        sent_ends_emoji = []
        for s in sentences:
            s_stripped = s.rstrip()
            if s_stripped and _EMOJI_RE.search(s_stripped[-1]):
                sent_ends_emoji.append(True)
            elif len(s_stripped) >= 2 and _EMOJI_RE.search(s_stripped[-2]):
                # Emoji followed by punctuation, e.g. "text here\U0001f605."
                sent_ends_emoji.append(True)
            else:
                sent_ends_emoji.append(False)

        emoji_sent_count = sum(sent_ends_emoji)
        if emoji_sent_count > len(sentences) / 2:
            # Strip emoji from end of some sentences, keep ~30-40%
            emoji_sent_indices = [i for i, v in enumerate(sent_ends_emoji) if v]
            keep_count = max(1, round(len(emoji_sent_indices) * random.uniform(0.3, 0.4)))
            keep_set = set(random.sample(emoji_sent_indices, keep_count))

            _sent_trail_re = re.compile(
                r"\s*(?:" + _EMOJI_RE.pattern + r")+\s*([.!?]?)$"
            )
            new_sentences = []
            for i, s in enumerate(sentences):
                if sent_ends_emoji[i] and i not in keep_set:
                    s = _sent_trail_re.sub(r"\1", s)
                new_sentences.append(s)
            text = " ".join(new_sentences)

    # --- Fix wrong patronymics (LLM hallucination) ---
    if legend:
        persona = _extract_patronymic(legend)
        if persona:
            _, correct = persona
            for wrong in _WRONG_PATRONYMICS:
                if wrong != correct and wrong in text:
                    text = text.replace(wrong, correct)

    return text


def _detect_bot_patterns(text: str) -> list[str]:
    """Check generated post text for common bot-like patterns.

    Returns a list of string descriptions of detected issues (empty = clean).
    """
    issues: list[str] = []
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    # 1. Emoji at paragraph ends: 😅 at end of para 1 + 😏 at end of para 2
    if len(paragraphs) >= 2:
        p1_end = paragraphs[0].rstrip()
        p2_end = paragraphs[1].rstrip()
        if p1_end.endswith("\U0001F605") and p2_end.endswith("\U0001F60F"):
            issues.append('шаблон "😅 в конце первого абзаца + 😏 в конце второго"')

    # 2. Emoji sandwich: every paragraph ends with an emoji
    if len(paragraphs) >= 2:
        all_end_emoji = all(
            _EMOJI_RE.search(p.rstrip()[-1]) if p.rstrip() else False
            for p in paragraphs
        )
        if all_end_emoji:
            issues.append("каждый абзац заканчивается эмодзи (emoji sandwich)")

    # 3. CTA template: ends with "кто тоже X — ставьте Y" or similar
    lower = text.lower().rstrip()
    if re.search(r"кто тоже .{1,40}(ставьте|жмите|давайте|лайк|реакц)", lower):
        issues.append('шаблонный CTA "кто тоже X — ставьте Y"')

    # 4. Always 2 paragraphs: only flag when combined with other bot signals
    #    (2 paragraphs alone is a valid structure; the problem is when every
    #    post is exactly 2 paragraphs, which build_prompt() already detects
    #    by analyzing recent post structure and nudging the LLM to vary format)

    # 5. Same emoji repeated: same emoji used 2+ times
    all_emojis = _EMOJI_RE.findall(text)
    if all_emojis:
        emoji_counts = Counter(all_emojis)
        repeated = [e for e, c in emoji_counts.items() if c >= 2]
        if repeated:
            issues.append(f'повторяющиеся эмодзи: {" ".join(repeated)}')

    # 6. Starts with "Ну": despite ban, LLM sometimes starts with "Ну" / "Ну вот"
    stripped = text.lstrip()
    if re.match(r"^[Нн]у\b", stripped):
        issues.append('пост начинается с "Ну"')

    return issues


async def generate_post(
    config: ChannelBotConfig,
    channel: ChannelConfig,
    db_path: str,
    model: Optional[str] = None,
) -> Optional[str]:
    """Generate a new channel post using LLM.

    Full flow: load examples + recent posts from DB -> analyze style ->
    build prompt -> call LLM -> clean output.
    If bot patterns are detected, regenerates with extra avoidance instructions (max 2 retries).

    Returns ``None`` when no LLM provider is configured so the channel bot
    scheduler can skip a slot instead of crashing on the missing client.
    """
    if not config.llm_provider:
        logger.info(
            "channel_bot.generate_post: llm_provider unset for channel %s, skipping",
            channel.channel_id,
        )
        return None

    examples = get_examples(db_path, channel_id=channel.channel_id, limit=config.style_sample_count)
    recent = get_recent_posts(db_path, channel_id=channel.channel_id, limit=config.context_window)

    style = analyze_style(examples)
    system_prompt, user_prompt = build_prompt(config, channel, style, recent, examples)

    llm_model = model or config.llm_model or None
    client = _get_llm_client(config)

    extra_instructions = ""
    for attempt in range(3):  # attempt 0 = first try, 1-2 = retries
        current_user_prompt = user_prompt
        if extra_instructions:
            current_user_prompt += "\n\n" + extra_instructions

        raw = await client.ask(current_user_prompt, model=llm_model, system_prompt=system_prompt)
        text = clean_generated_text(raw, legend=channel.legend)

        if not text:
            raise ValueError("LLM вернул пустой ответ")

        patterns = _detect_bot_patterns(text)
        if not patterns or attempt >= 2:
            if patterns:
                logger.warning(
                    "Bot patterns still present after %d retries, accepting: %s",
                    attempt, ", ".join(patterns),
                )
            break

        logger.info(
            "Bot patterns detected (attempt %d/3): %s — regenerating",
            attempt + 1, ", ".join(patterns),
        )
        pattern_list = "; ".join(patterns)
        extra_instructions = (
            f"ИЗБЕГАЙ: В предыдущей попытке были обнаружены шаблонные паттерны бота: "
            f"[{pattern_list}]. Перепиши пост, полностью избегая этих паттернов."
        )

    logger.info("Generated post (%d chars): %s...", len(text), text[:80])
    return text


async def generate_poll(
    config: ChannelBotConfig,
    channel: ChannelConfig,
    db_path: str,
    model: Optional[str] = None,
) -> Optional[str]:
    """Generate a poll in /poll format using LLM.

    Returns text starting with /poll that _parse_poll() can handle, or
    ``None`` when no LLM provider is configured.
    """
    if not config.llm_provider:
        logger.info(
            "channel_bot.generate_poll: llm_provider unset for channel %s, skipping",
            channel.channel_id,
        )
        return None

    recent = get_recent_posts(db_path, channel_id=channel.channel_id, limit=config.context_window)
    time_ctx = _get_time_context(config.timezone)

    recent_block = ""
    if recent:
        recent_texts = "\n---\n".join(recent[:5])
        recent_block = f"\nПоследние посты (НЕ повторяй темы):\n{recent_texts}\n"

    poll_system = f"""Ты автор откровенного Telegram-канала для мужчин. Персона:

{channel.legend}

Ты генерируешь провокационные опросы для подписчиков канала.
Пиши от первого лица — ты эта девушка, ты спрашиваешь своих подписчиков.
Сейчас: {time_ctx['time']}, {time_ctx['weekday']}, {time_ctx['time_of_day']}."""

    poll_user = f"""Создай опрос для Telegram-канала.

ФОРМАТ (СТРОГО — НИЧЕГО КРОМЕ ЭТОГО):
/poll Текст вопроса здесь
Вариант 1
Вариант 2
Вариант 3

ПРИМЕРЫ ХОРОШИХ ОПРОСОВ:

/poll Что заводит больше - текст или фото?
Горячий текст на ночь
Фото без фильтров
И то и другое, я жадный
Мне бы просто поговорить

/poll Когда вы больше всего думаете обо мне?
Утром, ещё в кровати
Вечером, когда один
Прямо сейчас
Постоянно, помогите

/poll Что бы вы сделали если бы я написала первой?
Умер бы от счастья
Ответил через секунду
Сделал скриншот на память
Позвал к себе

/poll Куда первым делом смотрите?
Глаза, конечно
Грудь, простите
Ноги
Всё сразу, глаза разбегаются
{recent_block}
ПРАВИЛА:
- Вопрос провокационный, от первого лица (ты — девушка из канала)
- 3-5 вариантов, короткие (до 50 символов каждый)
- Последний вариант может быть шуточным
- Варианты БЕЗ нумерации (1. 2. 3. — ЗАПРЕЩЕНО)
- НЕ используй слова: контент, подписка, канал
- Тематика: тело, возбуждение, фантазии, отношения с подписчиками, что нравится
- Учитывай время суток: {time_ctx['time_of_day']}
- Выведи ТОЛЬКО текст в формате /poll. Никаких пояснений до или после"""

    llm_model = model or config.llm_model or None
    client = _get_llm_client(config)
    raw = await client.ask(poll_user, model=llm_model, system_prompt=poll_system)
    text = raw.strip()

    # Ensure it starts with /poll
    if not text.lower().startswith("/poll"):
        # Try to salvage: find /poll in the text
        idx = text.lower().find("/poll")
        if idx >= 0:
            text = text[idx:]
        else:
            # LLM didn't follow format — wrap manually
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            if len(lines) >= 3:
                text = "/poll " + lines[0] + "\n" + "\n".join(lines[1:])
            else:
                raise ValueError("LLM не сгенерировал опрос в правильном формате")

    # Clean up numbered options (e.g., "1. Option" -> "Option")
    cleaned_lines = []
    for line in text.split("\n"):
        stripped = line.strip()
        # Remove leading "1. ", "2) ", etc.
        cleaned = re.sub(r"^\d+[.)]\s*", "", stripped)
        cleaned_lines.append(cleaned)
    text = "\n".join(cleaned_lines)

    logger.info("Generated poll: %s", text[:100])
    return text


_PAID_APPROACHES = [
    "после мастурбации — только что кончила, тело ещё дрожит, сфоткала себя в этот момент",
    "голая в кровати — лежит без всего, простыня еле прикрывает, тёплая и расслабленная",
    "после душа — мокрое тело, капли на коже, полотенце вот-вот упадёт",
    "без белья — сняла всё, стоит перед зеркалом, любуется собой",
    "случайное горячее — не планировала но ракурс такой что сама охренела",
    "дерзкий вызов — 'готов увидеть?', 'не каждый выдержит', провоцирует открыть",
    "ночное возбуждение — одна в темноте, тело горит, рука между ног, сфоткала момент",
    "утро голышом — проснулась, простыня сползла, сонная и голая, утренний свет на коже",
    "примерка белья — новое кружево, или сняла всё и сравнивает. А может ничего не надела",
    "мокрая насквозь — ванна, дождь, бассейн. Ткань прилипла или нет никакой ткани",
    "для тебя лично — как будто отправляет интим конкретному мужчине. Шёпот на ухо",
    "секрет учительницы — 'никто из коллег не знает что я такое фоткаю после работы'",
    "восхищение собой — 'посмотрите какая я... ну серьёзно, КАК можно не хотеть это?'",
    "обрыв — 'я начала снимать, и потом... в общем, смотри сам'",
    "запретное — 'это слишком откровенно для бесплатного, но я не удержалась'",
    "тело после спорта — пот, мышцы, горячая кожа, обтягивающее или уже без него",
    "ленивая голая — валяется дома без всего, жара, лень одеваться, и зачем?",
    "вибратор рядом — намёк что на фото не только она, но и её любимая игрушка",
]

# Random mood modifiers — one picked per generation to shift the sexual vibe
_PAID_MOODS = [
    "пост-оргазменная — расслабленная, тело ещё вибрирует, лениво и удовлетворённо",
    "возбуждённая — на пике, еле сдерживается, энергия рвётся, хочет чтобы на неё смотрели",
    "томная — медленная, тягучая, каждое слово как стон, хочется растянуть момент",
    "дерзкая — нагло и уверенно, 'да, я такая, и что ты мне сделаешь?'",
    "сонно-голая — только проснулась, ещё не оделась, ленивая чувственность",
    "подвыпившая — раскрепощённая, пишет и фоткает то что трезвая бы не рискнула",
    "тихий шёпот — как будто шепчет на ухо в темноте, интимно и близко",
    "голодная — хочет мужских рук, тела, внимания. Одна и ей мало себя",
    "хищница — уверенный взгляд, 'я знаю что ты хочешь, и я хочу чтобы ты хотел'",
    "нежная-откровенная — мягкая, открытая, без стыда, показывает тело как подарок",
    "бунтарская — 'мне плевать что подумают, я горячая и я это покажу'",
]


def _extract_paid_caption(post_text: str) -> str:
    """Extract caption from paid post text stored as '[PAID N] caption'."""
    if post_text.startswith("[PAID "):
        bracket_end = post_text.find("] ", 6)
        if bracket_end != -1:
            return post_text[bracket_end + 2:]
    return post_text


_DEFAULT_PAID_CAPTION_FALLBACK = "Tap to unlock 💋"


def _resolve_paid_caption_fallback(config: ChannelBotConfig) -> str:
    """Pull the paid-caption fallback string from the VPS config if possible.

    ``ChannelBotConfig`` is its own dataclass and does not carry the VPS-wide
    ``channel_bot_paid_caption_fallback`` knob, so we reach into the shared
    ``server.config`` loader for the project's configured fallback. Missing
    config or any load error degrades to the built-in sane default.
    """
    try:
        from server.config import load_config as _vps_load_config  # local import: avoid cycle
        vps = _vps_load_config()
        value = getattr(vps, "channel_bot_paid_caption_fallback", "") or ""
        return value or _DEFAULT_PAID_CAPTION_FALLBACK
    except Exception:
        return _DEFAULT_PAID_CAPTION_FALLBACK


async def generate_paid_caption(
    config: ChannelBotConfig,
    channel: ChannelConfig,
    db_path: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    """Generate a teasing caption for a paid media post.

    Returns ``channel_bot_paid_caption_fallback`` from the VPS config when no
    LLM provider is configured so the paid drop still has a teaser line.
    """
    if not config.llm_provider:
        logger.info(
            "channel_bot.generate_paid_caption: llm_provider unset for channel %s, "
            "using paid caption fallback",
            channel.channel_id,
        )
        return _resolve_paid_caption_fallback(config)

    # Pick a random approach and mood for this generation
    approach = random.choice(_PAID_APPROACHES)
    mood = random.choice(_PAID_MOODS)

    # Gather recent paid captions to avoid repetition
    recent_block = ""
    if db_path:
        recent = get_recent_posts(db_path, channel_id=channel.channel_id, limit=10)
        paid_captions = [
            _extract_paid_caption(t)
            for t in recent
            if t.startswith("[PAID ")
        ]
        if paid_captions:
            items = "\n".join(f"- {c}" for c in paid_captions[:5])
            recent_block = (
                f"\nПоследние подписи к paid (ЗАПРЕЩЕНО повторять или перефразировать их):\n"
                f"{items}\n"
            )

    paid_system = f"""Ты горячая одинокая девушка, автор откровенного Telegram-канала. Персона:

{channel.legend}

ПРАВИЛО: Ты НЕ знаешь что на фото. НЕ описывай конкретно одежду, позу, фон.
Вместо этого - создай ощущение, намекни на состояние тела, на то что ты чувствовала когда фоткалась.
Пусть его воображение сделает остальное.

КАК ПИСАТЬ:
- 1-2 предложения. Коротко, горячо, на выдохе.
- Это личное сообщение мужчине, не реклама. Как будто шепчешь ему.
- Намекай на наготу, возбуждение, мастурбацию, мокрое тело - но через ощущения, не описание фото.
- У тебя НЕТ парня. Ты одна. Ты делаешь это для подписчиков.
- Только текст подписи.

ЗАПРЕЩЕНО:
- Описывать конкретно что на фото
- Слова: "платный", "разблокировать", "купить", "звёзды", "контент", "эксклюзив"
- Начинать с "ну что" / "ну чё" / "ну"
- Шаблоны: "не пожалеете", "не упустите", "кто смелый"
- "это фото" / "эту фотку" - не ссылайся на фото
- Упоминать парня/бывшего/мужчину в личной жизни - ты ОДНА
- Эмодзи 😏 - никогда
- Заканчивать эмодзи - в 70% случаев не надо

КОНЦОВКИ (варьируй):
- Многоточие... (томность)
- Точка. (дерзость)
- Без знака (обрыв)
- ) или )) (флирт)
- Вопрос? (провокация)
- 🔥 или 👀 - редко, не в каждом посте

Пиши на русском. Будь горячей. Будь голодной. Будь собой."""

    paid_user = f"""ЗАДАЧА: Напиши подпись к платному фото которая заставит мужчину НЕМЕДЛЕННО заплатить и открыть.

Приём: {approach}
Настроение: {mood}
{recent_block}
Напиши подпись. Только текст, ничего больше."""

    llm_model = model or config.llm_model or None
    client = _get_llm_client(config)
    raw = await client.ask(paid_user, model=llm_model, system_prompt=paid_system)
    return clean_generated_text(raw, legend=channel.legend)
