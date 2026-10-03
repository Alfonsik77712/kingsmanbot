"""Формирование текстов сообщений (HTML)."""
from __future__ import annotations

from collections import Counter
from datetime import tzinfo

from appbot.config import Settings
from appbot.db.models import STATUS_TITLES, Answer, Application, Media, Status, StatusHistory
from appbot.media import KIND_NAMES
from appbot.questions import QType, Question
from appbot.utils import esc, fmt_dt, truncate

HELP_USER = (
    "/apply — подать заявку\n"
    "/cancel — отменить заполнение\n"
    "/myapplications — мои заявки"
)
HELP_ADMIN = (
    "\n\n<b>Для администраторов:</b>\n"
    "/applications — список заявок\n"
    "/stats — статистика\n"
    "/admins — список администраторов"
)
HELP_MANAGER = (
    "\n\n<b>Управление администраторами:</b>\n"
    "/addadmin ID — добавить администратора\n"
    "/removeadmin ID — удалить администратора"
)


def question_hint(q: Question) -> str | None:
    """Подсказка под вопросом — какой ответ ожидается."""
    t = q.type
    if t == QType.TEXT:
        hint = "Отправьте ответ текстовым сообщением."
    elif t == QType.NUMBER:
        hint = "Отправьте целое число"
        if q.min_value is not None and q.max_value is not None:
            hint += f" от {q.min_value} до {q.max_value}"
        elif q.min_value is not None:
            hint += f" не меньше {q.min_value}"
        elif q.max_value is not None:
            hint += f" не больше {q.max_value}"
        hint += "."
    elif t == QType.PHOTO:
        hint = "Отправьте фотографию (именно как фото, не как файл)."
    elif t == QType.VIDEO:
        hint = "Отправьте видео."
    elif t == QType.DOCUMENT:
        hint = "Отправьте файл как документ (через «Файл»)."
    elif t == QType.MEDIA:
        hint = "Отправьте фото, видео или документ."
    else:
        hint = "Можно отправить текст или файл."
    if q.multiple:
        hint += f" Можно несколько (до {q.max_files}); когда закончите — нажмите «Готово»."
    if not q.required:
        hint += " Вопрос необязательный."
    return hint


def media_summary(items: list[Media]) -> str:
    counts = Counter(m.kind for m in items)
    desc = ", ".join(f"{KIND_NAMES.get(kind, kind)} ×{n}" for kind, n in counts.items())
    line = f"📎 Файлов: {len(items)} ({desc})"
    captions = [m.caption for m in items if m.caption]
    if captions:
        line += "\n💬 Подписи: " + "; ".join(esc(truncate(c, 200)) for c in captions)
    return line


def render_answers(answers: list[Answer], media_by_answer: dict[int, list[Media]]) -> str:
    """Все ответы полностью (текст + сводка по файлам)."""
    blocks: list[str] = []
    for a in sorted(answers, key=lambda x: (x.position, x.id)):
        parts: list[str] = []
        if a.text_value:
            parts.append(esc(a.text_value))
        items = media_by_answer.get(a.id, [])
        if items:
            parts.append(media_summary(items))
        if not parts:
            parts.append("— (пропущено)" if a.skipped else "—")
        blocks.append(f"<b>{esc(a.question_label)}:</b>\n" + "\n".join(parts))
    return "\n\n".join(blocks) or "—"


def user_mention(app: Application) -> str:
    name = app.full_name or app.username or str(app.user_id)
    text = f'<a href="tg://user?id={app.user_id}">{esc(name)}</a>'
    if app.username:
        text += f" (@{esc(app.username)})"
    return text


def render_header(app: Application, settings: Settings) -> str:
    tz: tzinfo = settings.timezone
    lines = [
        f"<b>Заявка #{app.id}</b>",
        "",
        "<b>Пользователь:</b>",
        user_mention(app),
        "",
        "<b>Telegram ID:</b>",
        f"<code>{app.user_id}</code>",
        "",
        "<b>Дата подачи:</b>",
        fmt_dt(app.submitted_at or app.created_at, tz),
        "",
        "<b>Статус:</b>",
        STATUS_TITLES.get(app.status, app.status),
    ]
    if app.processed_by:
        lines += [
            "",
            "<b>Обработал:</b>",
            f"<code>{app.processed_by}</code> — {fmt_dt(app.status_changed_at, tz)}",
        ]
    if app.admin_comment:
        lines += ["", "<b>Комментарий администратора:</b>", esc(app.admin_comment)]
    return "\n".join(lines)


def render_history(app: Application, history: list[StatusHistory], settings: Settings) -> str:
    lines = [f"<b>История статусов заявки #{app.id}</b>", ""]
    if not history:
        lines.append("Записей нет.")
    for h in history:
        old = STATUS_TITLES.get(h.old_status, "—") if h.old_status else "—"
        new = STATUS_TITLES.get(h.new_status, h.new_status)
        who = f"админ <code>{h.admin_id}</code>" if h.admin_id else "пользователь"
        lines.append(f"{fmt_dt(h.created_at, settings.timezone)} — {old} → {new} ({who})")
        if h.comment:
            lines.append(f"   💬 {esc(truncate(h.comment, 300))}")
    return "\n".join(lines)


def status_notification(app_id: int, status: str, comment: str | None) -> str:
    if status == Status.APPROVED:
        text = f"🟢 Ваша заявка #{app_id} была одобрена."
    elif status == Status.REJECTED:
        text = f"🔴 Ваша заявка #{app_id} была отклонена."
    else:
        text = f"🟡 Ваша заявка #{app_id} возвращена на рассмотрение."
    if comment:
        text += f"\n\n💬 <b>Комментарий администратора:</b>\n{esc(comment)}"
    return text
