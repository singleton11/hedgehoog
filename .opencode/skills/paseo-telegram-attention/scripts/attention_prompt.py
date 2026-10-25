"""Validate and render bounded, escaped Telegram decision prompts (no network I/O)."""

import html
import re

from telegram_attention import AttentionError


OTHER = '__other__'
HEADINGS = {'approval': 'Approval needed', 'clarification': 'Clarification needed',
            'blocker': 'Help needed'}
PROMPT_KEYS = {'kind', 'context_label', 'context_url', 'question', 'options', 'recommended'}


def utf16_length(text):
    return len(text.encode('utf-16-le')) // 2


def _text(value, limit, field):
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or any(ord(c) < 32 for c in value) or len(value) > limit):
        raise AttentionError(f'prompt: {field} must be nonempty single-line text of at most {limit} characters.')
    try:
        utf16_length(value)
    except UnicodeError:
        raise AttentionError('prompt: text must contain valid Unicode.') from None
    return value


def validate_prompt(prompt):
    if not isinstance(prompt, dict) or set(prompt) != PROMPT_KEYS:
        raise AttentionError('prompt: provide kind, context_label, context_url, question, options and recommended only.')
    kind = prompt['kind']
    if not isinstance(kind, str) or kind not in HEADINGS:
        raise AttentionError('prompt: kind must be approval, clarification or blocker.')
    label = _text(prompt['context_label'], 80, 'context_label')
    url = prompt['context_url']
    if not isinstance(url, str) or not re.fullmatch(
            r'https://github\.com/singleton11/hedgehoog/(?:issues|pull)/[1-9][0-9]*', url):
        raise AttentionError('prompt: context_url must be an issue or PR URL in singleton11/hedgehoog.')
    question = _text(prompt['question'], 500, 'question')
    options = prompt['options']
    if not isinstance(options, list) or not 2 <= len(options) <= 5:
        raise AttentionError('prompt: provide 2-5 single-choice options.')
    normalized = []
    ids = set()
    for option in options:
        if not isinstance(option, dict) or set(option) != {'id', 'label', 'description'}:
            raise AttentionError('prompt: each option needs id, label and description only.')
        oid = option['id']
        if not isinstance(oid, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,31}', oid) or oid in ids:
            raise AttentionError('prompt: option IDs must be unique 1-32 character ASCII identifiers.')
        ids.add(oid)
        normalized.append({'id': oid, 'label': _text(option['label'], 40, 'option label'),
                           'description': _text(option['description'], 160, 'option description')})
    if kind == 'approval' and not ids.intersection({'decline', 'cancel'}):
        raise AttentionError('prompt: approval requires an explicit decline or cancel option ID.')
    recommended = prompt['recommended']
    if not isinstance(recommended, str) or recommended not in ids:
        raise AttentionError('prompt: recommended must identify exactly one option.')
    normalized.sort(key=lambda option: option['id'] != recommended)
    result = {'kind': kind, 'context_label': label, 'context_url': url, 'question': question,
              'options': normalized, 'recommended': recommended}
    # Validate the parsed message limit, not the longer escaped HTML representation.
    rendered, visible = _body(result)
    if utf16_length(visible) > 4096:
        raise AttentionError('prompt: rendered text exceeds 4096 UTF-16 units; shorten context or options.')
    return result


def _body(prompt):
    heading = HEADINGS[prompt['kind']]
    body = [f'<b>{heading}</b>',
            f'<a href="{html.escape(prompt["context_url"], quote=True)}">{html.escape(prompt["context_label"])}</a>',
            '', html.escape(prompt['question']), '']
    visible = [heading, prompt['context_label'], '', prompt['question'], '']
    for index, option in enumerate(prompt['options'], 1):
        label = option['label'] + (' (Recommended)' if option['id'] == prompt['recommended'] else '')
        body.append(f'{index}. <b>{html.escape(label)}</b> - {html.escape(option["description"])}')
        visible.append(f'{index}. {label} - {option["description"]}')
    footer = 'Choose a button, or use Reply to write a different answer. Task paused.'
    body.extend(['', footer])
    visible.extend(['', footer])
    return '\n'.join(body), '\n'.join(visible)


def render_prompt(prompt, tokens):
    prompt = validate_prompt(prompt)
    expected = {option['id'] for option in prompt['options']} | {OTHER}
    if not isinstance(tokens, dict) or set(tokens) != expected:
        raise AttentionError('prompt: persisted callback tokens must match all options and Other.')
    values = list(tokens.values())
    if any(not isinstance(token, str) or not re.fullmatch(r'a1:[A-Za-z0-9_-]+', token)
           or not 1 <= len(token.encode('utf-8')) <= 64 for token in values) or len(set(values)) != len(values):
        raise AttentionError('prompt: callback tokens must be unique versioned opaque values of 1-64 UTF-8 bytes.')
    rows = []
    for option in prompt['options']:
        label = option['label'] + (' (Recommended)' if option['id'] == prompt['recommended'] else '')
        rows.append([{'text': label, 'callback_data': tokens[option['id']]}])
    rows.append([{'text': 'Other / write a reply', 'callback_data': tokens[OTHER]}])
    return {'text': _body(prompt)[0], 'parse_mode': 'HTML',
            'reply_markup': {'inline_keyboard': rows}}
